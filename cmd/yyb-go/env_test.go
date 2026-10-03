package main

import (
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func unsetTestEnv(t *testing.T, keys ...string) {
	t.Helper()
	for _, key := range keys {
		t.Setenv(key, "")
		_ = os.Unsetenv(key)
	}
}

func writeEnvFixture(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestEnvFileSupportsWindowsEncodingAndPreservesProcessValues(t *testing.T) {
	unsetTestEnv(t, "YYB_TEST_ENV_PASSWORD", "YYB_TEST_ENV_QUOTED")
	t.Setenv("YYB_TEST_ENV_EXISTING", "process")
	t.Setenv("YYB_TEST_ENV_EMPTY", "")
	dir := t.TempDir()
	writeEnvFixture(t, dir, ".env", "\ufeff# Windows UTF-8 BOM / CRLF\r\nexport YYB_TEST_ENV_PASSWORD='a#b$c\\d'\r\nYYB_TEST_ENV_QUOTED=\"hello world\" # comment\r\nYYB_TEST_ENV_EXISTING=file\r\nYYB_TEST_ENV_EMPTY=file\r\n")
	loaded, err := loadEnvFile("", dir, t.TempDir())
	if err != nil || loaded != filepath.Join(dir, ".env") {
		t.Fatalf("load = %q %v", loaded, err)
	}
	for key, want := range map[string]string{"YYB_TEST_ENV_PASSWORD": `a#b$c\d`, "YYB_TEST_ENV_QUOTED": "hello world", "YYB_TEST_ENV_EXISTING": "process", "YYB_TEST_ENV_EMPTY": ""} {
		if os.Getenv(key) != want {
			t.Errorf("wrong handling for %s", key)
		}
	}
}

func TestEnvFileDiscoveryAndExplicitSelection(t *testing.T) {
	for _, mode := range []string{"executable", "working-directory", "explicit", "missing-default", "missing-explicit"} {
		t.Run(mode, func(t *testing.T) {
			unsetTestEnv(t, "YYB_TEST_ENV_SOURCE")
			exeDir, workDir := t.TempDir(), t.TempDir()
			explicit, want := "", mode
			switch mode {
			case "executable":
				writeEnvFixture(t, exeDir, ".env", "YYB_TEST_ENV_SOURCE=executable")
				writeEnvFixture(t, workDir, ".env", "YYB_TEST_ENV_SOURCE=wrong")
			case "working-directory":
				writeEnvFixture(t, workDir, ".env", "YYB_TEST_ENV_SOURCE=working-directory")
			case "explicit":
				writeEnvFixture(t, exeDir, ".env", "YYB_TEST_ENV_SOURCE=wrong")
				writeEnvFixture(t, workDir, "chosen.env", "YYB_TEST_ENV_SOURCE=explicit")
				explicit = "chosen.env"
			case "missing-default":
				want = ""
			case "missing-explicit":
				explicit, want = "missing.env", ""
			}
			_, err := loadEnvFile(explicit, exeDir, workDir)
			if (err != nil) != (mode == "missing-explicit") || os.Getenv("YYB_TEST_ENV_SOURCE") != want {
				t.Fatalf("wrong source selection: %v", err)
			}
		})
	}
}

func TestMalformedEnvDoesNotPartiallyApplyOrPrintSecrets(t *testing.T) {
	for _, content := range []string{"YYB_TEST_ENV_PARTIAL=changed\nPASSWORD='private-secret", "\xff\xfeS\x00E\x00C\x00R\x00E\x00T\x00"} {
		unsetTestEnv(t, "YYB_TEST_ENV_PARTIAL")
		dir := t.TempDir()
		writeEnvFixture(t, dir, ".env", content)
		if _, err := loadEnvFile("", dir, dir); err == nil || strings.Contains(err.Error(), "private-secret") {
			t.Fatal("invalid env accepted or secret exposed")
		}
		if _, exists := os.LookupEnv("YYB_TEST_ENV_PARTIAL"); exists {
			t.Fatal("malformed file was partially applied")
		}
	}
}

func TestStartupFlagPrecedence(t *testing.T) {
	t.Setenv("YYB_BIND_ADDRESS", "127.0.0.2")
	t.Setenv("YYB_PORT", "invalid-but-overridden-by-cli")
	t.Setenv("YYB_KEEPALIVE_INTERVAL", "0")
	t.Setenv("YYB_KEEPALIVE_AHEAD", "5m")
	flags := flag.NewFlagSet("test", flag.ContinueOnError)
	host := flags.String("host", "127.0.0.1", "")
	port := flags.Int("port", 8000, "")
	interval := flags.Duration("keepalive-interval", time.Minute, "")
	ahead := flags.Duration("keepalive-ahead", 45*time.Minute, "")
	if err := flags.Parse([]string{"-port", "18000"}); err != nil {
		t.Fatal(err)
	}
	if err := applyEnvFlags(flags); err != nil || *host != "127.0.0.2" || *port != 18000 || *interval != 0 || *ahead != 5*time.Minute {
		t.Fatalf("flag/env precedence failed: %v", err)
	}
	t.Setenv("YYB_KEEPALIVE_INTERVAL", "secret-invalid-duration")
	other := flag.NewFlagSet("other", flag.ContinueOnError)
	other.String("host", "", "")
	other.Int("port", 0, "")
	other.Duration("keepalive-interval", 0, "")
	other.Duration("keepalive-ahead", 0, "")
	t.Setenv("YYB_PORT", "8000")
	if err := applyEnvFlags(other); err == nil || strings.Contains(err.Error(), "secret-invalid-duration") {
		t.Fatal("invalid flag env accepted or leaked")
	}
}
