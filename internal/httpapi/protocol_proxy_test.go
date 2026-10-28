package httpapi

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestProtocolProxyPolicySaveAndLegacyUpdate(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "policy-save", time.Now().Add(time.Hour))
	body := map[string]any{"ref": fmt.Sprint(acc.ID), "mode": "static", "static_proxy": "socks5://127.0.0.1:1080", "protocol_proxy_mode": "direct"}
	handler := app.Handler()
	for _, policy := range []any{"direct", nil, "account"} {
		if policy == nil {
			delete(body, "protocol_proxy_mode")
		} else {
			body["protocol_proxy_mode"] = policy
		}
		response := apiRequest(t, handler, http.MethodPut, "/accounts/proxy", body)
		if response.Code != http.StatusOK {
			t.Fatalf("save policy %v: %d %s", policy, response.Code, response.Body.String())
		}
		want := "direct"
		if policy == "account" {
			want = "account"
		}
		got := apiRequest(t, handler, http.MethodGet, "/accounts/proxy?ref="+fmt.Sprint(acc.ID), nil)
		if got.Code != http.StatusOK || !strings.Contains(got.Body.String(), `"protocol_proxy_mode":"`+want+`"`) {
			t.Fatalf("saved policy %v: %s", policy, got.Body.String())
		}
	}
	for _, invalid := range []any{"auto", "", true} {
		body["protocol_proxy_mode"] = invalid
		response := apiRequest(t, handler, http.MethodPut, "/accounts/proxy", body)
		if response.Code != http.StatusBadRequest {
			t.Fatalf("invalid policy %v accepted: %s", invalid, response.Body.String())
		}
	}
}

func TestProtocolDirectSkipsBrokenDynamicProxy(t *testing.T) {
	var extracts atomic.Int32
	proxyAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		extracts.Add(1)
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer proxyAPI.Close()
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "direct-call", time.Now().Add(time.Hour))
	for _, policy := range []string{"account", "direct"} {
		if _, err := app.db.UpsertAccountProxySetting(context.Background(), acc.ID, "api", "http", "", proxyAPI.URL, nil, "", "", "", 300, &policy); err != nil {
			t.Fatal(err)
		}
		calls := 0
		_, err := app.invokeWXApp(context.Background(), acc, "wx-test", nil, func(_ context.Context, _ *store.WechatAccount, _ string, _ map[string]any, proxy string, fallback bool) (map[string]any, error) {
			calls++
			if proxy != "" || fallback {
				t.Fatalf("explicit direct call: proxy=%q fallback=%v", proxy, fallback)
			}
			return map[string]any{"code": "test"}, nil
		})
		if policy == "account" && (err == nil || calls != 0) {
			t.Fatalf("default must stop at proxy failure: calls=%d err=%v", calls, err)
		}
		if policy == "direct" && (err != nil || calls != 1) {
			t.Fatalf("direct must bypass extraction: calls=%d err=%v", calls, err)
		}
	}
	if extracts.Load() != 1 {
		t.Fatalf("proxy extraction calls=%d, want only default-mode call", extracts.Load())
	}
}

func TestSplitProxyKeepsCredentialRefreshOnAccountProxy(t *testing.T) {
	for _, action := range []string{"protocol-recovery", "manual-refresh", "scheduled-refresh"} {
		t.Run(action, func(t *testing.T) {
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
			acc := insertKeepAliveTestAccount(t, app, "refresh-"+action, time.Now().Add(time.Minute))
			policy := "direct"
			if _, err := app.db.UpsertAccountProxySetting(context.Background(), acc.ID, "static", "http", strings.Replace(proxy.URL, "http://", "http-connect://", 1), "", nil, "", "", "", 300, &policy); err != nil {
				t.Fatal(err)
			}
			app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
				t.Error("credential refresh unexpectedly used direct egress")
				return protocol.LoginBufferCredentials{}, errors.New("unexpected direct refresh")
			}
			if action == "protocol-recovery" {
				calls := 0
				_, err := app.invokeWXApp(context.Background(), acc, "wx-test", nil, func(_ context.Context, _ *store.WechatAccount, _ string, _ map[string]any, route string, fallback bool) (map[string]any, error) {
					calls++
					if route != "" || fallback {
						t.Errorf("protocol call did not use direct egress")
					}
					return nil, errors.New("test protocol login rejection")
				})
				if err == nil || calls != 1 || !strings.Contains(err.Error(), "refresh account credentials") {
					t.Fatalf("failed refresh must not replay: calls=%d err=%v", calls, err)
				}
			} else if action == "manual-refresh" {
				if _, err := app.refreshLiveness(context.Background(), acc); err == nil {
					t.Fatal("expected proxy failure")
				}
			} else {
				app.refreshDueAccounts(context.Background())
			}
			if connects.Load() == 0 {
				t.Fatal("refresh did not connect to the configured account proxy")
			}
			fresh, err := app.db.GetAccount(context.Background(), acc.ID)
			if err != nil || accountStatus(fresh) != "alive" {
				t.Fatalf("proxy failure expired valid credentials: %v", err)
			}
		})
	}
}

func TestProtocolDefaultUsesAccountProxy(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "default-proxy", time.Now().Add(time.Hour))
	const route = "socks5://127.0.0.1:1080"
	if _, err := app.db.UpsertAccountProxySetting(context.Background(), acc.ID, "static", "socks5", route, "", nil, "", "", "", 300, nil); err != nil {
		t.Fatal(err)
	}
	_, err := app.invokeWXApp(context.Background(), acc, "wx-test", nil, func(_ context.Context, _ *store.WechatAccount, _ string, _ map[string]any, proxy string, fallback bool) (map[string]any, error) {
		if proxy != route || fallback {
			t.Fatalf("default proxy=%q fallback=%v", proxy, fallback)
		}
		return map[string]any{"code": "test"}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
