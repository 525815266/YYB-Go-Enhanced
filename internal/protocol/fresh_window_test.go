package protocol

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/store"
)

func TestEffectiveFreshWindow(t *testing.T) {
	for _, tc := range []struct{ in, want time.Duration }{
		{0, DefaultFreshSessionWindow},
		{-1, 0},
		{-time.Hour, 0},
		{30 * time.Second, 30 * time.Second},
	} {
		if got := EffectiveFreshWindow(tc.in); got != tc.want {
			t.Fatalf("EffectiveFreshWindow(%s) = %s, want %s", tc.in, got, tc.want)
		}
	}
}

// A Config that does not set the field keeps the guard on, as it was before the
// setting existed; only a negative value switches it off.
func TestPoolFreshWindowFollowsConfigAndSetter(t *testing.T) {
	if got := NewPool(DefaultConfig(), nil).FreshSessionWindow(); got != DefaultFreshSessionWindow {
		t.Fatalf("default pool window = %s, want %s", got, DefaultFreshSessionWindow)
	}
	if got := NewPool(Config{}, nil).FreshSessionWindow(); got != DefaultFreshSessionWindow {
		t.Fatalf("a Config without the field must keep the default, got %s", got)
	}
	if got := NewPool(Config{FreshSessionWindow: -1}, nil).FreshSessionWindow(); got != 0 {
		t.Fatalf("a negative Config value must switch the guard off, got %s", got)
	}
	pool := NewPool(Config{FreshSessionWindow: 30 * time.Second}, nil)
	if got := pool.FreshSessionWindow(); got != 30*time.Second {
		t.Fatalf("configured window = %s, want 30s", got)
	}
	pool.SetFreshSessionWindow(time.Minute)
	if got := pool.FreshSessionWindow(); got != time.Minute {
		t.Fatalf("window after the setter = %s, want 1m", got)
	}
	pool.SetFreshSessionWindow(-time.Second)
	if got := pool.FreshSessionWindow(); got != 0 {
		t.Fatalf("a negative setter value must switch the guard off, got %s", got)
	}
}

// On accounts whose session serves one code, the guard refuses exactly the
// repair that would work. Switched off, a -14 right after a success is repaired
// like any other; switched back on at run time, the same success covers the
// session again, and the configured window decides when that cover ends.
func TestSwitchedOffFreshSessionGuardLetsRecoveryRun(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	status := "alive"
	acc, err := db.UpsertAccount(ctx, "synthetic-window-account", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "synthetic-access"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)
	original := &ShortlinkRejectedError{Code: -14}
	probes := 0
	probe := func(context.Context) (WmpfSession, map[string]any, error) {
		probes++
		return WmpfSession{}, nil, errors.New("synthetic ticket refusal")
	}
	st := testSession("window", "")
	if err := db.PutSession(ctx, acc.ID, nil, st.toBlob(), st.PSK.ExpiredAt, st.TCPProxy); err != nil {
		t.Fatal(err)
	}
	recoverFor := func(target string) {
		t.Helper()
		_, _ = pool.recoverCodeSession(ctx, acc, target, "", original, probe)
	}

	pool.SetFreshSessionWindow(0)
	pool.markSessionVerified(acc, acc.ID, st)
	pool.mu.Lock()
	kept := len(pool.verified)
	pool.mu.Unlock()
	if kept != 0 {
		t.Fatalf("a switched-off guard must not remember successes, kept %d", kept)
	}
	recoverFor("wx-a")
	recoverFor("wx-b")
	if probes != 2 {
		t.Fatalf("recovery ran %d times, want 2: the guard is off", probes)
	}

	pool.SetFreshSessionWindow(time.Minute)
	pool.markSessionVerified(acc, acc.ID, st)
	recoverFor("wx-c")
	if probes != 2 {
		t.Fatal("recovery ran on a session covered by the success")
	}

	pool.mu.Lock()
	key := sessionKey(acc.ID, "")
	verified := pool.verified[key]
	verified.At = time.Now().Add(-time.Minute - time.Second)
	pool.verified[key] = verified
	pool.mu.Unlock()
	recoverFor("wx-d")
	if probes != 3 {
		t.Fatal("recovery stayed suppressed after the configured one-minute window")
	}
}
