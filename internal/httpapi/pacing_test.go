package httpapi

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"yyb_go/internal/store"
)

func testPacer(defaults pacingSettings) (*accountPacer, *time.Time) {
	now := time.Unix(1_700_000_000, 0)
	p := newAccountPacer("", defaults)
	p.now = func() time.Time { return now }
	return p, &now
}

func pacingRefusal(err error) *circuitOpenError {
	var open *circuitOpenError
	if errors.As(err, &open) && open.Scope == "pacing" {
		return open
	}
	return nil
}

// A burst gets consecutive slots one gap apart, then is refused without taking
// a slot, and the queue drains as time passes.
func TestPacerSpacesRequestsOfOneAccount(t *testing.T) {
	p, now := testPacer(pacingSettings{MinGap: 2 * time.Second, MaxWait: 6 * time.Second})
	for i, want := range []time.Duration{0, 2 * time.Second, 4 * time.Second, 6 * time.Second} {
		wait, _, err := p.reserve(1)
		if err != nil || wait != want {
			t.Fatalf("request %d: wait %v err %v, want %v", i, wait, err, want)
		}
	}
	for range 3 {
		wait, _, err := p.reserve(1)
		open := pacingRefusal(err)
		if open == nil || wait != 0 || open.RetryAfter < 2*time.Second {
			t.Fatalf("overflow request was not refused as pacing: wait %v err %v", wait, err)
		}
	}
	*now = now.Add(2 * time.Second)
	if wait, _, err := p.reserve(1); err != nil || wait != 6*time.Second {
		t.Fatalf("refusals must not push the queue out; got wait %v err %v", wait, err)
	}
}

func TestPacerAccountsAreIndependentAndIdleAccountsAreImmediate(t *testing.T) {
	p, now := testPacer(pacingSettings{MinGap: time.Second, MaxWait: 2 * time.Second})
	for range 3 {
		if _, _, err := p.reserve(1); err != nil {
			t.Fatal(err)
		}
	}
	if wait, _, err := p.reserve(2); err != nil || wait != 0 {
		t.Fatalf("another account waited behind account 1: wait %v err %v", wait, err)
	}
	*now = now.Add(time.Minute)
	if wait, _, err := p.reserve(1); err != nil || wait != 0 {
		t.Fatalf("an idle account must be served at once: wait %v err %v", wait, err)
	}
}

func TestPacerDisabledWhenTheGapIsZero(t *testing.T) {
	p, _ := testPacer(pacingSettings{})
	for range 100 {
		if wait, summary, err := p.reserve(1); wait != 0 || summary != nil || err != nil {
			t.Fatalf("disabled pacer delayed a request: %v %v %v", wait, summary, err)
		}
	}
	var none *accountPacer
	if wait, _, err := none.reserve(1); wait != 0 || err != nil {
		t.Fatalf("nil pacer must allow: %v %v", wait, err)
	}
}

// A burst is reported once as counts, not once per request.
func TestPacerReportsABurstAsOneSummary(t *testing.T) {
	p, now := testPacer(pacingSettings{MinGap: time.Second, MaxWait: 2 * time.Second})
	var summaries int
	for range 5 {
		if _, summary, _ := p.reserve(1); summary != nil {
			summaries++
		}
	}
	if summaries != 0 {
		t.Fatal("a summary was reported before the window ended")
	}
	*now = now.Add(31 * time.Second)
	_, summary, err := p.reserve(1)
	if err != nil || summary == nil {
		t.Fatalf("expected a summary after the window: %v %v", summary, err)
	}
	if summary.Waited != 2 || summary.Refused != 2 || summary.Window < 30*time.Second {
		t.Fatalf("summary = %+v, want waited 2 refused 2 over >=30s", summary)
	}
	if _, again, _ := p.reserve(1); again != nil {
		t.Fatal("counters were not reset after the summary")
	}
}

func TestPacerReadsTheTuningFile(t *testing.T) {
	file := filepath.Join(t.TempDir(), "tuning.json")
	defaults := pacingSettings{MinGap: 2 * time.Second, MaxWait: 10 * time.Second}
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

	if got := active(); got != defaults {
		t.Fatalf("without a file the defaults apply, got %+v", got)
	}
	write(`{"code_min_gap_ms": 500}`)
	if got := active(); got.MinGap != 500*time.Millisecond || got.MaxWait != 10*time.Second {
		t.Fatalf("one key must override only itself, got %+v", got)
	}
	write(`{"code_min_gap_ms": 750, "code_max_wait_ms": 4000}`)
	if got := active(); got.MinGap != 750*time.Millisecond || got.MaxWait != 4*time.Second {
		t.Fatalf("both keys must apply, got %+v", got)
	}
	write(`{not json`)
	if got := active(); got.MinGap != 750*time.Millisecond || got.MaxWait != 4*time.Second {
		t.Fatalf("an unreadable file must keep the last good settings, got %+v", got)
	}
	write(`{"code_min_gap_ms": 99999999, "code_max_wait_ms": -5}`)
	if got := active(); got.MinGap != pacingMaxGap || got.MaxWait != 0 {
		t.Fatalf("values must be clamped, got %+v", got)
	}
	write(`{"code_min_gap_ms": 0}`)
	if got := active(); got.MinGap != 0 {
		t.Fatalf("a zero gap must turn pacing off, got %+v", got)
	}
	if err := os.Remove(file); err != nil {
		t.Fatal(err)
	}
	if got := active(); got != defaults {
		t.Fatalf("removing the file must restore the defaults, got %+v", got)
	}
}

