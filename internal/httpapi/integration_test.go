package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"
)

func TestIntegrationManifestAndAuthorization(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), IntegrationToken: "secret-token", RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	handler := app.Handler()

	manifest := httptest.NewRecorder()
	handler.ServeHTTP(manifest, httptest.NewRequest(http.MethodGet, "/integration/module-manifest.json", nil))
	if manifest.Code != http.StatusOK {
		t.Fatalf("manifest status = %d", manifest.Code)
	}
	var payload map[string]any
	if err := json.Unmarshal(manifest.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode manifest: %v", err)
	}
	if payload["id"] != "yyb" || payload["schema_version"] != float64(1) {
		t.Fatalf("manifest = %#v", payload)
	}

	unauthorized := httptest.NewRecorder()
	handler.ServeHTTP(unauthorized, httptest.NewRequest(http.MethodGet, "/integration/accounts", nil))
	if unauthorized.Code != http.StatusUnauthorized {
		t.Fatalf("accounts without token status = %d", unauthorized.Code)
	}

	authorized := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/integration/accounts", nil)
	request.Header.Set("Authorization", "Bearer secret-token")
	handler.ServeHTTP(authorized, request)
	if authorized.Code != http.StatusOK {
		t.Fatalf("accounts with token status = %d, body = %s", authorized.Code, authorized.Body.String())
	}
}

func TestIntegrationAccountProxyRequiresTokenAndResolvesSetting(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), IntegrationToken: "secret-token", RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	status := "alive"
	account, err := app.db.UpsertAccount(context.Background(), "proxy-integration-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	if _, err = app.db.UpsertAccountProxySetting(context.Background(), account.ID, "static", "http", "http-connect://proxy-user:proxy-pass@203.0.113.8:8080", "", nil, "", "", "", 300); err != nil {
		t.Fatalf("UpsertAccountProxySetting() error = %v", err)
	}

	handler := app.Handler()
	unauthorized := httptest.NewRecorder()
	handler.ServeHTTP(unauthorized, httptest.NewRequest(http.MethodGet, "/integration/accounts/proxy?ref=1", nil))
	if unauthorized.Code != http.StatusUnauthorized {
		t.Fatalf("proxy without token status = %d", unauthorized.Code)
	}

	authorized := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/integration/accounts/proxy?ref=1", nil)
	request.Header.Set("Authorization", "Bearer secret-token")
	handler.ServeHTTP(authorized, request)
	if authorized.Code != http.StatusOK {
		t.Fatalf("proxy with token status = %d, body = %s", authorized.Code, authorized.Body.String())
	}
	var payload map[string]any
	if err = json.Unmarshal(authorized.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode proxy response: %v", err)
	}
	data, ok := payload["data"].(map[string]any)
	if !ok || data["proxy"] != "http-connect://proxy-user:proxy-pass@203.0.113.8:8080" || data["masked"] != "http-connect://203.0.113.8:8080" {
		t.Fatalf("proxy response = %#v", payload)
	}

	directAccount, err := app.db.UpsertAccount(context.Background(), "direct-integration-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount(direct) error = %v", err)
	}
	badFallback := httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodGet, "/integration/accounts/proxy?ref="+strconv.FormatInt(directAccount.ID, 10)+"&fallback_profile_id=invalid", nil)
	request.Header.Set("Authorization", "Bearer secret-token")
	handler.ServeHTTP(badFallback, request)
	if badFallback.Code != http.StatusBadRequest {
		t.Fatalf("invalid fallback profile status = %d, body = %s", badFallback.Code, badFallback.Body.String())
	}
}
