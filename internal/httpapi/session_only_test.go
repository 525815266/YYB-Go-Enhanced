package httpapi

import (
	"context"
	"errors"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func putEligibleSession(t *testing.T, app *App, acc *store.WechatAccount) {
	t.Helper()
	blob := map[string]any{
		"session":   map[string]any{"uin": 1234, "send_key": "synthetic-send", "recv_key": "synthetic-recv"},
		"psk_entry": map[string]any{"psk_type": 1, "pre_shared_key": "001122", "ticket_entry": "aabbcc", "expired_at": time.Now().Add(time.Hour).Unix()},
	}
	if err := app.db.PutSession(context.Background(), acc.ID, nil, blob, time.Now().Add(time.Hour).Unix(), ""); err != nil {
		t.Fatal(err)
	}
}

func TestOAuthRejectionRetainsOnlyExistingSession(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	acc := insertKeepAliveTestAccount(t, app, "synthetic-layered-account", time.Now().Add(time.Minute))
	scanTime := time.Now().Add(-time.Hour).Unix()
	acc.Credentials["refresh_token_observed_at"] = scanTime
	if err := app.db.SetAccountCredential(ctx, acc.ID, acc.LoginBuffer, acc.Credentials); err != nil {
		t.Fatal(err)
	}
	acc, _ = app.db.GetAccount(ctx, acc.ID)
	putEligibleSession(t, app, acc)
	calls := 0
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		calls++
		return protocol.LoginBufferCredentials{}, &protocol.RefreshRejectedError{Code: -109, Message: "error 42007 | 42007"}
	}
	status, renewed, err := app.refreshAccount(ctx, acc, false)
	if status != store.StatusSessionOnly || renewed || err == nil {
		t.Fatalf("OAuth rejection: status=%s renewed=%t error=%v", status, renewed, err)
	}
	current, err := app.db.GetAccount(ctx, acc.ID)
	if err != nil || !current.SessionOnly() || len(current.Credentials) != 1 || current.LoginBuffer != "" || !current.Public().RescanRecommended {
		t.Fatal("rejected OAuth credentials retained or limited state hidden")
	}
	if current.Public().RefreshTokenObservedAt == nil || *current.Public().RefreshTokenObservedAt != scanTime || current.Public().CredentialExpiresAt != nil {
		t.Fatal("scan timestamp changed or a rejected credential countdown survived")
	}
	if !app.pool.HasReusableSession(ctx, acc.ID, "") {
		t.Fatal("OAuth rejection deleted the separate eligible protocol session")
	}
	app.refreshDueAccounts(ctx)
	_, _, _ = app.refreshAccount(ctx, current, true)
	if calls != 1 {
		t.Fatal("rejected OAuth credentials were retried")
	}
	result, err := app.invokeWXApp(ctx, current, "synthetic-target", nil,
		func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
			return map[string]any{"code": "synthetic-code"}, nil
		})
	if err != nil || result["code"] != "synthetic-code" {
		t.Fatal("limited account could not use its existing session")
	}
	// A failed application must never restart OAuth recovery from limited mode.
	_, err = app.invokeWXApp(ctx, current, "synthetic-target", nil,
		func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
			return nil, &protocol.ShortlinkRejectedError{Code: -14}
		})
	if err == nil || calls != 1 || !app.pool.HasReusableSession(ctx, acc.ID, "") {
		t.Fatal("limited application failure reset shared state")
	}
	// Once no cache exists, fail terminally rather than log in with stale data.
	if err := app.db.InvalidateAccountSessions(ctx, acc.ID); err != nil {
		t.Fatal(err)
	}
	_, err = app.invokeWXApp(ctx, current, "synthetic-target", nil, app.invokeGetCode)
	var expired accountExpiredError
	if !errors.As(err, &expired) || calls != 1 {
		t.Fatal("missing session attempted credential recovery")
	}
	final, _ := app.db.GetAccount(ctx, acc.ID)
	if accountStatus(final) != "expired" {
		t.Fatal("missing session left an unusable account in limited state")
	}
}

func TestManualExpiryStillRemovesRetainedSession(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	acc := insertKeepAliveTestAccount(t, app, "synthetic-manual-expiry", time.Now().Add(time.Hour))
	putEligibleSession(t, app, acc)
	if _, err := app.db.ApplyCredentialRefresh(ctx, acc, "", nil, store.StatusSessionOnly); err != nil {
		t.Fatal(err)
	}
	if err := app.setAccountStatus(ctx, acc.ID, "expired"); err != nil {
		t.Fatal(err)
	}
	if app.pool.HasReusableSession(ctx, acc.ID, "") {
		t.Fatal("manual expiry retained a session")
	}
}
