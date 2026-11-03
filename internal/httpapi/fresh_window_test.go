package httpapi

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/protocol"
)

// tuning.json can shorten the guard or switch it off; a missing key keeps the
// configured value and removing the file restores it.
func TestPacerReadsTheFreshSessionWindowFromTheTuningFile(t *testing.T) {
	file := filepath.Join(t.TempDir(), "tuning.json")
	defaults := pacingSettings{MinGap: 2 * time.Second, MaxWait: 10 * time.Second, FreshWindow: 2 * time.Minute}
	p := newAccountPacer(file, defaults)
	now := time.Unix(1_700_000_000, 0)
	p.now = func() time.Time { return now }
	active := func() pacingSettings {
		now = now.Add(2 * time.Second)
		p.mu.Lock()
		defer p.mu.Unlock()
		return p.settingsLocked(now)
	}
	write := func(body string) {
		t.Helper()
		if err := os.WriteFile(file, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Chtimes(file, now, now); err != nil {
			t.Fatal(err)
		}
	}

	write(`{"fresh_session_window_ms": 30000}`)
	if got := active(); got.FreshWindow != 30*time.Second || got.MinGap != 2*time.Second {
		t.Fatalf("the key must override only itself, got %+v", got)
	}
	write(`{"fresh_session_window_ms": 0}`)
	if got := active(); got.FreshWindow != 0 {
		t.Fatalf("a zero window must switch the guard off, got %+v", got)
	}
	write(`{"fresh_session_window_ms": 99999999999}`)
	if got := active(); got.FreshWindow != maxFreshWindow {
		t.Fatalf("the window must be clamped, got %+v", got)
	}
	write(`{"code_min_gap_ms": 500}`)
	if got := active(); got.FreshWindow != 2*time.Minute {
		t.Fatalf("a missing key must keep the configured window, got %+v", got)
	}
	if err := os.Remove(file); err != nil {
		t.Fatal(err)
	}
	if got := active(); got != defaults {
		t.Fatalf("removing the file must restore the defaults, got %+v", got)
	}
}

// The installed change hook hands the window to the running pool, and the
// configured value (zero keeps the default, negative switches off) is what the
// pool and the pacer start with.
func TestRunningAppAdoptsTheFreshSessionWindow(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	if got := app.pool.FreshSessionWindow(); got != protocol.DefaultFreshSessionWindow {
		t.Fatalf("an unset Config must keep the default window, got %s", got)
	}
	file := filepath.Join(app.resources.DB, "tuning.json")
	now := time.Unix(1_700_000_000, 0)
	app.pacer.now = func() time.Time { return now }
	tick := func() {
		now = now.Add(2 * time.Second)
		app.pacer.reserve(1)
	}
	if err := os.WriteFile(file, []byte(`{"fresh_session_window_ms": 0}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(file, now.Add(time.Second), now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	tick()
	if got := app.pool.FreshSessionWindow(); got != 0 {
		t.Fatalf("the pool did not adopt the switched-off guard, window %s", got)
	}
	if err := os.Remove(file); err != nil {
		t.Fatal(err)
	}
	tick()
	if got := app.pool.FreshSessionWindow(); got != protocol.DefaultFreshSessionWindow {
		t.Fatalf("removing the file must restore the configured window, got %s", got)
	}
}

func TestConfigCanStartTheAppWithTheFreshSessionGuardOff(t *testing.T) {
	app, err := NewApp(Config{
		ResourceRoot:       t.TempDir(),
		RequestTimeout:     time.Second,
		AvatarTimeout:      time.Second,
		SessionTTL:         time.Minute,
		QRSessionTTL:       time.Minute,
		FreshSessionWindow: -1,
	})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	if got := app.pool.FreshSessionWindow(); got != 0 {
		t.Fatalf("the pool must start with the guard off, window %s", got)
	}
	if got := app.pacer.defaults.FreshWindow; got != 0 {
		t.Fatalf("tuning.json must fall back to the guard off, got %s", got)
	}
}
