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

func TestPublicOAuthReturnsAuthorizationURL(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second, QRSessionTTL: time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	status := "alive"
	account, err := app.db.UpsertAccount(context.Background(), "openid-oauth-test", "login-buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}

	body := []byte(`{"ref":"` + strconv.FormatInt(account.ID, 10) + `","appid":"wx1234567890abcdef","redirect_uri":"https://example.com/callback","scope":"snsapi_base"}`)
	recorder := httptest.NewRecorder()
	app.Handler().ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/wx/oauth", bytes.NewReader(body)))
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
	var response struct {
		Code int `json:"code"`
		Data struct {
			FullURL string  `json:"full_url"`
			State   string  `json:"state"`
			Code    *string `json:"code"`
			NextStep string `json:"next_step"`
		} `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Code != 0 || response.Data.Code != nil || response.Data.State == "" || !strings.Contains(response.Data.FullURL, "#wechat_redirect") || !strings.Contains(response.Data.NextStep, "code=null") {
		t.Fatalf("unexpected OAuth response: %#v", response)
	}
}
