package protocol

import (
	"context"
	"errors"
	"io"
	"net"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"yyb_go/internal/store"
)

// Complete SOCKS5 negotiation, then accept protocol bytes without answering.
func silentProtocolPeer(t *testing.T, socks bool) (Target, <-chan struct{}) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ready, finished, shutdown := make(chan struct{}), make(chan struct{}), make(chan struct{})
	go func() {
		defer close(finished)
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		stop := make(chan struct{})
		defer close(stop)
		go func() {
			select {
			case <-shutdown:
				_ = conn.Close()
			case <-stop:
			}
		}()
		if socks {
			header := make([]byte, 2)
			if _, err := io.ReadFull(conn, header); err != nil {
				return
			}
			if _, err := io.CopyN(io.Discard, conn, int64(header[1])); err != nil {
				return
			}
			_, _ = conn.Write([]byte{5, 0})
			request := make([]byte, 5)
			if _, err := io.ReadFull(conn, request); err != nil {
				return
			}
			if _, err := io.CopyN(io.Discard, conn, int64(request[4])+2); err != nil {
				return
			}
			_, _ = conn.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 80})
		}
		if _, err := io.ReadFull(conn, make([]byte, 1)); err != nil {
			return
		}
		close(ready)
		_, _ = io.Copy(io.Discard, conn)
	}()
	t.Cleanup(func() { close(shutdown); _ = listener.Close(); <-finished })
	addr := listener.Addr().(*net.TCPAddr)
	return Target{IP: "127.0.0.1", Port: addr.Port}, ready
}

func TestProtocolReadHonoursParentDeadline(t *testing.T) {
	for _, kind := range []string{"LongLink", "ShortLink"} {
		t.Run(kind, func(t *testing.T) {
			target, _ := silentProtocolPeer(t, false)
			ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
			defer cancel()
			done := make(chan error, 1)
			go func() {
				if kind == "LongLink" {
					client, err := connectMmtls(ctx, target, 5*time.Second, "", false)
					if client != nil {
						client.close()
					}
					done <- err
				} else {
					_, _, err := send0RTTRaw(ctx, target, pskEntry{PreSharedKey: "00", TicketEntry: "00"}, nil, []byte("test"), 5*time.Second, "", false)
					done <- err
				}
			}()
			select {
			case err := <-done:
				if err == nil {
					t.Fatal("timed-out operation succeeded")
				}
			case <-time.After(2 * time.Second):
				t.Fatal("read deadline exceeded operation budget")
			}
		})
	}
}

func TestSessionWaitHonoursLoginBudget(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	p := NewPool(Config{LoginTimeout: 100 * time.Millisecond}, db)
	lock := p.lockFor("1\x00")
	if err := acquire(context.Background(), lock); err != nil {
		t.Fatal(err)
	}
	defer release(lock)
	done := make(chan error, 1)
	go func() { _, err := p.state(context.Background(), "unused", 1, "", false); done <- err }()
	select {
	case err := <-done:
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("queue error = %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("session queue ignored login budget")
	}
}

func TestProtocolReadStopsAfterCancellation(t *testing.T) {
	for _, kind := range []string{"LongLink", "ShortLink"} {
		for _, mode := range []string{"direct", "socks5"} {
			t.Run(kind+"/"+mode, func(t *testing.T) {
				target, ready := silentProtocolPeer(t, mode == "socks5")
				proxy := ""
				if mode == "socks5" {
					proxy = "socks5://" + net.JoinHostPort(target.IP, strconv.Itoa(target.Port))
					target = Target{IP: "example.invalid", Port: 443}
				}
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				done := make(chan error, 1)
				go func() {
					if kind == "LongLink" {
						client, err := connectMmtls(ctx, target, 5*time.Second, proxy, false)
						if client != nil {
							client.close()
						}
						done <- err
					} else {
						_, _, err := send0RTTRaw(ctx, target, pskEntry{PreSharedKey: "00", TicketEntry: "00"}, nil, []byte("test"), 5*time.Second, proxy, false)
						done <- err
					}
				}()
				select {
				case <-ready:
				case <-time.After(3 * time.Second):
					t.Fatal("protocol read did not begin")
				}
				cancel()
				select {
				case err := <-done:
					if err == nil {
						t.Fatal("cancelled operation succeeded")
					}
				case <-time.After(time.Second):
					t.Fatal("protocol read ignored cancellation after TCP/proxy connect")
				}
			})
		}
	}
}
