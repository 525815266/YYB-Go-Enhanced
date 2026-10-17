package main

import (
	"bytes"
	"context"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"yyb_go/internal/auth"
	"yyb_go/internal/httpapi"
)

func passwordInput(t *testing.T, password string) *os.File {
	t.Helper()
	path := filepath.Join(t.TempDir(), "stdin")
	if err := os.WriteFile(path, []byte(password), 0o600); err != nil {
		t.Fatal(err)
	}
	input, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { input.Close() })
	return input
}

func TestResetAdminPasswordUsesExistingConfiguredDatabase(t *testing.T) {
	root := t.TempDir()
	dbDir := filepath.Join(root, "db")
	if err := os.Mkdir(dbDir, 0o700); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	store, err := auth.Open(ctx, "sqlite", filepath.Join(dbDir, "auth.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.BootstrapAdmin(ctx, "admin", "old-password"); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	err = resetAdminPassword(httpapi.Config{AuthDriver: "sqlite", ResourceRoot: root}, "admin", passwordInput(t, "new-password\r\n"), &output)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(output.String(), "new-password") {
		t.Fatal("password leaked to output")
	}
	if _, err := store.Authenticate(ctx, "admin", "new-password"); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{"avatars", "qr", "templates", "static"} {
		if _, err := os.Stat(filepath.Join(root, dir)); !os.IsNotExist(err) {
			t.Fatalf("recovery initialized server resources: %s", dir)
		}
	}
}

func TestResetAdminPasswordRejectsMissingDatabaseAndDisabledAuth(t *testing.T) {
	root := t.TempDir()
	for _, driver := range []string{"sqlite", "none", ""} {
		if err := resetAdminPassword(httpapi.Config{AuthDriver: driver, ResourceRoot: root}, "admin", passwordInput(t, "new-password\n"), &bytes.Buffer{}); err == nil {
			t.Fatalf("accepted driver %q without an existing database", driver)
		}
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 0 {
		t.Fatal("recovery created an empty database or runtime directories")
	}
}

func TestReadAdminPasswordValidatesInput(t *testing.T) {
	for _, password := range []string{"", "short\n", strings.Repeat("x", 73), strings.Repeat("x", 1000)} {
		if _, err := readAdminPassword(passwordInput(t, password), &bytes.Buffer{}); err == nil {
			t.Fatal("invalid password accepted")
		}
	}
}

func TestExistingAuthFileSupportsPathsAndSQLiteURIs(t *testing.T) {
	path := filepath.Join(t.TempDir(), "auth file.db")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	urlPath := filepath.ToSlash(path)
	if filepath.VolumeName(path) != "" {
		urlPath = "/" + urlPath
	}
	uri := (&url.URL{Scheme: "file", Path: urlPath, RawQuery: "mode=rw"}).String()
	for _, dsn := range []string{path, path + "?_pragma=busy_timeout(5000)", uri, "file:" + filepath.ToSlash(path)} {
		got, err := existingAuthFile(dsn)
		if err != nil || got != path {
			t.Fatalf("existingAuthFile(%q) = %q, %v; want %q", dsn, got, err, path)
		}
	}
	for _, dsn := range []string{":memory:", "file::memory:", "file://remote/auth.db", t.TempDir()} {
		if _, err := existingAuthFile(dsn); err == nil {
			t.Fatalf("accepted non-file %q", dsn)
		}
	}
}
