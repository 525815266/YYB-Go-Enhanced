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
