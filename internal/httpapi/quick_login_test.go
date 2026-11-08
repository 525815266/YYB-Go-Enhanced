package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/qr"
)

func TestParseQuickAuthorizeRedirect(t *testing.T) {
	tests := []struct {
		name    string
		raw     string
		want    string
		wantErr bool
	}{
		{
			name: "valid",
			raw:  "https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=WX&code=desktop-code&state=web",
			want: "desktop-code",
		},
		{
			name:    "wrong host",
			raw:     "https://example.com/pc_yyb/pcyyb_oauth?login_type=WX&code=desktop-code&state=web",
			wantErr: true,
		},
		{
			name:    "wrong state",
			raw:     "https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=WX&code=desktop-code&state=other",
			wantErr: true,
		},
		{
			name:    "wrong scheme",
			raw:     "http://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=WX&code=desktop-code&state=web",
			wantErr: true,
		},
		{
			name:    "unexpected port",
			raw:     "https://yybadaccess.3g.qq.com:443/pc_yyb/pcyyb_oauth?login_type=WX&code=desktop-code&state=web",
			wantErr: true,
		},
		{
			name:    "wrong login type",
			raw:     "https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=QQ&code=desktop-code&state=web",
			wantErr: true,
		},
		{
			name:    "missing code",
			raw:     "https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=WX&state=web",
			wantErr: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := parseQuickAuthorizeRedirect(tt.raw)
			if (err != nil) != tt.wantErr {
				t.Fatalf("parseQuickAuthorizeRedirect() error = %v, wantErr %v", err, tt.wantErr)
			}
			if got != tt.want {
				t.Fatalf("parseQuickAuthorizeRedirect() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestQuickLoginCreatesAccountAndRejectsReplay(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{
		ResourceRoot:   t.TempDir(),
		RequestTimeout: time.Second,
		AvatarTimeout:  time.Second,
		SessionTTL:     time.Minute,
		QRSessionTTL:   time.Minute,
		EnablePCLogin:  true,
	})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()

	var exchangedCode string
	app.exchangeAuthCode = func(_ context.Context, code string) (protocol.LoginBufferResult, error) {
		exchangedCode = code
		return protocol.LoginBufferResult{
			LoginBuffer: "desktop-login-buffer",
			Credentials: protocol.LoginBufferCredentials{
				OpenID:       "desktop-openid",
				AccessToken:  "access-token",
				RefreshToken: "refresh-token",
				LoginType:    "WX",
				Nickname:     "Desktop WeChat",
				ExpiresAt:    time.Now().Add(time.Hour).Unix(),
				ExpiresIn:    3600,
			},
		}, nil
	}
	app.fetchUserInfo = func(_ context.Context, _ protocol.LoginBufferCredentials) (map[string]any, error) {
		return map[string]any{"nick_name": "Desktop WeChat"}, nil
	}
	handler := app.Handler()

	create := httptest.NewRecorder()
	handler.ServeHTTP(create, httptest.NewRequest(http.MethodPost, "/quick-login", nil))
	if create.Code != http.StatusOK {
		t.Fatalf("POST /quick-login status = %d, body = %s", create.Code, create.Body.String())
	}
	var created struct {
		Code int `json:"code"`
		Data struct {
			SessionID   string `json:"session_id"`
			AppID       string `json:"appid"`
			RedirectURI string `json:"redirect_uri"`
		} `json:"data"`
	}
	if err := json.Unmarshal(create.Body.Bytes(), &created); err != nil {
		t.Fatalf("decode create response: %v", err)
	}
	if created.Code != 0 || created.Data.SessionID == "" || created.Data.AppID != qr.AppID || created.Data.RedirectURI != qr.OAuthRedirectURI {
		t.Fatalf("POST /quick-login body = %#v", created)
	}

	body := []byte(`{"redirect_url":"https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=WX&code=desktop-code&state=web"}`)
	confirmPath := "/quick-login/" + created.Data.SessionID + "/confirm"
	confirm := httptest.NewRecorder()
	handler.ServeHTTP(confirm, httptest.NewRequest(http.MethodPost, confirmPath, bytes.NewReader(body)))
	if confirm.Code != http.StatusOK {
		t.Fatalf("POST %s status = %d, body = %s", confirmPath, confirm.Code, confirm.Body.String())
	}
	if exchangedCode != "desktop-code" {
		t.Fatalf("exchanged code = %q", exchangedCode)
	}
	var confirmed struct {
		Code int `json:"code"`
		Data struct {
			OpenID string `json:"openid"`
			Status string `json:"status"`
		} `json:"data"`
	}
	if err := json.Unmarshal(confirm.Body.Bytes(), &confirmed); err != nil {
		t.Fatalf("decode confirm response: %v", err)
	}
	if confirmed.Code != 0 || confirmed.Data.OpenID != "desktop-openid" || confirmed.Data.Status != "alive" {
		t.Fatalf("POST %s body = %#v", confirmPath, confirmed)
	}

	replay := httptest.NewRecorder()
	handler.ServeHTTP(replay, httptest.NewRequest(http.MethodPost, confirmPath, bytes.NewReader(body)))
	if replay.Code != http.StatusNotFound {
		t.Fatalf("replayed POST %s status = %d, want %d", confirmPath, replay.Code, http.StatusNotFound)
	}
}

func TestQuickLoginDisabledByDefault(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir()})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()

	response := httptest.NewRecorder()
	app.Handler().ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/quick-login", nil))
	if response.Code != http.StatusNotFound {
		t.Fatalf("POST /quick-login status = %d, want %d; body=%s", response.Code, http.StatusNotFound, response.Body.String())
	}
}

