package protocol

import (
	"context"
	"errors"
	"net"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestLonglinkFallbackAfterSilentHandshake(t *testing.T) {
	for _, socks := range []bool{false, true} {
		t.Run(strconv.FormatBool(socks), func(t *testing.T) {
			target, ready := silentProtocolPeer(t, socks)
			proxy := ""
			if socks {
				proxy = "socks5://" + net.JoinHostPort(target.IP, strconv.Itoa(target.Port))
			}
			fallback := Target{IP: "example.invalid", Port: 443}
			calls := 0
			var successfulCtx context.Context
			st, err := tryLonglinkCandidates(context.Background(), []Target{target, fallback}, time.Second,
				func(ctx context.Context, candidate Target) (WmpfSession, error) {
					calls++
					if candidate == target {
						client, err := connectMmtls(ctx, candidate, 5*time.Second, proxy, false)
						if client != nil {
							client.close()
						}
						return WmpfSession{}, err
					}
					if ctx.Err() != nil {
						t.Fatalf("fallback received an expired context: %v", ctx.Err())
					}
					successfulCtx = ctx
					return WmpfSession{Session: AppSession{UIN: 123}}, nil
				})
			if err != nil || calls != 2 || st.Session.UIN != 123 {
				t.Fatalf("fallback: calls=%d session=%+v err=%v", calls, st, err)
			}
			select {
			case <-ready:
			default:
				t.Fatal("first endpoint never received the handshake")
			}
			if successfulCtx.Err() == nil {
				t.Fatal("successful attempt retained its context resources")
			}
		})
	}
}

func TestLonglinkCandidatesHonorCancellation(t *testing.T) {
	for _, cancelDuringAttempt := range []bool{false, true} {
		ctx, cancel := context.WithCancel(context.Background())
		if !cancelDuringAttempt {
			cancel()
		}
		calls := 0
		_, err := tryLonglinkCandidates(ctx, []Target{{IP: "first", Port: 443}, {IP: "second", Port: 80}}, time.Second,
			func(attemptCtx context.Context, _ Target) (WmpfSession, error) {
				calls++
				cancel()
				<-attemptCtx.Done()
				return WmpfSession{}, attemptCtx.Err()
			})
		cancel()
		if !errors.Is(err, context.Canceled) || calls > 1 || (!cancelDuringAttempt && calls != 0) {
			t.Fatalf("cancelDuringAttempt=%v: calls=%d err=%v", cancelDuringAttempt, calls, err)
		}
	}
}

func TestLonglinkCandidatesBoundTotalWaitAndKeepFailure(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	started := time.Now()
	calls := 0
	_, err := tryLonglinkCandidates(ctx, []Target{{IP: "first", Port: 443}, {IP: "last", Port: 8080}}, 5*time.Second,
		func(attemptCtx context.Context, _ Target) (WmpfSession, error) {
			calls++
			<-attemptCtx.Done()
			return WmpfSession{}, attemptCtx.Err()
		})
	if !errors.Is(err, context.DeadlineExceeded) || calls != 2 || !strings.Contains(err.Error(), "last:8080") {
		t.Fatalf("calls=%d err=%v", calls, err)
	}
	if time.Since(started) > 2*time.Second {
		t.Fatal("candidate retries exceeded the parent budget")
	}
}

func TestLonglinkSingleCandidateKeepsFullBudget(t *testing.T) {
	_, err := tryLonglinkCandidates(context.Background(), []Target{{IP: "only", Port: 443}}, time.Minute,
		func(ctx context.Context, _ Target) (WmpfSession, error) {
			deadline, _ := ctx.Deadline()
			if time.Until(deadline) < 55*time.Second {
				t.Fatal("single endpoint lost its available budget")
			}
			return WmpfSession{}, nil
		})
	if err != nil {
		t.Fatal(err)
	}
}

func TestLonglinkUnreachableHTTPDNSCanReachHostnameFallback(t *testing.T) {
	// Model a nonempty but unusable HTTPDNS answer such as #93. Regular DNS
	// must still receive an attempt rather than failing on the HTTPDNS list.
	targets := longlinkLoginTargets([]Target{{IP: "203.0.113.10", Port: 443}}, 6)
	calls := 0
	st, err := tryLonglinkCandidates(context.Background(), targets, time.Second,
		func(ctx context.Context, candidate Target) (WmpfSession, error) {
			calls++
			if candidate.IP != longlinkDomain {
				return WmpfSession{}, &net.OpError{Op: "dial", Net: "tcp", Err: errors.New("unreachable")}
			}
			if ctx.Err() != nil {
				t.Fatal("hostname fallback lost its time budget")
			}
			return WmpfSession{Session: AppSession{UIN: 123}}, nil
		})
	if err != nil || calls != 2 || st.Session.UIN != 123 {
		t.Fatalf("calls=%d session=%+v err=%v", calls, st, err)
	}
}

func TestLonglinkFailureIncludesAllEndpointStages(t *testing.T) {
	dialErr := errors.New("dial refused")
	handshakeErr := errors.New("handshake failed")
	_, err := tryLonglinkCandidates(context.Background(), []Target{
		{IP: "203.0.113.10", Port: 443}, {IP: longlinkDomain, Port: 443},
	}, time.Second, func(_ context.Context, candidate Target) (WmpfSession, error) {
		if candidate.IP == longlinkDomain {
			return WmpfSession{}, handshakeErr
		}
		return WmpfSession{}, dialErr
	})
	for _, text := range []string{"candidate 1/2 203.0.113.10:443", "dial refused", "candidate 2/2 longcloud.weixin.com:443", "handshake failed"} {
		if err == nil || !strings.Contains(err.Error(), text) {
			t.Fatalf("missing %q in %v", text, err)
		}
	}
	if !errors.Is(err, dialErr) || !errors.Is(err, handshakeErr) {
		t.Fatalf("error causes lost: %v", err)
	}
}

func TestLonglinkTicketRejectionStopsEndpointRetries(t *testing.T) {
	rejected := &ManualAuthRejectedError{Code: -10}
	calls := 0
	_, err := tryLonglinkCandidates(context.Background(), []Target{
		{IP: "first", Port: 443}, {IP: "second", Port: 443}, {IP: "third", Port: 443},
	}, time.Second, func(context.Context, Target) (WmpfSession, error) {
		calls++
		if calls == 1 {
			return WmpfSession{}, errors.New("dial refused")
		}
		return WmpfSession{}, rejected
	})
	if calls != 2 || !errors.Is(err, rejected) || !strings.Contains(err.Error(), "dial refused") {
		t.Fatalf("ticket rejection retried or prior diagnostic lost: calls=%d err=%v", calls, err)
	}
}
