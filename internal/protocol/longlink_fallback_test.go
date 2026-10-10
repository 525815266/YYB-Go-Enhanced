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
