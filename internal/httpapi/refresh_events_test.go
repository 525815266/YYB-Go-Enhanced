package httpapi

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

// Credential refreshes asked for on demand or by hand must be countable apart
// from the background loop, which reports its own keepalive.* events.
func TestOnDemandAndManualRefreshesAreRecordedWithTheirSource(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	background := insertKeepAliveTestAccount(t, app, "openid-refresh-background", time.Now().Add(10*time.Minute))
	manual := insertKeepAliveTestAccount(t, app, "openid-refresh-manual", time.Now().Add(10*time.Minute))
	onDemand := insertKeepAliveTestAccount(t, app, "openid-refresh-on-demand", time.Now().Add(10*time.Minute))
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		if creds.OpenID == onDemand.OpenID {
			return protocol.LoginBufferCredentials{}, errors.New("synthetic refresh failure")
		}
		creds.AccessToken = "access-new"
		creds.ExpiresAt = time.Now().Add(2 * time.Hour).Unix()
		return creds, nil
	}

	if _, refreshed, err := app.refreshAccount(ctx, background, false); err != nil || !refreshed {
		t.Fatalf("background refresh: refreshed=%v err=%v", refreshed, err)
	}
	if _, refreshed, err := app.refreshAccount(withRefreshSource(ctx, eventlog.SourceManual), manual, false); err != nil || !refreshed {
		t.Fatalf("manual refresh: refreshed=%v err=%v", refreshed, err)
	}
	if _, refreshed, err := app.refreshAccount(withRefreshSource(ctx, eventlog.SourceOnDemand), onDemand, false); err == nil || refreshed {
		t.Fatalf("on-demand refresh should have failed: refreshed=%v err=%v", refreshed, err)
	}
	// Nothing is due, so no attempt is made and nothing is recorded.
	if _, refreshed, err := app.refreshAccount(withRefreshSource(ctx, eventlog.SourceManual), manual, false); err != nil || refreshed {
		t.Fatalf("not-due refresh: refreshed=%v err=%v", refreshed, err)
	}

	app.events.Close() // flush the asynchronous queue
	entries, err := app.db.ListSystemLogs(ctx, store.SystemLogFilter{Limit: 100})
	if err != nil {
		t.Fatal(err)
	}
	var refreshed, failed []store.SystemLog
	for _, entry := range entries {
		switch entry.Event {
		case "account.refreshed":
			refreshed = append(refreshed, entry)
		case "account.refresh_failed":
			failed = append(failed, entry)
		}
	}
	if len(refreshed) != 1 || refreshed[0].AccountID != manual.ID || !strings.Contains(string(refreshed[0].Details), `"source":"manual"`) {
		t.Fatalf("manual refresh not recorded once with its source: %+v", refreshed)
	}
	if len(failed) != 1 || failed[0].AccountID != onDemand.ID || !strings.Contains(string(failed[0].Details), `"source":"on_demand"`) {
		t.Fatalf("failed on-demand refresh not recorded once with its source: %+v", failed)
	}
}

// A forced refresh whose OAuth step succeeds but whose login-buffer step fails
// must not look like a clean success in the event log.
func TestPartialRefreshIsRecordedAsRefreshedAndFailed(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	acc := insertKeepAliveTestAccount(t, app, "openid-refresh-partial", time.Now().Add(10*time.Minute))
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		creds.AccessToken = "access-new"
		creds.ExpiresAt = time.Now().Add(2 * time.Hour).Unix()
		return creds, nil
	}
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
		return "", errors.New("synthetic ticket failure")
	}
	status, refreshed, err := app.refreshAccountWithPolicy(withRefreshSource(ctx, eventlog.SourceOnDemand), acc, true, "", false, true)
	if !refreshed || err == nil || status != "alive" {
		t.Fatalf("expected a renewed but incomplete refresh: status=%q refreshed=%v err=%v", status, refreshed, err)
	}
	app.events.Close()
	entries, err := app.db.ListSystemLogs(ctx, store.SystemLogFilter{Limit: 50})
	if err != nil {
		t.Fatal(err)
	}
	var refreshedEvents, failedEvents int
	for _, entry := range entries {
		switch entry.Event {
		case "account.refreshed":
			refreshedEvents++
		case "account.refresh_failed":
			failedEvents++
		}
	}
	if refreshedEvents != 1 || failedEvents != 1 {
		t.Fatalf("partial refresh recorded refreshed=%d failed=%d, want 1 and 1", refreshedEvents, failedEvents)
	}
}
