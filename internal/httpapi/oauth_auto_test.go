package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

// The field H5 convention (proxy-login build): one POST with appid+
// redirect_uri returns a real authorization code, routed through the same
// invoke as /wx/h5code so every gate applies.
func TestPublicOAuthAutoExchangeReturnsCode(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "oauth-auto", time.Now().Add(time.Hour))
	seen := ""
	app.oauthH5Invoke = func(_ context.Context, account *store.WechatAccount, appID string, payload map[string]any, _ string, _ bool) (map[string]any, error) {
		seen = fmt.Sprintf("%s|%v|%v", appID, payload["redirect_uri"], payload["scope"])
		return map[string]any{
			"code": "0pCODE123", "state": "st-auto", "scope": "snsapi_base",
			"redirect_uri": "https://biz.example/cb", "mode": "h5_oauth_auto",
		}, nil
	}
	out := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/wx/oauth",
		strings.NewReader(fmt.Sprintf(`{"ref":"%d","appid":"wxabec74192b99a2d6","redirect_uri":"https://biz.example/cb"}`, acc.ID)))
	app.handlePublicOAuth(out, request)
	if out.Code != http.StatusOK {
		t.Fatalf("HTTP %d: %s", out.Code, out.Body.String())
	}
	var body struct {
		Data struct {
			Code    any            `json:"code"`
			Mode    string         `json:"mode"`
			Scope   any            `json:"scope"`
			State   any            `json:"state"`
			URL     string         `json:"url"`
			Account map[string]any `json:"account"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Data.Code != "0pCODE123" || body.Data.Mode != "authorization_code" {
		t.Fatalf("unexpected auto response: %s", out.Body.String())
	}
	if body.Data.Scope != "snsapi_base" || body.Data.State != "st-auto" {
		t.Fatalf("scope/state not mapped: %s", out.Body.String())
	}
	if !strings.Contains(body.Data.URL, "biz.example%2Fcb") {
		t.Fatalf("authorize URL missing redirect: %s", out.Body.String())
	}
	if body.Data.Account == nil || body.Data.Account["id"] == nil {
		t.Fatalf("account object missing: %s", out.Body.String())
	}
	if seen != "wxabec74192b99a2d6|https://biz.example/cb|<nil>" {
		// scope was empty in the request, so nothing should be forwarded.
		t.Fatalf("unexpected invoke params: %q", seen)
	}
}

func TestPublicOAuthAutoExchangePassesBusinessRejectionThrough(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "oauth-reject", time.Now().Add(time.Hour))
	refreshes := 0
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		refreshes++
		return protocol.LoginBufferCredentials{}, nil
	}
	app.oauthH5Invoke = func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, &protocol.ErrH5OAuthBusinessRejected{Ret: 10003}
	}
	out := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/wx/oauth",
		strings.NewReader(fmt.Sprintf(`{"ref":"%d","appid":"wxabec74192b99a2d6","redirect_uri":"https://biz.example/cb"}`, acc.ID)))
	app.handlePublicOAuth(out, request)
	if out.Code != http.StatusBadGateway {
		t.Fatalf("HTTP %d, want 502 passthrough: %s", out.Code, out.Body.String())
	}
	if !strings.Contains(out.Body.String(), "10003") {
		t.Fatalf("business rejection detail lost: %s", out.Body.String())
	}
	if refreshes != 0 {
		t.Fatal("h5 rejection must not trigger credential refresh")
	}
}

func TestPublicOAuthReturnURLOnlyKeepsLegacyShape(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "oauth-legacy", time.Now().Add(time.Hour))
	out := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/wx/oauth",
		strings.NewReader(fmt.Sprintf(`{"ref":"%d","appid":"wxabec74192b99a2d6","redirect_uri":"https://biz.example/cb","return_url_only":true}`, acc.ID)))
	app.handlePublicOAuth(out, request)
	if out.Code != http.StatusOK {
		t.Fatalf("HTTP %d: %s", out.Code, out.Body.String())
	}
	var body struct {
		Data struct {
			Code any    `json:"code"`
			Mode string `json:"mode"`
			URL  string `json:"url"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Data.Mode != "authorization_url" || body.Data.Code != nil {
		t.Fatalf("legacy URL mode changed: %s", out.Body.String())
	}
	if !strings.Contains(body.Data.URL, "open.weixin.qq.com") {
		t.Fatalf("unexpected URL: %s", body.Data.URL)
	}
}

func TestPublicOAuthAutoRequiresRedirectURI(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "oauth-noredirect", time.Now().Add(time.Hour))
	out := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/wx/oauth",
		strings.NewReader(fmt.Sprintf(`{"ref":"%d","appid":"wxabec74192b99a2d6"}`, acc.ID)))
	app.handlePublicOAuth(out, request)
	if out.Code != http.StatusBadRequest {
		t.Fatalf("HTTP %d, want 400: %s", out.Code, out.Body.String())
	}
	if !strings.Contains(out.Body.String(), "return_url_only") {
		t.Fatalf("error should point at the legacy flag: %s", out.Body.String())
	}
}