func TestQQQuickCallbackBindsChannelAndSessionState(t *testing.T) {
	const base = "https://go.browser.qq.com/wx-login.html?code=synthetic&state=per-session-state"
	if got, err := parseQuickAuthorizeRedirectForProduct(base, "qbbrowser", "per-session-state"); err != nil || got != "synthetic" {
		t.Fatalf("QQ callback rejected: %v", err)
	}
	for _, raw := range []string{
		base + "&state=other", base + "&code=another", base + "#fragment",
		"https://go.browser.qq.com:443/wx-login.html?code=synthetic&state=per-session-state",
		"https://go.browser.qq.com@evil.example/wx-login.html?code=synthetic&state=per-session-state",
		"https://go.browser.qq.com/wx-login.html?code=synthetic&state=web",
		"https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?code=synthetic&state=per-session-state&login_type=WX",
	} {
		if _, err := parseQuickAuthorizeRedirectForProduct(raw, "qbbrowser", "per-session-state"); err == nil {
			t.Fatal("unbound QQ callback accepted")
		}
	}
	if _, err := parseQuickAuthorizeRedirect(base); err == nil {
		t.Fatal("QQ callback accepted by appstore")
	}
	if _, err := parseQuickAuthorizeRedirectForProduct(base, "qbbrowser", ""); err == nil {
		t.Fatal("missing expected state accepted")
	}
}

func TestQQQuickSessionUsesRuntimeScopeAndRejectsWrongState(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), EnablePCLogin: true, QRSessionTTL: time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	w := httptest.NewRecorder()
	app.handleQuickLoginRoot(w, httptest.NewRequest("POST", "/quick-login", bytes.NewBufferString(`{"product":"qbbrowser","proxy_mode":"direct"}`)))
	var payload struct {
		Data struct {
			SessionID string `json:"session_id"`
			AppID     string `json:"appid"`
			Scope     string `json:"scope"`
			State     string `json:"state"`
		} `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &payload); err != nil || w.Code != 200 {
		t.Fatal("QQ quick session create failed")
	}
	c := payload.Data
	if c.AppID != "wx0dbc1ae97f156c38" || c.Scope != "snsapi_login,snsapi_runtime_pcsdk" || c.State == "" || c.State == "web" {
		t.Fatal("QQ quick authorization used wrong app or permissions")
	}
	w = httptest.NewRecorder()
	app.handleQuickLogin(w, httptest.NewRequest("POST", "/quick-login/"+c.SessionID+"/confirm", bytes.NewBufferString(`{"redirect_url":"https://go.browser.qq.com/wx-login.html?code=synthetic&state=wrong"}`)))
	if w.Code != 400 {
		t.Fatal("wrong quick state not rejected before exchange")
	}
	session, ok := app.takeQuickSession(c.SessionID)
	if !ok || session.Product != "qbbrowser" || session.State != c.State {
		t.Fatal("invalid callback consumed or changed the session")
	}
	if _, ok := app.takeQuickSession(c.SessionID); ok {
		t.Fatal("QQ quick session consumed twice")
	}
}
