package httpapi

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"
	"yyb_go/internal/auth"
)

func TestUpdateCustomSourceAndOfficialFallback(t *testing.T) {
	for _, body := range []string{"0.2.24\n", "<html>502</html>", "0.2.24" + strings.Repeat(" ", 4096)} {
		t.Run(body[:6], func(t *testing.T) {
			var customCalls, officialCalls atomic.Int32
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/mirror" {
					customCalls.Add(1)
					_, _ = io.WriteString(w, body)
				} else {
					officialCalls.Add(1)
					_, _ = io.WriteString(w, "0.2.25")
				}
			}))
			defer srv.Close()
			checker := newUpdateChecker("", srv.URL+"/mirror")
			defer checker.client.CloseIdleConnections()
			checker.url, checker.fallbackURL, checker.releaseURL = srv.URL+"/official", "", ""
			want, wantOfficial := "0.2.25", int32(1)
			if body == "0.2.24\n" {
				want, wantOfficial = "0.2.24", 0
			}
			for i := 0; i < 2; i++ {
				if latest, err := checker.check(context.Background()); err != nil || latest != want {
					t.Fatalf("latest=%q err=%v", latest, err)
				}
			}
			if customCalls.Load() != 1 || officialCalls.Load() != wantOfficial {
				t.Fatalf("custom=%d official=%d", customCalls.Load(), officialCalls.Load())
			}
		})
	}
}

func TestUpdateConfigurationErrorsDoNotExposeCredentials(t *testing.T) {
	for _, tc := range []struct{ proxy, source, field string }{
		{"http://user:secret@proxy.invalid/path", "", "YYB_UPDATE_PROXY"},
		{"file:///secret", "", "YYB_UPDATE_PROXY"},
		{"", "https://user:secret@mirror.invalid/VERSION", "YYB_UPDATE_VERSION_URL"},
		{"", "file:///secret", "YYB_UPDATE_VERSION_URL"},
		{"", "https:///secret", "YYB_UPDATE_VERSION_URL"},
	} {
		checker := newUpdateChecker(tc.proxy, tc.source)
		_, err := checker.check(context.Background())
		if err == nil || !strings.Contains(err.Error(), tc.field) || strings.Contains(err.Error(), "secret") {
			t.Fatalf("unexpected config error: %v", err)
		}
	}
}

func TestUpdateRequestErrorsOmitPrivateSourceURL(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	address := srv.URL
	srv.Close()
	checker := newUpdateChecker("", address+"/VERSION?token=do-not-display")
	checker.url, checker.fallbackURL, checker.releaseURL = address, "", ""
	_, err := checker.check(context.Background())
	if err == nil || strings.Contains(err.Error(), "do-not-display") || !strings.Contains(err.Error(), "自定义版本源") {
		t.Fatalf("error leaked source or lost label: %v", err)
	}
}

func TestUpdateDedicatedProxyHandlesHTTPSWithoutChangingGlobalTransport(t *testing.T) {
	var proxyCalls, versionCalls atomic.Int32
	target := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		versionCalls.Add(1)
		if r.Header.Get("Proxy-Authorization") != "" {
			t.Error("proxy credential reached version source")
		}
		_, _ = io.WriteString(w, "0.2.24")
	}))
	defer target.Close()
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodConnect || r.Host != target.Listener.Addr().String() {
			http.Error(w, "unexpected proxy request", http.StatusBadRequest)
			return
		}
		proxyCalls.Add(1)
		upstream, err := net.DialTimeout("tcp", r.Host, time.Second)
		if err != nil {
			t.Error(err)
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		defer upstream.Close()
		client, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		defer client.Close()
		_, _ = io.WriteString(client, "HTTP/1.1 200 Connection Established\r\n\r\n")
		go func() { _, _ = io.Copy(upstream, client); _ = upstream.Close() }()
		_, _ = io.Copy(client, upstream)
	}))
	defer proxy.Close()
	global := http.DefaultTransport
	checker := newUpdateChecker(proxy.URL, target.URL+"/VERSION")
	defer checker.client.CloseIdleConnections()
	transport := checker.client.Transport.(*http.Transport)
	transport.TLSClientConfig = target.Client().Transport.(*http.Transport).TLSClientConfig.Clone()
	if latest, err := checker.check(context.Background()); err != nil || latest != "0.2.24" {
		t.Fatalf("HTTPS through update proxy: %q %v", latest, err)
	}
	if proxyCalls.Load() != 1 || versionCalls.Load() != 1 || http.DefaultTransport != global || transport == global {
		t.Fatal("update proxy was not isolated or not used")
	}
}

