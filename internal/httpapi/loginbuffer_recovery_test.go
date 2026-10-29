package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestApplicationEmptyCodeDoesNotRecoverAccountCredentials(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	acc := insertKeepAliveTestAccount(t, app, "application-empty-code", time.Now().Add(time.Hour))
	before, _ := json.Marshal(acc.Credentials)
	if err := app.db.PutSession(ctx, acc.ID, nil, map[string]any{"test": "healthy-session"}, time.Now().Add(time.Hour).Unix(), ""); err != nil {
		t.Fatal(err)
	}
	refreshes, fetches := 0, 0
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
		fetches++
		return "", errors.New("unexpected buffer fetch")
	}
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		refreshes++
		return protocol.LoginBufferCredentials{}, errors.New("unexpected refresh")
	}
	_, err := app.invokeWXApp(ctx, acc, "wx-app-without-code", nil,
		func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
			return nil, fmt.Errorf("application response: %w", protocol.ErrLoginCodeUnavailable)
		})
	if !errors.Is(err, protocol.ErrLoginCodeUnavailable) || fetches != 0 || refreshes != 0 {
		t.Fatalf("application failure triggered recovery: fetches=%d refreshes=%d error=%v", fetches, refreshes, err)
	}
	out := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/wxapp/getCode", strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wx-app-without-code"}`, acc.ID)))
	app.callWXApp(out, req, false, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, protocol.ErrLoginCodeUnavailable
	})
	if out.Code != http.StatusBadGateway || fetches != 0 || refreshes != 0 || strings.Contains(out.Body.String(), "re-scan required") {
		t.Fatalf("HTTP application error became account expiry: status=%d fetches=%d refreshes=%d", out.Code, fetches, refreshes)
	}
	stored, getErr := app.db.GetAccount(ctx, acc.ID)
	if getErr != nil {
		t.Fatal(getErr)
	}
	after, _ := json.Marshal(stored.Credentials)
	if getErr != nil || accountStatus(stored) != "alive" || string(before) != string(after) {
		t.Fatal("application failure changed account")
	}
	if _, err = app.db.GetSession(ctx, acc.ID, ""); err != nil {
		t.Fatalf("shared session lost: %v", err)
	}
}

func TestManualAuthRejectionRecoversBufferWithoutTokenRotation(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	acc := insertKeepAliveTestAccount(t, app, "buffer-recovery", time.Now().Add(time.Hour))
	before, _ := json.Marshal(acc.Credentials)
	fetches, refreshes, calls := 0, 0, 0
	app.fetchLoginBuffer = func(_ context.Context, creds protocol.LoginBufferCredentials) (string, error) {
		fetches++
		if creds.AccessToken != protocol.CredentialsFromMap(acc.Credentials).AccessToken {
			t.Fatal("wrong access token")
		}
		return "fresh-buffer", nil
	}
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		refreshes++
		return protocol.LoginBufferCredentials{}, errors.New("must not rotate token")
	}
	result, err := app.invokeWXApp(ctx, acc, "wx-test", nil, func(_ context.Context, used *store.WechatAccount, _ string, _ map[string]any, _ string, _ bool) (map[string]any, error) {
		calls++
		if calls == 1 {
			// One candidate timed out, another returned a real business rejection.
			// The transport failure must not hide the actionable rejection.
			return nil, fmt.Errorf("login candidates: %w", errors.Join(&protocol.ManualAuthRejectedError{Code: -10}, &net.OpError{Op: "read", Net: "tcp", Err: context.DeadlineExceeded}))
		}
		if used.LoginBuffer != "fresh-buffer" {
			t.Fatal("did not use fresh buffer")
		}
		return map[string]any{"code": "test-code"}, nil
	})
	if err != nil || result["code"] != "test-code" || calls != 2 || fetches != 1 || refreshes != 0 {
		t.Fatalf("calls=%d fetches=%d refreshes=%d err=%v", calls, fetches, refreshes, err)
	}
	afterAcc, _ := app.db.GetAccount(ctx, acc.ID)
	after, _ := json.Marshal(afterAcc.Credentials)
	if string(before) != string(after) || acc.LoginBuffer == "fresh-buffer" {
		t.Fatal("buffer recovery changed stored credentials or shared account")
	}
}

func TestBufferRecoveryNetworkErrorKeepsOriginalDiagnostic(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "buffer-network", time.Now().Add(time.Hour))
	want := &net.OpError{Op: "read", Net: "tcp", Err: context.DeadlineExceeded}
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) { return "", want }
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		t.Fatal("must not rotate token after transport error")
		return protocol.LoginBufferCredentials{}, nil
	}
	_, err := app.invokeWXApp(context.Background(), acc, "wx-test", nil, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, &protocol.ManualAuthRejectedError{Code: -10}
	})
	var rejected *protocol.ManualAuthRejectedError
	if !errors.Is(err, want) || !errors.As(err, &rejected) {
		t.Fatalf("lost original error: %v", err)
	}
}

func TestFailedCredentialRecoveryKeepsProtocolFailure(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "buffer-rejected", time.Now().Add(time.Hour))
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
		return "", &protocol.LoginBufferRejectedError{Code: -101, Message: "access rejected"}
	}
	refreshErr := &protocol.RefreshRejectedError{Code: -109, Message: "RC_PARAMS_INVALID"}
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		return protocol.LoginBufferCredentials{}, refreshErr
	}
	calls := 0
	_, err := app.invokeWXApp(context.Background(), acc, "wx-test", nil, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		calls++
		return nil, &protocol.ManualAuthRejectedError{Code: -10}
	})
	var rejected *protocol.ManualAuthRejectedError
	if calls != 1 || !errors.As(err, &rejected) || !errors.Is(err, refreshErr) || !strings.Contains(err.Error(), "fetch fresh login buffer") {
		t.Fatalf("calls=%d err=%v", calls, err)
	}
}

func TestExpiredRecoveryPreservesTypedCauses(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "buffer-terminal", time.Now().Add(time.Hour))
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
		return "", &protocol.LoginBufferRejectedError{Code: -101, Message: "upstream rejected access"}
	}
	refreshErr := &protocol.RefreshRejectedError{Code: -109, Message: "CheckWXToken wx refresh return error 42007 | 42007"}
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		return protocol.LoginBufferCredentials{}, refreshErr
	}
	_, err := app.invokeWXApp(context.Background(), acc, "wx-test", nil, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, &protocol.ManualAuthRejectedError{Code: -10}
	})
	var expired accountExpiredError
	var auth *protocol.ManualAuthRejectedError
	if !errors.As(err, &expired) || !errors.As(err, &auth) || !errors.Is(err, refreshErr) {
		t.Fatalf("terminal recovery lost error types: %v", err)
	}
	stored, getErr := app.db.GetAccount(context.Background(), acc.ID)
	if getErr != nil || accountStatus(stored) != "expired" || stored.Credentials != nil {
		t.Fatal("rejected credentials remained callable")
	}
}

func TestBufferRecoveryKeepsAccountProxyWhenProtocolUsesDirect(t *testing.T) {
	var connects atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodConnect {
			connects.Add(1)
		}
		http.Error(w, "test proxy unavailable", http.StatusBadGateway)
	}))
	defer proxy.Close()
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "buffer-split-proxy", time.Now().Add(time.Hour))
	policy := "direct"
	if _, err := app.db.UpsertAccountProxySetting(context.Background(), acc.ID, "static", "http", strings.Replace(proxy.URL, "http://", "http-connect://", 1), "", nil, "", "", "", 300, &policy); err != nil {
		t.Fatal(err)
	}
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
		t.Error("buffer fetch bypassed account proxy")
		return "", errors.New("unexpected direct fetch")
	}
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		t.Error("refresh bypassed account proxy")
		return protocol.LoginBufferCredentials{}, errors.New("unexpected direct refresh")
	}
	calls := 0
	_, err := app.invokeWXApp(context.Background(), acc, "wx-test", nil, func(_ context.Context, _ *store.WechatAccount, _ string, _ map[string]any, route string, fallback bool) (map[string]any, error) {
		calls++
		if route != "" || fallback {
			t.Fatal("protocol request did not use direct egress")
		}
		return nil, &protocol.ManualAuthRejectedError{Code: -10}
	})
	if err == nil || calls != 1 || connects.Load() == 0 {
		t.Fatalf("calls=%d connects=%d err=%v", calls, connects.Load(), err)
	}
}
