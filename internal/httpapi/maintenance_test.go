package httpapi

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
	"yyb_go/internal/auth"
)

func TestMaintenanceRequiresAdminAndConfirmation(t *testing.T) {
	a := &App{}
	for _, tc := range []struct {
		role, header, body string
		want               int
	}{
		{"", "1", `{}`, 403}, {"user", "1", `{}`, 403},
		{"admin", "", `{}`, 403}, {"admin", "1", `{"action":"shell","confirm":true}`, 400},
		{"admin", "1", `{"action":"restart","confirm":true,"request_id":"0123456789abcdef"}`, 409},
	} {
		req := httptest.NewRequest(http.MethodPost, "/api/maintenance", strings.NewReader(tc.body))
		if tc.role != "" {
			req = req.WithContext(context.WithValue(req.Context(), authUserKey, &auth.User{Role: tc.role}))
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-YYB-Maintenance", tc.header)
		w := httptest.NewRecorder()
		a.handleMaintenance(w, req)
		if w.Code != tc.want {
			t.Errorf("role=%s: %d %s", tc.role, w.Code, w.Body.String())
		}
	}
}

func TestVersionCheckCachesAndValidates(t *testing.T) {
	for _, tc := range []struct {
		current, latest string
		want            bool
	}{{"0.2.9", "0.2.10", true}, {"0.2.10", "0.2.9", false}, {"0.2.10", "0.2.10", false}, {"dev", "0.2.10", false}} {
		if newerMaintenanceVersion(tc.current, tc.latest) != tc.want {
			t.Fatalf("bad version compare: %+v", tc)
		}
	}
	for _, body := range []string{"0.2.10\n", "<html>502</html>"} {
		calls := 0
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; _, _ = w.Write([]byte(body)) }))
		checker := &updateChecker{client: &http.Client{Timeout: time.Second}, url: srv.URL}
		latest, err := checker.check(context.Background())
		if strings.HasPrefix(body, "0.") && (err != nil || latest != "0.2.10") {
			t.Fatal(latest, err)
		}
		if strings.HasPrefix(body, "<") && err == nil {
			t.Fatal("accepted invalid version")
		}
		_, _ = checker.check(context.Background())
		if calls != 1 {
			t.Fatal("cache missed")
		}
		srv.Close()
	}
}