func TestUpdateStandardProxyEnvironment(t *testing.T) {
	// net/http caches process proxy variables, so verify real env handling in a child.
	if os.Getenv("YYB_TEST_UPDATE_PROXY_CHILD") == "1" {
		checker := newUpdateChecker("", "")
		defer checker.client.CloseIdleConnections()
		checker.url, checker.fallbackURL, checker.releaseURL = "http://version.invalid/VERSION", "", ""
		if latest, err := checker.check(context.Background()); err != nil || latest != "0.2.24" {
			t.Fatalf("HTTP_PROXY ignored: %q %v", latest, err)
		}
		transport := checker.client.Transport.(*http.Transport)
		for _, target := range []string{"http://172.18.0.2/", "http://192.168.9.83/", "http://10.0.1.1/", "http://qinglong/"} {
			req, _ := http.NewRequest(http.MethodGet, target, nil)
			if proxy, err := transport.Proxy(req); err != nil || proxy != nil {
				t.Fatalf("NO_PROXY did not bypass %s: %v %v", target, proxy, err)
			}
		}
		req, _ := http.NewRequest(http.MethodGet, "https://version.invalid/", nil)
		if proxy, err := transport.Proxy(req); err != nil || proxy == nil || proxy.String() != os.Getenv("HTTPS_PROXY") {
			t.Fatal("HTTPS_PROXY ignored")
		}
		return
	}
	var calls atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Host != "version.invalid" {
			t.Errorf("proxy target = %s", r.URL.Host)
		}
		_, _ = io.WriteString(w, "0.2.24")
	}))
	defer proxy.Close()
	t.Setenv("HTTP_PROXY", proxy.URL)
	t.Setenv("HTTPS_PROXY", proxy.URL)
	t.Setenv("NO_PROXY", "localhost,127.0.0.1,172.16.0.0/12,192.168.0.0/16,10.0.0.0/8,qinglong")
	t.Setenv("REQUEST_METHOD", "")
	t.Setenv("YYB_TEST_UPDATE_PROXY_CHILD", "1")
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if output, err := exec.CommandContext(ctx, executable, "-test.run=^TestUpdateStandardProxyEnvironment$").CombinedOutput(); err != nil {
		t.Fatalf("proxy environment regression: %v\n%s", err, output)
	}
	if calls.Load() != 1 {
		t.Fatalf("proxy received %d requests", calls.Load())
	}
}

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

func TestVersionCheckReleaseFallback(t *testing.T) {
	var releaseCalls, followedRedirect atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/raw":
			http.Error(w, "unavailable", http.StatusBadGateway)
		case "/api":
			w.Header().Set("X-RateLimit-Remaining", "0")
			w.WriteHeader(http.StatusForbidden)
		case "/latest":
			releaseCalls.Add(1)
			if r.Method != http.MethodHead {
				t.Errorf("release lookup method = %s", r.Method)
			}
			w.Header().Set("Location", maintenanceReleaseBase+"/tag/v0.2.23")
			w.WriteHeader(http.StatusFound)
		default:
			followedRedirect.Add(1)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()
	checker := &updateChecker{client: &http.Client{Timeout: time.Second}, url: srv.URL + "/raw", fallbackURL: srv.URL + "/api", releaseURL: srv.URL + "/latest"}
	for i := 0; i < 2; i++ {
		latest, err := checker.check(context.Background())
		if err != nil || latest != "0.2.23" {
			t.Fatalf("release fallback = %q, %v", latest, err)
		}
	}
	if releaseCalls.Load() != 1 || followedRedirect.Load() != 0 {
		t.Fatal("release fallback was not cached or followed a redirect")
	}
}

