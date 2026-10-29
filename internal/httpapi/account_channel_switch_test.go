package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/qr"
)

func TestUpdateChannelSwitchRequiresConsentAndPreservesAccountID(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	acc, err := app.db.UpsertAccount(ctx, "original-identity", "old", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	request := func(body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		app.handleAccountLinksAPI(w, httptest.NewRequest("POST", "/api/account-links", strings.NewReader(body)))
		return w
	}
	body := `{"kind":"update","ref":"1","product":"pcmgr"}`
	if w := request(body); w.Code != 422 {
		t.Fatal("unconfirmed switch accepted", w.Code)
	}
	links, _ := app.db.ListAccountLinks(ctx, nil)
	if len(links) != 0 {
		t.Fatal("unconfirmed switch allocated link")
	}
	w := request(`{"kind":"update","ref":"1","product":"pcmgr","confirm_switch":true}`)
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	var created struct {
		Data struct {
			URL string `json:"url"`
		}
	}
	if err = json.Unmarshal(w.Body.Bytes(), &created); err != nil {
		t.Fatal(err)
	}
	raw := created.Data.URL[strings.LastIndex(created.Data.URL, "/")+1:]
	link, err := app.accountLinkForToken(ctx, raw)
	if err != nil {
		t.Fatal(err)
	}
	if !link.SwitchConfirmed || link.TargetProduct != "pcmgr" {
		t.Fatal("channel consent lost")
	}
	// Cached fake QR result exercises the full HTTP commit without upstream calls.
	credentials := &protocol.LoginBufferCredentials{Provider: "pcmgr", OpenID: "pcmgr:new-identity", Nickname: "Demo"}
	login := &qrLoginSession{Session: &qr.Session{ID: "switch-test", Product: "pcmgr", LoginBuffer: "new-buffer", Credentials: credentials}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash, SwitchConfirmed: true}
	app.qrSessions["switch-test"] = login
	w = httptest.NewRecorder()
	app.handleAccountLink(w, httptest.NewRequest("POST", "/account-link/"+raw+"/qr/switch-test/confirm", strings.NewReader("{}")))
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	actual, _ := app.db.GetAccount(ctx, acc.ID)
	if actual.OpenID != credentials.OpenID || actual.LoginBuffer != "new-buffer" {
		t.Fatal("original ID not replaced")
	}
	accounts, _ := app.db.ListAccounts(ctx)
	if len(accounts) != 1 {
		t.Fatal("new account allocated")
	}
	// A same-channel update must keep enforcing the original identity.
	raw, hash, err := newAccountLinkToken()
	if err != nil {
		t.Fatal(err)
	}
	link, err = app.db.CreateAccountLink(ctx, hash, "update", acc.ID, nil, actual.OpenID, time.Now().Add(time.Hour).Unix())
	if err != nil {
		t.Fatal(err)
	}
	credentials = &protocol.LoginBufferCredentials{Provider: "pcmgr", OpenID: "pcmgr:wrong-account"}
	login = &qrLoginSession{Session: &qr.Session{ID: "wrong", Product: "pcmgr", LoginBuffer: "wrong", Credentials: credentials}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash}
	app.qrSessions["wrong"] = login
	w = httptest.NewRecorder()
	app.handleAccountLink(w, httptest.NewRequest("POST", "/account-link/"+raw+"/qr/wrong/confirm", strings.NewReader("{}")))
	if w.Code != 409 || !strings.Contains(w.Body.String(), "请使用目标账号") {
		t.Fatal(w.Code, w.Body.String())
	}
	if _, err = app.accountLinkForToken(ctx, raw); err != nil {
		t.Fatal("wrong account consumed link")
	}
	actual, _ = app.db.GetAccount(ctx, acc.ID)
	if actual.OpenID != "pcmgr:new-identity" {
		t.Fatal("wrong account overwrote target")
	}
}

func TestChannelReplacementPreservesAccountOwner(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	user, err := app.auth.CreateUser(ctx, "owner", "Owner", "test-channel-password", "user")
	if err != nil {
		t.Fatal(err)
	}
	acc, err := app.db.UpsertAccount(ctx, "owner-original", "old", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = app.auth.ClaimAccount(ctx, acc.ID, user.ID); err != nil {
		t.Fatal(err)
	}
	link, err := app.db.CreateAccountLink(ctx, "owned-link", "update", acc.ID, &user.ID, acc.OpenID, time.Now().Add(time.Hour).Unix())
	if err != nil {
		t.Fatal(err)
	}
	if _, err = app.db.UpdateAccountFromLink(ctx, link, "pcmgr:owned", "new", nil, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	owner, err := app.auth.AccountOwner(ctx, acc.ID)
	if err != nil || owner != user.ID {
		t.Fatal("owner changed after replacement", err)
	}
}

func TestPanelReferencesMigrateAndRetryAfterFailure(t *testing.T) {
	fake, server := newFakeQingLong(t)
	app, _, ref := newRunsTestApp(t, server.URL)
	ctx := context.Background()
	before, err := app.db.ResolveAccount(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	fake.envs = []qingLongEnv{{ID: 1, Name: "YYB_SERVER", Value: fmt.Sprintf("host@%s\nhost@%d\nmanual@other", before.OpenID, before.ID), Remarks: "custom", Status: 0}}
	fake.mu.Unlock()
	link, err := app.db.CreateAccountLink(ctx, "panel-migrate", "update", before.ID, nil, before.OpenID, time.Now().Add(time.Hour).Unix())
	if err != nil {
		t.Fatal(err)
	}
	after, err := app.db.UpdateAccountFromLink(ctx, link, "pcmgr:panel-new", "new", nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	// Broken panel returns a warning but leaves the successful authorization and
	// durable old reference alias intact for the ordinary Sync action to retry.
	app.qinglong.reconfigure(PanelTypeQingLong, "http://127.0.0.1:1", "id", "secret")
	if warning := app.syncUpdatedAccountReferences(after); warning == "" {
		t.Fatal("panel failure not reported")
	}
	refs, _, _ := app.db.PendingAccountReferences(ctx, after.ID)
	if len(refs) != 1 {
		t.Fatal("lost retry state")
	}
	app.qinglong.reconfigure(PanelTypeQingLong, server.URL, "test-client", "test-secret")
	if _, _, err = app.syncAccountToQingLong(ctx, after); err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	value := fake.envs[0].Value
	fake.mu.Unlock()
	if strings.Contains(value, "@"+before.OpenID) || !strings.Contains(value, "host@pcmgr:panel-new") || !strings.Contains(value, fmt.Sprintf("host@%d", before.ID)) || !strings.Contains(value, "manual@other") {
		t.Fatal("incorrect panel migration", value)
	}
	refs, _, _ = app.db.PendingAccountReferences(ctx, after.ID)
	if len(refs) != 0 {
		t.Fatal("acknowledged migration still pending")
	}
}
