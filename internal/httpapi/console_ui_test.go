package httpapi

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

func TestConsolePagesShareAdminTheme(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{
		ResourceRoot:  filepath.Join("..", "..", "resource"),
		AuthDriver:    "sqlite",
		AuthDSN:       filepath.Join(t.TempDir(), "auth.db"),
		AdminUser:     "uiadmin",
		AdminPassword: "ui-test-password",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	handler := app.Handler()
	login := httptest.NewRecorder()
	loginRequest := httptest.NewRequest(http.MethodPost, "/login", strings.NewReader(`{"username":"uiadmin","password":"ui-test-password"}`))
	loginRequest.Header.Set("Content-Type", "application/json")
	handler.ServeHTTP(login, loginRequest)
	if login.Code != http.StatusOK {
		t.Fatalf("test login status = %d", login.Code)
	}
	for _, path := range []string{"/", "/scan", "/runs", "/proxies", "/users", "/settings", "/account-links", "/maintenance"} {
		t.Run(path, func(t *testing.T) {
			response := httptest.NewRecorder()
			request := httptest.NewRequest(http.MethodGet, path, nil)
			for _, cookie := range login.Result().Cookies() {
				request.AddCookie(cookie)
			}
			handler.ServeHTTP(response, request)
			if response.Code != http.StatusOK {
				t.Fatalf("page status = %d", response.Code)
			}
			html := response.Body.String()
			if !strings.Contains(html, "/static/css/console.css") || !strings.Contains(html, "/static/js/platform.js") {
				t.Fatal("shared console assets missing")
			}
		})
	}
	for _, path := range []string{"/static/css/console.css", "/static/css/platform.css", "/static/js/platform.js"} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		if response.Code != http.StatusOK || response.Body.Len() == 0 {
			t.Fatalf("asset %s status = %d", path, response.Code)
		}
	}
}