func TestVersionCheckRejectsUntrustedReleaseRedirects(t *testing.T) {
	for _, target := range []string{
		"https://example.com/releases/tag/v0.2.23",
		"https://github.com/other/repo/releases/tag/v0.2.23",
		maintenanceReleaseBase + "/tag/v0.2.23?x=1",
		maintenanceReleaseBase + "/tag/v0.2.23-rc.1",
		maintenanceReleaseBase + "/latest",
		"",
	} {
		t.Run(target, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Location", target)
				w.WriteHeader(http.StatusFound)
			}))
			defer srv.Close()
			checker := &updateChecker{client: &http.Client{Timeout: time.Second}, releaseURL: srv.URL}
			if _, err := checker.fetchRelease(context.Background()); err == nil {
				t.Fatalf("accepted invalid release destination %q", target)
			}
		})
	}
}

func TestVersionCheckDoesNotRequestReleaseWhenPrimaryWorks(t *testing.T) {
	var releaseCalls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/latest" {
			releaseCalls.Add(1)
		}
		_, _ = w.Write([]byte("0.2.23"))
	}))
	defer srv.Close()
	checker := &updateChecker{client: srv.Client(), url: srv.URL, releaseURL: srv.URL + "/latest"}
	if _, err := checker.check(context.Background()); err != nil || releaseCalls.Load() != 0 {
		t.Fatalf("unexpected fallback: %v", err)
	}
}

func TestVersionCheckFailureRecoversAfterShortCache(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			w.Header().Set("X-RateLimit-Remaining", "0")
			w.WriteHeader(http.StatusForbidden)
			return
		}
		_, _ = w.Write([]byte("0.2.23"))
	}))
	defer srv.Close()
	checker := &updateChecker{client: srv.Client(), url: srv.URL}
	if _, err := checker.check(context.Background()); err == nil || !strings.Contains(err.Error(), "请求限流") {
		t.Fatalf("missing rate limit diagnostic: %v", err)
	}
	if _, err := checker.check(context.Background()); err == nil || calls.Load() != 1 {
		t.Fatal("failure cache should prevent immediate repeated requests")
	}
	checker.checked = time.Now().Add(-31 * time.Second)
	if latest, err := checker.check(context.Background()); err != nil || latest != "0.2.23" {
		t.Fatalf("did not recover after 30 seconds: %q %v", latest, err)
	}
}

func TestVersionCheckCancellationDoesNotPoisonCache(t *testing.T) {
	started := make(chan struct{})
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			close(started)
			<-r.Context().Done()
			return
		}
		_, _ = w.Write([]byte("0.2.23"))
	}))
	defer srv.Close()
	checker := &updateChecker{client: &http.Client{Timeout: time.Second}, url: srv.URL}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { <-started; cancel() }()
	if _, err := checker.check(ctx); err == nil || !checker.checked.IsZero() {
		t.Fatal("cancelled request must not fill the cache")
	}
	if latest, err := checker.check(context.Background()); err != nil || latest != "0.2.23" {
		t.Fatalf("fresh request failed after cancellation: %q %v", latest, err)
	}
}

func TestVersionSourceHTTPErrorDoesNotAssume403IsRateLimit(t *testing.T) {
	err := versionSourceHTTPError(&http.Response{StatusCode: http.StatusForbidden, Header: make(http.Header)})
	if !strings.Contains(err.Error(), "无法确认") {
		t.Fatal(err)
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
