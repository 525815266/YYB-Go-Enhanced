package httpapi

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestEnsureResourcesRestoresEmbeddedWebAssetsWithoutOverwriting(t *testing.T) {
	root := t.TempDir()
	res, err := ensureResources(root)
	if err != nil {
		t.Fatalf("ensure resources: %v", err)
	}

	loginPath := filepath.Join(res.Templates, "login.html")
	login, err := os.ReadFile(loginPath)
	if err != nil {
		t.Fatalf("read restored login template: %v", err)
	}
	if !bytes.Contains(login, []byte("<!doctype html>")) {
		t.Fatalf("restored login template does not contain HTML: %q", login)
	}
	if _, err := os.Stat(filepath.Join(res.Static, "css", "auth.css")); err != nil {
		t.Fatalf("restored auth stylesheet: %v", err)
	}

	custom := []byte("custom login template")
	if err := os.WriteFile(loginPath, custom, 0o644); err != nil {
		t.Fatalf("write custom login template: %v", err)
	}
	if _, err := ensureResources(root); err != nil {
		t.Fatalf("ensure resources again: %v", err)
	}
	after, err := os.ReadFile(loginPath)
	if err != nil {
		t.Fatalf("read custom login template: %v", err)
	}
	if !bytes.Equal(after, custom) {
		t.Fatalf("custom login template was overwritten: %q", after)
	}
}
