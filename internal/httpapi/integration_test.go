package httpapi

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
