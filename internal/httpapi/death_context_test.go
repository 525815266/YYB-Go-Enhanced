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

func deathEventDetails(t *testing.T, app *App, event string, accountID int64) string {
	t.Helper()
	app.events.Close() // flush the asynchronous queue
	entries, err := app.db.ListSystemLogs(context.Background(), store.SystemLogFilter{Limit: 100})
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.Event == event && entry.AccountID == accountID {
			return string(entry.Details)
		}
	}
	t.Fatalf("no %s event for account %d", event, accountID)
	return ""
}

// When the upstream refuses an account, the event that says so names the
// requests the account had just made, and only its own.
func TestRefusedRefreshEventNamesTheRequestsBeforeIt(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-death-refresh", time.Now().Add(10*time.Minute))
	other := insertKeepAliveTestAccount(t, app, "openid-death-other", time.Now().Add(10*time.Minute))
	app.events.Emit("protocol.succeeded", eventlog.ForAccount(acc).WithTarget("wxaaaaaaaaaaaaaaaa", eventlog.OpGetCode, ""))
	failed := eventlog.ForAccount(acc).WithTarget("wxbbbbbbbbbbbbbbbb", eventlog.OpGetCode, "")
	failed.Rejections = []eventlog.Rejection{{Stage: "shortlink_application", Code: -14}}
	app.events.Emit("protocol.failed", failed)
	app.events.Emit("protocol.failed", eventlog.ForAccount(other).WithTarget("wxcccccccccccccccc", eventlog.OpGetCode, ""))
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		return protocol.LoginBufferCredentials{}, errors.New("synthetic refresh failure")
	}
	if _, refreshed, err := app.refreshAccount(withRefreshSource(context.Background(), eventlog.SourceOnDemand), acc, false); err == nil || refreshed {
		t.Fatalf("the refresh should have failed: refreshed=%v err=%v", refreshed, err)
	}
	details := deathEventDetails(t, app, "account.refresh_failed", acc.ID)
	if !strings.Contains(details, `"recent":[`) || !strings.Contains(details, "wxaaaaaaaaaaaaaaaa") || !strings.Contains(details, "wxbbbbbbbbbbbbbbbb") || !strings.Contains(details, `"code":-14`) {
		t.Fatalf("the event does not name the earlier requests: %s", details)
	}
	if strings.Contains(details, "wxcccccccccccccccc") {
		t.Fatalf("a request of another account leaked into the event: %s", details)
	}
}

// Expiring an account for any reason records the same context.
func TestExpiredEventNamesTheRequestsBeforeIt(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-death-expired", time.Now().Add(10*time.Minute))
	app.events.Emit("protocol.recovery_started", eventlog.ForAccount(acc).WithTarget("wxdddddddddddddddd", eventlog.OpGetCode, ""))
	if err := app.setAccountStatus(context.Background(), acc.ID, "expired"); err != nil {
		t.Fatal(err)
	}
	details := deathEventDetails(t, app, "account.expired", acc.ID)
	if !strings.Contains(details, "wxdddddddddddddddd") || !strings.Contains(details, `"event":"recovery_started"`) {
		t.Fatalf("the expiry event does not name the earlier request: %s", details)
	}
}
