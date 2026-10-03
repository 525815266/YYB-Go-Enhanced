package protocol

import (
	"bufio"
	"context"
	"encoding/base64"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestProxyHandshakeHonoursCancellationAndDeadline(t *testing.T) {
	for _, scheme := range []string{"socks5", "http-connect"} {
		for _, mode := range []string{"cancel", "deadline", "proxy-timeout"} {
			t.Run(scheme+"-"+mode, func(t *testing.T) {
				listener, err := net.Listen("tcp", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				defer listener.Close()
				started := make(chan struct{})
				go func() {
					conn, err := listener.Accept()
					if err != nil {
						return
					}
					defer conn.Close()
					one := make([]byte, 1)
					_, _ = conn.Read(one)
					close(started)
					_, _ = io.Copy(io.Discard, conn) // Accept but never answer negotiation.
				}()
				ctx, cancel := context.WithCancel(context.Background())
				if mode == "deadline" {
					cancel()
					ctx, cancel = context.WithTimeout(context.Background(), 150*time.Millisecond)
				}
				defer cancel()
				proxy, err := parseTCPProxy(scheme + "://" + listener.Addr().String())
				if err != nil {
					t.Fatal(err)
				}
				done := make(chan error, 1)
				timeout := 5 * time.Second
				if mode == "proxy-timeout" {
					timeout = 150 * time.Millisecond
				}
				go func() {
					conn, err := dialViaProxy(ctx, proxy, "example.invalid", 443, timeout)
					if conn != nil {
						_ = conn.Close()
					}
					done <- err
				}()
				select {
				case <-started:
				case <-time.After(2 * time.Second):
					t.Fatal("proxy handshake did not start")
				}
				if mode == "cancel" {
					cancel()
				}
				select {
				case err := <-done:
					if err == nil || !strings.Contains(err.Error(), "proxy handshake") {
						t.Fatalf("missing handshake diagnostic: %v", err)
					}
				case <-time.After(2 * time.Second):
					t.Fatal("proxy handshake ignored request cancellation/deadline")
				}
			})
		}
	}
}

func TestSuccessfulProxyConnectionOutlivesDialContext(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	serverDone := make(chan error, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			serverDone <- err
			return
		}
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
		if _, err = http.ReadRequest(bufio.NewReader(conn)); err == nil {
			_, err = io.WriteString(conn, "HTTP/1.1 200 Connection established\r\n\r\n")
		}
		if err == nil {
			_, err = io.Copy(conn, conn)
		}
		serverDone <- err
	}()
	proxy, err := parseTCPProxy("http-connect://" + listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	conn, err := dialViaProxy(ctx, proxy, "example.invalid", 443, 150*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	cancel()
	time.Sleep(200 * time.Millisecond) // Also cross the old negotiation deadline.
	if _, err := io.WriteString(conn, "ping"); err != nil {
		t.Fatalf("successful connection retained dial cancellation/deadline: %v", err)
	}
	response := make([]byte, 4)
	if _, err := io.ReadFull(conn, response); err != nil || string(response) != "ping" {
		t.Fatalf("returned tunnel unusable: %v", err)
	}
	_ = conn.Close()
	if err := <-serverDone; err != nil {
		t.Fatal(err)
	}
}

func TestParseTCPProxyCredentials(t *testing.T) {
	proxy, err := parseTCPProxy("http-connect://user:pass@127.0.0.1:8080")
	if err != nil {
		t.Fatalf("parseTCPProxy() error = %v", err)
	}
	if proxy.Username != "user" || proxy.Password != "pass" {
		t.Fatalf("proxy credentials = %q/%q", proxy.Username, proxy.Password)
	}
}

func TestHTTPConnectSendsBasicAuthentication(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()
	done := make(chan error, 1)
	go func() {
		reader := bufio.NewReader(server)
		var request strings.Builder
		for {
			line, err := reader.ReadString('\n')
			if err != nil {
				done <- err
				return
			}
			request.WriteString(line)
			if line == "\r\n" {
				break
			}
		}
		want := "Proxy-Authorization: Basic " + base64.StdEncoding.EncodeToString([]byte("user:pass"))
		if !strings.Contains(request.String(), want) {
			done <- errors.New("missing proxy authorization")
			return
		}
		_, err := server.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n"))
		done <- err
	}()
	proxy := &tcpProxy{Username: "user", Password: "pass"}
	if err := httpConnect(client, proxy, "example.com", 443); err != nil {
		t.Fatalf("httpConnect() error = %v", err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}