// A refusal is a local 429 like the breaker's, so every entry point already
// knows how to answer it.
func TestPacingRefusalIsALocal429(t *testing.T) {
	p, _ := testPacer(pacingSettings{MinGap: time.Second, MaxWait: time.Second})
	var refusal error
	for range 3 {
		_, _, refusal = p.reserve(7)
	}
	if pacingRefusal(refusal) == nil {
		t.Fatalf("expected a pacing refusal, got %v", refusal)
	}
	rec := httptest.NewRecorder()
	if !writeIfCircuitOpen(rec, refusal) || rec.Code != http.StatusTooManyRequests || rec.Header().Get("Retry-After") == "" {
		t.Fatalf("refusal did not map to 429 with Retry-After: %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"circuit_open":"pacing"`) {
		t.Fatalf("429 body does not name the pacing scope: %s", rec.Body.String())
	}
}

// Through the real call path: a burst of requests for one account reaches the
// upstream only as often as there are slots, spread over time, and the rest are
// refused without calling the pool.
func TestInvokeWXAppPacesABurstAndRefusesTheOverflow(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	app.pacer = newAccountPacer("", pacingSettings{MinGap: 40 * time.Millisecond, MaxWait: 100 * time.Millisecond})
	acc := insertKeepAliveTestAccount(t, app, "openid-pacing", time.Now().Add(time.Hour))
	var calls atomic.Int32
	var mu sync.Mutex
	var starts []time.Time
	ok := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		calls.Add(1)
		mu.Lock()
		starts = append(starts, time.Now())
		mu.Unlock()
		return map[string]any{"code": "c"}, nil
	}

	const burst = 12
	results := make([]error, burst)
	var wg sync.WaitGroup
	for i := range burst {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, results[i] = app.invokeWXApp(context.Background(), acc, fmt.Sprintf("wxApp%d", i), nil, ok)
		}()
	}
	wg.Wait()

	var served, refused int
	for _, err := range results {
		switch {
		case err == nil:
			served++
		case pacingRefusal(err) != nil:
			refused++
		default:
			t.Fatalf("unexpected error: %v", err)
		}
	}
	if served < 2 || served > 5 || served+refused != burst {
		t.Fatalf("served %d refused %d of %d", served, refused, burst)
	}
	if int(calls.Load()) != served {
		t.Fatalf("the pool saw %d calls for %d served requests", calls.Load(), served)
	}
	mu.Lock()
	defer mu.Unlock()
	first, last := starts[0], starts[0]
	for _, s := range starts {
		if s.Before(first) {
			first = s
		}
		if s.After(last) {
			last = s
		}
	}
	// Scheduler slack is allowed; the point is that the calls were not simultaneous.
	if span := last.Sub(first); span < 30*time.Millisecond {
		t.Fatalf("%d served calls were not spread out; they began within %v", served, span)
	}
}

// Refusals must not flood the event log. One summary per window is recorded
// instead, and the held-off requests never reach the breaker's deferred event.
func TestPacingRecordsOneSummaryEventAndNoPerRequestEvent(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	clock := time.Unix(1_700_000_000, 0)
	var clockMu sync.Mutex
	app.pacer = newAccountPacer("", pacingSettings{MinGap: 10 * time.Millisecond, MaxWait: 20 * time.Millisecond})
	app.pacer.now = func() time.Time {
		clockMu.Lock()
		defer clockMu.Unlock()
		return clock
	}
	acc := insertKeepAliveTestAccount(t, app, "openid-pacing-events", time.Now().Add(time.Hour))
	ok := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return map[string]any{"code": "c"}, nil
	}
	ctx := context.Background()
	for i := range 8 {
		_, _ = app.invokeWXApp(ctx, acc, fmt.Sprintf("wxEvent%d", i), nil, ok)
	}
	clockMu.Lock()
	clock = clock.Add(31 * time.Second)
	clockMu.Unlock()
	if _, err := app.invokeWXApp(ctx, acc, "wxAfterWindow", nil, ok); err != nil {
		t.Fatalf("request after the window: %v", err)
	}

	app.events.Close() // flush the asynchronous queue
	entries, err := app.db.ListSystemLogs(context.Background(), store.SystemLogFilter{Limit: 100})
	if err != nil {
		t.Fatal(err)
	}
	var paced int
	for _, entry := range entries {
		switch entry.Event {
		case "protocol.paced":
			paced++
			details := string(entry.Details)
			if !strings.Contains(details, `"refused":`) || !strings.Contains(details, `"waited":`) {
				t.Fatalf("summary details missing counts: %s", details)
			}
		case "protocol.circuit_deferred":
			t.Fatalf("a pacing refusal produced a per-request deferred event: %s", entry.Details)
		}
	}
	if paced != 1 {
		t.Fatalf("expected exactly one protocol.paced event, got %d", paced)
	}
}
