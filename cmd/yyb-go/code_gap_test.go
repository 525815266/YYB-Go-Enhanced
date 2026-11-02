package main

import (
	"os"
	"strings"
	"testing"
	"time"
)

// The breaker and the login budget only react to failures and OAuth calls, so
// the request spacing is what limits a burst of successful codes. Losing its
// default would silently remove that limit.
func TestCodeMinGapDefaultsToThreeSecondsAndCanBeSwitchedOff(t *testing.T) {
	unsetTestEnv(t, "YYB_CODE_MIN_GAP")
	if got := envDuration("YYB_CODE_MIN_GAP", defaultCodeMinGap); got != 3*time.Second {
		t.Fatalf("default gap = %s, want 3s", got)
	}
	for _, off := range []string{"0", "off", "OFF"} {
		t.Setenv("YYB_CODE_MIN_GAP", off)
		if got := envDuration("YYB_CODE_MIN_GAP", defaultCodeMinGap); got != 0 {
			t.Fatalf("YYB_CODE_MIN_GAP=%q gives %s, want pacing off", off, got)
		}
	}
	t.Setenv("YYB_CODE_MIN_GAP", "5s")
	if got := envDuration("YYB_CODE_MIN_GAP", defaultCodeMinGap); got != 5*time.Second {
		t.Fatalf("gap = %s, want 5s", got)
	}
	t.Setenv("YYB_CODE_MIN_GAP", "garbage")
	if got := envDuration("YYB_CODE_MIN_GAP", defaultCodeMinGap); got != 3*time.Second {
		t.Fatalf("invalid value gives %s, want the 3s default", got)
	}
}

// main builds its configuration inline, so the wiring is checked in the source:
// the service must start with the default gap, not with pacing off.
func TestMainStartsWithTheDefaultCodeMinGap(t *testing.T) {
	source, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(source), `CodeMinGap:  envDuration("YYB_CODE_MIN_GAP", defaultCodeMinGap)`) {
		t.Fatal("main no longer reads YYB_CODE_MIN_GAP with defaultCodeMinGap as its default")
	}
}
