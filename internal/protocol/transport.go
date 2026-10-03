package protocol

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type tcpProxy struct {
	Scheme   string
	Host     string
	Port     string
	Username string
	Password string
}

func parseTCPProxy(value string) (*tcpProxy, error) {
	if value == "" {
		return nil, nil
	}
	u, err := url.Parse(value)
	if err != nil {
		return nil, err
	}
	if u.Scheme != "socks5" && u.Scheme != "http-connect" {
		return nil, fmt.Errorf("tcp_proxy must use socks5:// or http-connect://")
	}
	if u.Hostname() == "" || u.Port() == "" {
		return nil, fmt.Errorf("tcp_proxy must include host and port")
	}
	proxy := &tcpProxy{Scheme: u.Scheme, Host: u.Hostname(), Port: u.Port()}
	if u.User != nil {
		proxy.Username = u.User.Username()
		proxy.Password, _ = u.User.Password()
	}
	return proxy, nil
}

func NewHTTPTransport(proxyValue string, fallbackDirect bool) (*http.Transport, error) {
	proxy, err := parseTCPProxy(proxyValue)
	if err != nil {
		return nil, err
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	if proxy == nil {
		return transport, nil
	}
	transport.DialContext = func(ctx context.Context, _ string, address string) (net.Conn, error) {
		host, rawPort, err := net.SplitHostPort(address)
		if err != nil {
			return nil, err
		}
		port, err := strconv.Atoi(rawPort)
		if err != nil {
			return nil, err
		}
		// net/http may keep a dial alive after its initiating request is cancelled
		// for connection reuse. Bound proxy negotiation even without a deadline.
		conn, err := dialViaProxy(ctx, proxy, host, port, 30*time.Second)
		if err == nil || !fallbackDirect {
			return conn, err
		}
		return dialDirect(ctx, host, port, 30*time.Second)
	}
	return transport, nil
}

func dialTCP(ctx context.Context, host string, port int, timeout time.Duration, proxyValue string, fallbackDirect bool) (net.Conn, error) {
	proxy, err := parseTCPProxy(proxyValue)
	if err != nil {
		return nil, err
	}
	if proxy == nil {
		return dialDirect(ctx, host, port, timeout)
	}
	conn, err := dialViaProxy(ctx, proxy, host, port, timeout)
	if err == nil {
		return conn, nil
	}
	if !fallbackDirect {
		return nil, err
	}
	return dialDirect(ctx, host, port, timeout)
}

func dialDirect(ctx context.Context, host string, port int, timeout time.Duration) (net.Conn, error) {
	var d net.Dialer
	if timeout > 0 {
		d.Timeout = timeout
	}
	return d.DialContext(ctx, "tcp", net.JoinHostPort(host, strconv.Itoa(port)))
}

func dialViaProxy(ctx context.Context, proxy *tcpProxy, targetHost string, targetPort int, timeout time.Duration) (net.Conn, error) {
	// DialContext only covers TCP establishment. Proxy negotiation also performs
	// blocking reads, so honour cancellation and the earlier request deadline.
	deadline, hasDeadline := ctx.Deadline()
	if timeout > 0 {
		if limit := time.Now().Add(timeout); !hasDeadline || limit.Before(deadline) {
			deadline, hasDeadline = limit, true
		}
	}
	conn, err := dialDirect(ctx, proxy.Host, mustAtoi(proxy.Port), timeout)
	if err != nil {
		return nil, fmt.Errorf("dial proxy failed: %w", err)
	}
	if hasDeadline {
		_ = conn.SetDeadline(deadline)
	}
	stopCancel := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stopCancel()
	if proxy.Scheme == "socks5" {
		err = socks5Connect(conn, proxy, targetHost, targetPort)
	} else {
		err = httpConnect(conn, proxy, targetHost, targetPort)
	}
	if err != nil {
		_ = conn.Close()
		if ctx.Err() != nil {
			err = ctx.Err()
		}
		return nil, fmt.Errorf("%s proxy handshake to %s failed: %w", proxy.Scheme, net.JoinHostPort(targetHost, strconv.Itoa(targetPort)), err)
	}
	if !stopCancel() {
		_ = conn.Close()
		return nil, ctx.Err()
	}
	_ = conn.SetDeadline(time.Time{})
	return conn, nil
}

func socks5Connect(conn net.Conn, proxy *tcpProxy, targetHost string, targetPort int) error {
	methods := []byte{0x00}
	if proxy.Username != "" {
		methods = append(methods, 0x02)
	}
	greeting := append([]byte{0x05, byte(len(methods))}, methods...)
	if _, err := conn.Write(greeting); err != nil {
		return err
	}
	buf := make([]byte, 2)
	if _, err := io.ReadFull(conn, buf); err != nil {
		return err
	}
	if buf[0] != 0x05 {
		return fmt.Errorf("SOCKS5 no-auth negotiation failed: %x", buf)
	}
	if buf[1] == 0x02 {
		if err := socks5Authenticate(conn, proxy.Username, proxy.Password); err != nil {
			return err
		}
	} else if buf[1] != 0x00 {
		return fmt.Errorf("SOCKS5 authentication method rejected: %x", buf)
	}
	hostBytes := []byte(targetHost)
	if len(hostBytes) > 255 {
		return fmt.Errorf("SOCKS5 target host too long")
	}
	req := []byte{0x05, 0x01, 0x00, 0x03, byte(len(hostBytes))}
	req = append(req, hostBytes...)
	var p [2]byte
	binary.BigEndian.PutUint16(p[:], uint16(targetPort))
	req = append(req, p[:]...)
	if _, err := conn.Write(req); err != nil {
		return err
	}
	head := make([]byte, 4)
	if _, err := io.ReadFull(conn, head); err != nil {
		return err
	}
	if head[0] != 5 || head[1] != 0 {
		return fmt.Errorf("SOCKS5 connect failed: %x", head)
	}
	switch head[3] {
	case 1:
		_, err := io.CopyN(io.Discard, conn, 6)
		return err
	case 3:
		ln := make([]byte, 1)
		if _, err := io.ReadFull(conn, ln); err != nil {
			return err
		}
		_, err := io.CopyN(io.Discard, conn, int64(ln[0])+2)
		return err
	case 4:
		_, err := io.CopyN(io.Discard, conn, 18)
		return err
	default:
		return fmt.Errorf("SOCKS5 unsupported bind address type: %d", head[3])
	}
}

func socks5Authenticate(conn net.Conn, username, password string) error {
	if len(username) == 0 || len(username) > 255 || len(password) > 255 {
		return fmt.Errorf("SOCKS5 username/password length is invalid")
	}
	request := []byte{0x01, byte(len(username))}
	request = append(request, username...)
	request = append(request, byte(len(password)))
	request = append(request, password...)
	if _, err := conn.Write(request); err != nil {
		return err
	}
	response := make([]byte, 2)
	if _, err := io.ReadFull(conn, response); err != nil {
		return err
	}
	if response[0] != 0x01 || response[1] != 0x00 {
		return fmt.Errorf("SOCKS5 username/password authentication failed")
	}
	return nil
}

func httpConnect(conn net.Conn, proxy *tcpProxy, targetHost string, targetPort int) error {
	target := net.JoinHostPort(targetHost, strconv.Itoa(targetPort))
	authorization := ""
	if proxy.Username != "" {
		token := base64.StdEncoding.EncodeToString([]byte(proxy.Username + ":" + proxy.Password))
		authorization = "Proxy-Authorization: Basic " + token + "\r\n"
	}
	req := fmt.Sprintf("CONNECT %s HTTP/1.1\r\nHost: %s\r\n%sProxy-Connection: keep-alive\r\n\r\n", target, target, authorization)
	if _, err := conn.Write([]byte(req)); err != nil {
		return err
	}
	br := bufio.NewReader(conn)
	line, err := br.ReadString('\n')
	if err != nil {
		return err
	}
	parts := strings.Fields(line)
	if len(parts) < 2 || !strings.HasPrefix(parts[1], "2") {
		return fmt.Errorf("HTTP CONNECT failed: %s", strings.TrimSpace(line))
	}
	for {
		l, err := br.ReadString('\n')
		if err != nil {
			return err
		}
		if l == "\r\n" || l == "\n" {
			break
		}
	}
	return nil
}

func mustAtoi(s string) int {
	n, _ := strconv.Atoi(s)
	return n
}
