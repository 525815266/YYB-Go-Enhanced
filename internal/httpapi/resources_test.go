package httpapi

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestEnsureResourcesRestoresEmbeddedWebAssetsWithoutOverwriting(t *testing.T) {
	root := t.TempDir()
	res, err := ensureResources(root, false)
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
	if _, err := ensureResources(root, false); err != nil {
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

func TestStandaloneWebAssetsUseVersionedDirectory(t *testing.T) {
	root := t.TempDir()
	old, err := ensureResourcesVersion(root, true, "0.2.16")
	if err != nil {
		t.Fatalf("restore old version: %v", err)
	}
	if err := os.WriteFile(filepath.Join(old.Templates, "login.html"), []byte("stale page"), 0o644); err != nil {
		t.Fatalf("seed old page: %v", err)
	}

	next, err := ensureResourcesVersion(root, true, "0.2.17")
	if err != nil {
		t.Fatalf("restore next version: %v", err)
	}
	if next.Templates == old.Templates || next.Static == old.Static {
		t.Fatalf("web roots were reused: old=%#v next=%#v", old, next)
	}
	login, err := os.ReadFile(filepath.Join(next.Templates, "login.html"))
	if err != nil {
		t.Fatalf("read next login page: %v", err)
	}
	if bytes.Equal(login, []byte("stale page")) || !bytes.Contains(login, []byte("<!doctype html>")) {
		t.Fatalf("next version restored stale page: %q", login)
	}
	if _, err := os.Stat(filepath.Join(root, "db")); err != nil {
		t.Fatalf("runtime database directory moved: %v", err)
	}
}
