package httpapi

import (
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

func TestConsoleHTMLIsNotStoredAcrossUpgrades(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite", AdminUser: "cache-owner", AdminPassword: "synthetic-password"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	login := httptest.NewRecorder()
	app.Handler().ServeHTTP(login, httptest.NewRequest("POST", "/login", strings.NewReader(`{"username":"cache-owner","password":"synthetic-password"}`)))
	if len(login.Result().Cookies()) == 0 {
		t.Fatal("synthetic login failed")
	}
	for _, path := range []string{"/", "/scan", "/settings", "/proxies", "/users", "/runs", "/logs", "/login"} {
		t.Run(path, func(t *testing.T) {
			w := httptest.NewRecorder()
			r := httptest.NewRequest("GET", path, nil)
			if path != "/login" {
				for _, cookie := range login.Result().Cookies() {
					r.AddCookie(cookie)
				}
			}
			app.Handler().ServeHTTP(w, r)
			if w.Code != 200 || w.Header().Get("Cache-Control") != "no-store" {
				t.Fatal(path, w.Code, w.Header())
			}
		})
	}
	w := httptest.NewRecorder()
	serveFileOrText(w, httptest.NewRequest("GET", "/", nil), filepath.Join(t.TempDir(), "missing.html"), "fallback")
	if w.Code != 200 || w.Body.String() != "fallback" || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("fallback HTML can be cached")
	}
}
