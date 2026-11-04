package httpapi

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

// A request whose ManualAuth is refused asks for a fresh login ticket. That is
// an OAuth-layer call, so an account that has used its budget fails with the
// error it already had and the ticket endpoint is not touched again.
func TestLoginBudgetStopsTheFreshTicketOfAFailingCall(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	app.pool.SetLoginBudget(1, time.Hour)
	acc := insertKeepAliveTestAccount(t, app, "budget-ticket", time.Now().Add(time.Hour))
	fetches, calls := 0, 0
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
		fetches++
		return "fresh-buffer", nil
	}
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		t.Fatal("a refused ticket must not turn into a credential refresh")
		return protocol.LoginBufferCredentials{}, nil
	}
	call := func(_ context.Context, used *store.WechatAccount, _ string, _ map[string]any, _ string, _ bool) (map[string]any, error) {
		calls++
		if used.LoginBuffer != "fresh-buffer" {
			return nil, &protocol.ManualAuthRejectedError{Code: -10}
		}
		return map[string]any{"code": "synthetic-code"}, nil
	}

	if result, err := app.invokeWXApp(context.Background(), acc, "wx-first", nil, call); err != nil || result["code"] != "synthetic-code" {
		t.Fatalf("first request inside the budget: %v %v", result, err)
	}
	callsBefore := calls
	_, err := app.invokeWXApp(context.Background(), acc, "wx-second", nil, call)
	var auth *protocol.ManualAuthRejectedError
	if !errors.Is(err, errLoginBudget) || !errors.As(err, &auth) {
		t.Fatalf("second request must end with the budget error and its original -10: %v", err)
	}
	if fetches != 1 || calls != callsBefore+1 {
		t.Fatalf("the budget did not hold: fetches=%d, calls after the first request=%d", fetches, calls-callsBefore)
	}
	stored, getErr := app.db.GetAccount(context.Background(), acc.ID)
	if getErr != nil || accountStatus(stored) != "alive" || !stored.SameAuthorization(acc) {
		t.Fatal("a refused recovery changed the account")
	}
}

// A forced refresh is gated too, and a refused one leaves the cached session
// alone instead of invalidating it for a repair that will not happen.
func TestLoginBudgetStopsTheForcedRefreshAndKeepsTheSession(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	app.pool.SetLoginBudget(1, time.Hour)
	acc := insertKeepAliveTestAccount(t, app, "budget-refresh", time.Now().Add(time.Hour))
	refreshes := 0
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		refreshes++
		creds.ExpiresAt = time.Now().Add(2 * time.Hour).Unix()
		return creds, nil
	}
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) { return "buffer", nil }
	synthetic := errors.New("synthetic upstream failure")
	fail := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, synthetic
	}

	_, _ = app.invokeWXApp(context.Background(), acc, "wx-one", nil, fail)
	if refreshes != 1 {
		t.Fatalf("the first failing request should have refreshed once, got %d", refreshes)
	}
	// The refresh rotated the stored credentials; the next request must carry the
	// current account, as a real caller loads it fresh on every request.
	acc, err := app.db.GetAccount(context.Background(), acc.ID)
	if err != nil {
		t.Fatal(err)
	}
	_, err = app.invokeWXApp(context.Background(), acc, "wx-two", nil, fail)
	if !errors.Is(err, errLoginBudget) || !errors.Is(err, synthetic) {
		t.Fatalf("second failing request: %v", err)
	}
	if refreshes != 1 {
		t.Fatalf("a refresh ran although the budget was used up: %d", refreshes)
	}
}

// tuning.json reaches the running pool through the pacer's change hook, and
// removing it restores the configured values.
func TestTuningFileChangesTheLoginBudgetOfARunningApp(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	file := filepath.Join(app.resources.DB, "tuning.json")
	now := time.Unix(1_700_000_000, 0)
	app.pacer.now = func() time.Time { return now }
	var seen []pacingSettings
	app.pacer.onChange = func(s pacingSettings) {
		seen = append(seen, s)
		app.pool.SetLoginBudget(s.LoginBudget, s.LoginWindow)
	}
	tick := func() {
		now = now.Add(2 * time.Second)
		app.pacer.reserve(1)
	}

	if err := os.WriteFile(file, []byte(`{"login_budget": 1, "login_window_ms": 60000}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(file, now.Add(time.Second), now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	tick()
	if len(seen) != 1 || seen[0].LoginBudget != 1 || seen[0].LoginWindow != time.Minute {
		t.Fatalf("settings seen after writing the file: %+v", seen)
	}
	if !app.pool.AllowLogin(41, "") || app.pool.AllowLogin(41, "") {
		t.Fatal("the pool did not adopt a budget of one")
	}

	if err := os.Remove(file); err != nil {
		t.Fatal(err)
	}
	tick()
	if len(seen) != 2 || seen[1].LoginBudget != app.cfg.LoginBudget {
		t.Fatalf("removing the file must restore the configured budget: %+v", seen)
	}
}
