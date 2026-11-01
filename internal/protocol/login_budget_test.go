package protocol

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/store"
)

func testBudget(limit int, window time.Duration) (*loginBudget, *time.Time) {
	now := time.Unix(1_700_000_000, 0)
	b := newLoginBudget(limit, window)
	b.now = func() time.Time { return now }
	return b, &now
}

// The budget allows its limit inside the window, refuses the next call, and
// frees a slot as the oldest call leaves the window.
func TestLoginBudgetAllowsTheLimitThenRefusesUntilTheWindowMovesOn(t *testing.T) {
	b, now := testBudget(3, 10*time.Minute)
	for i := range 3 {
		if ok, _ := b.take(1); !ok {
			t.Fatalf("call %d inside the budget was refused", i+1)
		}
		*now = now.Add(time.Minute)
	}
	if ok, _ := b.take(1); ok {
		t.Fatal("a call past the limit was allowed")
	}
	// The first call was at t0; at t0+10m it leaves the window.
	*now = now.Add(7 * time.Minute) // t0 + 10m
	if ok, _ := b.take(1); !ok {
		t.Fatal("the window moved on but the budget stayed exhausted")
	}
	if ok, _ := b.take(1); ok {
		t.Fatal("the freed slot was used twice")
	}
}

func TestLoginBudgetIsPerAccountAndCanBeDisabled(t *testing.T) {
	b, _ := testBudget(1, time.Hour)
	if ok, _ := b.take(1); !ok {
		t.Fatal("first call refused")
	}
	if ok, _ := b.take(1); ok {
		t.Fatal("second call for the same account allowed")
	}
	if ok, _ := b.take(2); !ok {
		t.Fatal("another account was held back by account 1")
	}

	b.set(0, time.Hour)
	for range 20 {
		if ok, _ := b.take(1); !ok {
			t.Fatal("a disabled budget refused a call")
		}
	}
	var none *loginBudget
	if ok, _ := none.take(1); !ok {
		t.Fatal("a nil budget must allow")
	}
}

// A run of refusals is reported once per window, not once per refusal.
func TestLoginBudgetSummarisesRefusals(t *testing.T) {
	b, now := testBudget(1, time.Hour)
	b.take(1)
	var summaries []*budgetSummary
	for range 40 {
		_, summary := b.take(1)
		if summary != nil {
			summaries = append(summaries, summary)
		}
		*now = now.Add(time.Second)
	}
	if len(summaries) != 1 {
		t.Fatalf("got %d summaries over 40 seconds, want 1", len(summaries))
	}
	if s := summaries[0]; s.Refused < 30 || s.Window < loginBudgetSummaryEvery {
		t.Fatalf("summary = %+v, want about 31 refusals over >= 30s", s)
	}
}

func TestLoginBudgetChangesTakeEffectImmediately(t *testing.T) {
	b, _ := testBudget(1, time.Hour)
	b.take(1)
	if ok, _ := b.take(1); ok {
		t.Fatal("budget of one allowed two calls")
	}
	b.set(5, time.Hour)
	if ok, _ := b.take(1); !ok {
		t.Fatal("raising the limit did not free the account")
	}
}

// Through the real recovery path: once an account has used its budget a -14 is
// returned as it came, no probe runs, and no five minute backoff is registered
// for the AppID, so it can still be repaired when the window has moved on.
func TestRecoveryStopsAtTheLoginBudgetWithoutBackoff(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	status := "alive"
	acc, err := db.UpsertAccount(ctx, "synthetic-budget-account", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "synthetic-access"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	cfg := DefaultConfig()
	cfg.LoginBudget, cfg.LoginWindow = 2, 10*time.Minute
	pool := NewPool(cfg, db)
	now := time.Unix(1_700_000_000, 0)
	pool.budget.now = func() time.Time { return now }

	original := &ShortlinkRejectedError{Code: -14}
	probes := 0
	probe := func(context.Context) (WmpfSession, map[string]any, error) {
		probes++
		return WmpfSession{}, nil, errors.New("synthetic ticket refusal")
	}
	run := func(target string) error {
		t.Helper()
		_, err := pool.recoverCodeSession(ctx, acc, target, "", original, probe)
		return err
	}
	_ = run("wx-a")
	_ = run("wx-b")
	if probes != 2 {
		t.Fatalf("probes inside the budget = %d, want 2", probes)
	}
	if err := run("wx-c"); !errors.Is(err, original) {
		t.Fatalf("a refused recovery must return the original -14, got %v", err)
	}
	if probes != 2 {
		t.Fatal("a probe ran although the login budget was used up")
	}
	pool.mu.Lock()
	_, registered := pool.codeRepairs[fmt.Sprintf("%d\x00%s\x00%s", acc.ID, "", "wx-c")]
	pool.mu.Unlock()
	if registered {
		t.Fatal("a refused recovery left a backoff behind for its AppID")
	}

	// Ten minutes later the same AppID may be repaired.
	now = now.Add(10*time.Minute + time.Second)
	_ = run("wx-c")
	if probes != 3 {
		t.Fatalf("after the window moved on the probe ran %d times in total, want 3", probes)
	}
}
