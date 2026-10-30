package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/eventlog"
)

func TestSystemLogsRequireAdminAndValidateFilter(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite", AdminUser: "owner", AdminPassword: "owner-password"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	handler := app.Handler()
	for _, path := range []string{"/api/system-logs", "/logs"} {
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, httptest.NewRequest("GET", path, nil))
		want := http.StatusUnauthorized
		if path == "/logs" {
			want = http.StatusSeeOther
		}
		if out.Code != want {
			t.Fatalf("anonymous %s status=%d", path, out.Code)
		}
	}
	login := httptest.NewRecorder()
	handler.ServeHTTP(login, httptest.NewRequest("POST", "/login", strings.NewReader(`{"username":"owner","password":"owner-password"}`)))
	if len(login.Result().Cookies()) == 0 {
		t.Fatalf("login failed: %d", login.Code)
	}
	request := func(path string) *httptest.ResponseRecorder {
		req := httptest.NewRequest("GET", path, nil)
		for _, cookie := range login.Result().Cookies() {
			req.AddCookie(cookie)
		}
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		return out
	}
	app.events.Emit("protocol.failed", eventlog.Fields{AccountID: 1, Channel: "pcmgr", Rejections: []eventlog.Rejection{{Stage: "pcmgr_login_buffer", Code: 1}}})
	app.events.Close() // Flush the asynchronous queue before inspecting the endpoint.
	for _, path := range []string{"/api/system-logs?category=protocol&level=error&account_id=1", "/logs"} {
		out := request(path)
		if out.Code != 200 {
			t.Fatalf("admin %s status=%d", path, out.Code)
		}
	}
	for _, query := range []string{"level=secret", "category=secret", "channel=secret", "before=-1", "since=abc", "account_id=1%20OR%201=1", "limit=101"} {
		out := request("/api/system-logs?" + query)
		if out.Code != 400 {
			t.Fatalf("invalid query accepted: %s", query)
		}
	}
	out := request("/api/system-logs?category=protocol&limit=1")
	var body struct {
		Data struct {
			Entries []struct {
				AccountID int64 `json:"account_id"`
			} `json:"entries"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &body); err != nil || len(body.Data.Entries) != 1 || body.Data.Entries[0].AccountID != 1 {
		t.Fatal("missing diagnostic record", err)
	}
	if out.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("logs may be cached")
	}
	member, err := app.auth.RegisterUser(context.Background(), "member", "Member", "member-password")
	if err != nil {
		t.Fatal(err)
	}
	token, _, err := app.auth.CreateSession(context.Background(), member.ID, "test", "127.0.0.1", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/api/system-logs", "/logs"} {
		req := httptest.NewRequest("GET", path, nil)
		req.AddCookie(&http.Cookie{Name: sessionCookie, Value: token})
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		if out.Code != 403 {
			t.Fatalf("nonadmin %s status=%d", path, out.Code)
		}
	}
}
