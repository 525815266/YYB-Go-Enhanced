package httpapi

import (
	"context"
	"encoding/base64"
	"encoding/json"
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

func TestVersionCheckFallsBackToGitHubContents(t *testing.T) {
	failedCalls, fallbackCalls := 0, 0
	primarySeen := make(chan struct{})
	failed := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		failedCalls++
		close(primarySeen)
		http.Error(w, "upstream unavailable", http.StatusBadGateway)
	}))
	defer failed.Close()
	fallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-primarySeen
		fallbackCalls++
		_ = json.NewEncoder(w).Encode(map[string]string{
			"content":  base64.StdEncoding.EncodeToString([]byte("0.2.13\n")),
			"encoding": "base64",
		})
	}))
	defer fallback.Close()

	checker := &updateChecker{client: &http.Client{Timeout: time.Second}, url: failed.URL, fallbackURL: fallback.URL}
	latest, err := checker.check(context.Background())
	if err != nil || latest != "0.2.13" {
		t.Fatalf("fallback failed: latest=%q err=%v", latest, err)
	}
	_, _ = checker.check(context.Background())
	if failedCalls != 1 || fallbackCalls != 1 {
		t.Fatalf("version result was not cached: primary=%d fallback=%d", failedCalls, fallbackCalls)
	}
}

func TestDescribeMaintenanceRuntime(t *testing.T) {
	tests := []struct {
		name            string
		goos, goarch    string
		docker, managed bool
		kind, asset     string
		downloadable    bool
	}{
		{name: "windows amd64", goos: "windows", goarch: "amd64", kind: "windows", asset: "yyb-go-windows-amd64.exe", downloadable: true},
		{name: "windows arm64", goos: "windows", goarch: "arm64", kind: "windows", asset: "yyb-go-windows-arm64.exe", downloadable: true},
		{name: "linux armv7", goos: "linux", goarch: "arm", kind: "linux", asset: "yyb-go-linux-armv7", downloadable: true},
		{name: "darwin arm64", goos: "darwin", goarch: "arm64", kind: "darwin", asset: "yyb-go-darwin-arm64", downloadable: true},
		{name: "magisk", goos: "android", goarch: "arm64", kind: "magisk", asset: "yyb-go-magisk-arm64-0.2.17.zip", downloadable: true},
		{name: "docker executor", goos: "linux", goarch: "amd64", docker: true, managed: true, kind: "docker"},
		{name: "docker without executor", goos: "linux", goarch: "amd64", docker: true, kind: "docker"},
		{name: "unsupported", goos: "freebsd", goarch: "amd64", kind: "freebsd"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := describeMaintenanceRuntime(tt.goos, tt.goarch, "0.2.17", tt.docker, tt.managed)
			if got.Kind != tt.kind || got.ManagedUpdate != tt.managed || got.DownloadAvailable != tt.downloadable || got.DownloadName != tt.asset {
				t.Fatalf("runtime = %#v", got)
			}
			if got.DownloadAvailable && (!strings.Contains(got.DownloadURL, "/v0.2.17/") || !strings.HasSuffix(got.DownloadURL, tt.asset)) {
				t.Fatalf("download URL = %q", got.DownloadURL)
			}
		})
	}
}
