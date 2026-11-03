package main

import (
	"os"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/protocol"
)

func TestFreshSessionWindowEnv(t *testing.T) {
	unsetTestEnv(t, "YYB_FRESH_SESSION_WINDOW")
	if got := envFreshSessionWindow("YYB_FRESH_SESSION_WINDOW"); got != protocol.DefaultFreshSessionWindow {
		t.Fatalf("unset gives %s, want the default %s", got, protocol.DefaultFreshSessionWindow)
	}
	for _, off := range []string{"0", "off", "OFF"} {
		t.Setenv("YYB_FRESH_SESSION_WINDOW", off)
		if got := envFreshSessionWindow("YYB_FRESH_SESSION_WINDOW"); got >= 0 {
			t.Fatalf("YYB_FRESH_SESSION_WINDOW=%q gives %s, want a negative value (guard off)", off, got)
		}
	}
	t.Setenv("YYB_FRESH_SESSION_WINDOW", "30s")
	if got := envFreshSessionWindow("YYB_FRESH_SESSION_WINDOW"); got != 30*time.Second {
		t.Fatalf("window = %s, want 30s", got)
	}
	t.Setenv("YYB_FRESH_SESSION_WINDOW", "garbage")
	if got := envFreshSessionWindow("YYB_FRESH_SESSION_WINDOW"); got != protocol.DefaultFreshSessionWindow {
		t.Fatalf("invalid value gives %s, want the default", got)
	}
}

// main builds its configuration inline, so the wiring is checked in the source.
func TestMainReadsTheFreshSessionWindow(t *testing.T) {
	source, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(source), `FreshSessionWindow: envFreshSessionWindow("YYB_FRESH_SESSION_WINDOW")`) {
		t.Fatal("main no longer reads YYB_FRESH_SESSION_WINDOW into the Config")
	}
}
