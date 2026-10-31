package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

// The default request must keep returning a browser authorization link with a
// null code. Only an explicit mode asks for a real code, so existing callers
// that never send mode keep their previous behaviour.
func TestPublicOAuthModeDispatch(t *testing.T) {
	for _, mode := range []string{"", "url", "link"} {
		t.Run("mode="+mode, func(t *testing.T) {
			t.Setenv("GIN_MODE", "test")
			app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second, QRSessionTTL: time.Minute})
			if err != nil {
				t.Fatal(err)
			}
			defer app.Close()
			status := "alive"
			account, err := app.db.UpsertAccount(context.Background(), "openid-oauth-mode-"+mode, "login-buffer", nil, nil, nil, nil, nil, &status)
			if err != nil {
				t.Fatal(err)
			}

			body := []byte(`{"ref":"` + strconv.FormatInt(account.ID, 10) + `","appid":"wx1234567890abcdef","redirect_uri":"https://example.com/callback","scope":"snsapi_base","mode":"` + mode + `"}`)
			recorder := httptest.NewRecorder()
			app.Handler().ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/wx/oauth", bytes.NewReader(body)))
			if recorder.Code != http.StatusOK {
				t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
			}
			var response struct {
				Code int `json:"code"`
				Data struct {
					Mode string  `json:"mode"`
					Code *string `json:"code"`
				} `json:"data"`
			}
			if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if response.Code != 0 || response.Data.Mode != "authorization_url" || response.Data.Code != nil {
				t.Fatalf("unexpected OAuth response: %#v", response)
			}
		})
	}
}

// An explicit mode must reach the real authorization handler. The account has
// no usable session here, so the request must fail as a gateway error rather
// than fall back to the authorization link.
func TestPublicOAuthCodeModeReachesAuthorization(t *testing.T) {
	for _, mode := range []string{"code", "authorize", "auto"} {
		t.Run("mode="+mode, func(t *testing.T) {
			t.Setenv("GIN_MODE", "test")
			app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second, QRSessionTTL: time.Minute})
			if err != nil {
				t.Fatal(err)
			}
			defer app.Close()
			status := "alive"
			account, err := app.db.UpsertAccount(context.Background(), "openid-oauth-code-"+mode, "login-buffer", nil, nil, nil, nil, nil, &status)
			if err != nil {
				t.Fatal(err)
			}

			body := []byte(`{"ref":"` + strconv.FormatInt(account.ID, 10) + `","appid":"wx1234567890abcdef","redirect_uri":"https://example.com/callback","scope":"snsapi_base","mode":"` + mode + `"}`)
			recorder := httptest.NewRecorder()
			app.Handler().ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/wx/oauth", bytes.NewReader(body)))
			if recorder.Code == http.StatusOK {
				var response struct {
					Data struct {
						Mode string `json:"mode"`
					} `json:"data"`
				}
				if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
					t.Fatal(err)
				}
				if response.Data.Mode != "authorize" {
					t.Fatalf("mode = %q, want authorize", response.Data.Mode)
				}
				return
			}
			// A synthetic account cannot complete a real authorization, so a
			// gateway error is the expected outcome.
			if recorder.Code != http.StatusBadGateway && recorder.Code != http.StatusGatewayTimeout && recorder.Code != http.StatusConflict {
				t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
			}
		})
	}
}

// A code mode request without a redirect_uri is rejected before any session
// work happens.
func TestPublicOAuthCodeModeRequiresRedirectURI(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second, QRSessionTTL: time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	status := "alive"
	account, err := app.db.UpsertAccount(context.Background(), "openid-oauth-redirect", "login-buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}

	body := []byte(`{"ref":"` + strconv.FormatInt(account.ID, 10) + `","appid":"wx1234567890abcdef","mode":"code"}`)
	recorder := httptest.NewRecorder()
	app.Handler().ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/wx/oauth", bytes.NewReader(body)))
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
	if !strings.Contains(recorder.Body.String(), "redirect_uri") {
		t.Fatalf("body = %s, want a redirect_uri error", recorder.Body.String())
	}
}
