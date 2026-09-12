package httpapi

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAuthenticatedConsoleAutomationRoutes(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite", AdminUser: "owner", AdminPassword: "owner-password"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	handler := app.Handler()
	for _, remote := range []string{"127.0.0.1:12345", "192.0.2.10:12345"} {
		for _, tc := range []struct {
			method string
			path   string
			status int
		}{
			{http.MethodPost, "/wxapp/getCode", http.StatusBadRequest},
			{http.MethodPost, "/wx/code", http.StatusBadRequest},
			{http.MethodGet, "/wxapp/getCode", http.StatusMethodNotAllowed},
			{http.MethodGet, "/wx/code", http.StatusMethodNotAllowed},
			{http.MethodPost, "/wxapp/getcode", http.StatusNotFound},
			{http.MethodPost, "/api/wxapp/getCode", http.StatusNotFound},
			{http.MethodGet, "/accounts", http.StatusUnauthorized},
			{http.MethodPost, "/accounts/proxy", http.StatusUnauthorized},
			{http.MethodGet, "/api/auth/me", http.StatusUnauthorized},
		} {
			t.Run(remote+tc.method+tc.path, func(t *testing.T) {
				req := httptest.NewRequest(tc.method, tc.path, strings.NewReader("{}"))
				req.Header.Set("Content-Type", "application/json")
				req.RemoteAddr = remote
				response := httptest.NewRecorder()
				handler.ServeHTTP(response, req)
				if response.Code != tc.status || response.Header().Get("Location") != "" || !json.Valid(response.Body.Bytes()) {
					t.Fatalf("status=%d location=%q body=%s", response.Code, response.Header().Get("Location"), response.Body.String())
				}
			})
		}
	}
	for _, path := range []string{"/", "/settings", "/proxies", "/scan"} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		if response.Code != http.StatusSeeOther || !strings.HasPrefix(response.Header().Get("Location"), "/login?next=") {
			t.Fatalf("page %s no longer requires login: %d", path, response.Code)
		}
	}
}
