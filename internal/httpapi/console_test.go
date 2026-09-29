package httpapi

import (
	"io/fs"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	embeddedresource "yyb_go/resource"
)

func TestExperimentalConsoleIsolatedFromManagementAPIs(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	root := t.TempDir()
	app, err := NewApp(Config{ResourceRoot: root, AuthDriver: "sqlite", AuthDSN: filepath.Join(root, "auth.db"), AdminUser: "uiadmin", AdminPassword: "ui-test-password"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	app.resources.Static = filepath.Join(root, "unbuilt-static")
	handler := app.Handler()
	request := func(path string) *httptest.ResponseRecorder {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		return response
	}
	if result := request("/console/login"); result.Code != http.StatusServiceUnavailable {
		t.Fatalf("unbuilt console status = %d", result.Code)
	}
	dir := filepath.Join(app.resources.Static, "console")
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "index.html"), []byte(`<html><div id="app"></div></html>`), 0644); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/console", "/console/login", "/console/users", "/console/runs"} {
		result := request(path)
		if result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `id="app"`) || result.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("SPA entry %s: status=%d body=%s", path, result.Code, result.Body.String())
		}
	}
	for _, path := range []string{"/accounts", "/api/auth/users", "/api/qinglong/runs?ref=1"} {
		if result := request(path); result.Code != http.StatusUnauthorized {
			t.Fatalf("console bypassed auth for %s: %d", path, result.Code)
		}
	}
	if result := request("/wx/not-a-route"); result.Code != http.StatusNotFound {
		t.Fatalf("SPA swallowed protocol 404: %d", result.Code)
	}
}

func TestBuiltConsoleReferencesEmbeddedAssets(t *testing.T) {
	index, err := fs.ReadFile(embeddedresource.WebAssets, "static/console/index.html")
	if os.IsNotExist(err) {
		t.Skip("console not built; CI builds it before Go regression")
	}
	if err != nil {
		t.Fatal(err)
	}
	matches := regexp.MustCompile(`(?:src|href)="/static/console/([^"]+)"`).FindAllSubmatch(index, -1)
	if len(matches) < 2 {
		t.Fatal("built console is missing script or stylesheet references")
	}
	for _, match := range matches {
		asset := "static/console/" + string(match[1])
		content, err := fs.ReadFile(embeddedresource.WebAssets, asset)
		if err != nil || len(content) == 0 {
			t.Fatalf("missing embedded console asset %s: %v", asset, err)
		}
	}
}
