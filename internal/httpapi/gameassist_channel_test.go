package httpapi

import (
	"context"
	"testing"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

// A GameAssist account keeps its real WeChat openId, so the provider marker in
// its credentials is the only thing that says which channel it belongs to. On
// 2026-11-04 account 15 had its OAuth refresh rejected, was limited to its
// existing session, lost that marker with the rest of its credentials and from
// then on showed up as a YYB account in the console and in every event while it
// kept serving codes. The marker must survive both ways an account is limited.
func TestGameAssistKeepsItsChannelWhenItsAuthorizationIsRejected(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	acc := insertKeepAliveTestAccount(t, app, "synthetic-gameassist-account", time.Now().Add(time.Minute))
	scanTime := time.Now().Add(-time.Hour).Unix()
	acc.Credentials["provider"] = "gameassist"
	acc.Credentials["refresh_token_observed_at"] = scanTime
	if err := app.db.SetAccountCredential(ctx, acc.ID, acc.LoginBuffer, acc.Credentials); err != nil {
		t.Fatal(err)
	}
	acc, _ = app.db.GetAccount(ctx, acc.ID)
	putEligibleSession(t, app, acc)
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		return protocol.LoginBufferCredentials{}, &protocol.RefreshRejectedError{
			Code: -109, Operation: "check_login",
			Message: "CheckWXToken WXRefresh catch error WXRefresh got error, code: 40030, msg invalid refresh_token, rid: synthetic",
		}
	}

	status, renewed, err := app.refreshAccount(ctx, acc, false)
	if status != store.StatusSessionOnly || renewed || err == nil {
		t.Fatalf("OAuth rejection: status=%s renewed=%t error=%v", status, renewed, err)
	}
	limited, err := app.db.GetAccount(ctx, acc.ID)
	if err != nil || !limited.SessionOnly() {
		t.Fatalf("account was not limited to its session: %v", err)
	}
	if limited.Credentials["provider"] != "gameassist" || len(limited.Credentials) != 2 || limited.Credentials["accesstoken"] != nil || limited.Credentials["refreshtoken"] != nil {
		t.Fatalf("limited credentials = %v, want the marker and the scan time only", limited.Credentials)
	}
	if got := limited.Public().ScanProduct; got != "gameassist" {
		t.Fatalf("limited account shows as %q", got)
	}
	if got := eventlog.ForAccount(limited).Channel; got != "gameassist" {
		t.Fatalf("events for the limited account carry channel %q", got)
	}
	if protocol.ProviderOf(limited) != "gameassist" {
		t.Fatal("protocol layer no longer sees the provider")
	}
	if limited.IsIncomplete() {
		t.Fatal("a limited GameAssist account became a cleanup candidate")
	}

	if err := app.setAccountStatus(ctx, acc.ID, "expired"); err != nil {
		t.Fatal(err)
	}
	expired, err := app.db.GetAccount(ctx, acc.ID)
	if err != nil {
		t.Fatal(err)
	}
	if accountStatus(expired) != "expired" || expired.LoginBuffer != "" || len(expired.Credentials) != 1 || expired.Credentials["provider"] != "gameassist" {
		t.Fatalf("expired account: status=%s credentials=%v", accountStatus(expired), expired.Credentials)
	}
	if expired.Public().ScanProduct != "gameassist" || eventlog.ForAccount(expired).Channel != "gameassist" {
		t.Fatal("expired account lost its channel")
	}
}

// Accounts of the other channels must behave exactly as before: App Store
// accounts carry no marker and end up with nothing, PCMgr accounts are told
// apart by their openId prefix.
func TestExpiryStillWipesCredentialsOfOtherChannels(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	acc := insertKeepAliveTestAccount(t, app, "synthetic-appstore-account", time.Now().Add(time.Minute))
	if err := app.setAccountStatus(ctx, acc.ID, "expired"); err != nil {
		t.Fatal(err)
	}
	expired, err := app.db.GetAccount(ctx, acc.ID)
	if err != nil {
		t.Fatal(err)
	}
	if expired.Credentials != nil || expired.LoginBuffer != "" || expired.Public().ScanProduct != "appstore" {
		t.Fatalf("appstore expiry: credentials=%v product=%s", expired.Credentials, expired.Public().ScanProduct)
	}
}
