package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"yyb_go/internal/pcmgr"
	"yyb_go/internal/protocol"
	"yyb_go/internal/qbbrowser"
	"yyb_go/internal/qr"
	"yyb_go/internal/store"
)

func TestQQMultiLinkUsesNativeUnionID(t *testing.T) {
	for _, uid := range []string{"", "other-wechat", "same-wechat"} {
		t.Run(uid, func(t *testing.T) {
			app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
			if err != nil {
				t.Fatal(err)
			}
			defer app.Close()
			ctx := context.Background()
			base, err := app.db.UpsertAccount(ctx, "root", "old-buffer", nil, nil, nil, map[string]any{"unionid": "same-wechat"}, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			token, link := seedMultiLink(t, app, "update", base, nil)
			creds := &protocol.LoginBufferCredentials{Provider: "qbbrowser", OpenID: "qbbrowser:synthetic", QBBrowser: qbbrowser.Credentials{OpenID: "synthetic", UnionID: uid, QBAccessToken: "access", WXRefreshToken: "refresh"}}
			app.qrSessions["qq-native"] = &qrLoginSession{Session: &qr.Session{ID: "qq-native", Product: "qbbrowser", Credentials: creds, LoginBuffer: "synthetic-buffer"}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash}
			w := multiLinkRequest(app, token, "/qr/qq-native/confirm")
			rows, _ := app.db.ListAccounts(ctx)
			if uid == "same-wechat" {
				if w.Code != 200 || len(rows) != 2 {
					t.Fatal("QQ native identity not accepted", w.Code)
				}
			} else if (w.Code != 409 && w.Code != 422) || len(rows) != 1 {
				t.Fatal("unverified QQ identity changed accounts", w.Code)
			}
		})
	}
}

func multiLinkRequest(app *App, token, path string) *httptest.ResponseRecorder {
	w := httptest.NewRecorder()
	app.handleAccountLink(w, httptest.NewRequest("POST", "/account-link/"+token+path, strings.NewReader("{}")))
	return w
}

func seedMultiLink(t *testing.T, app *App, kind string, base *store.WechatAccount, owner *int64) (string, *store.AccountLink) {
	t.Helper()
	token, hash, err := newAccountLinkToken()
	if err != nil {
		t.Fatal(err)
	}
	var root *int64
	expected := ""
	if kind == "update" {
		root = &base.ID
		expected = base.OpenID
	}
	link, err := app.db.CreateAccountLinkWithChannels(context.Background(), hash, "", kind, base.ID, owner, expected, time.Now().Add(time.Hour).Unix(), "appstore", false, true, root)
	if err != nil {
		t.Fatal(err)
	}
	return token, link
}

func seedMultiQR(app *App, link *store.AccountLink, sid, openID, unionID string) {
	creds := &protocol.LoginBufferCredentials{Provider: "pcmgr", OpenID: openID, PCMgr: pcmgr.Credentials{UnionID: unionID}}
	app.qrSessions[sid] = &qrLoginSession{Session: &qr.Session{ID: sid, Product: "pcmgr", Credentials: creds, LoginBuffer: "new-buffer"}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash}
}

func TestMultiAccountLinkIdentityProgressRetryAndFinish(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	base, err := app.db.UpsertAccount(ctx, "root", "old-buffer", nil, nil, nil, map[string]any{"unionid": "same-wechat"}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	token, link := seedMultiLink(t, app, "update", base, nil)
	for _, uid := range []string{"", "other-wechat"} {
		sid := "wrong-" + uid
		seedMultiQR(app, link, sid, "pcmgr:wrong", uid)
		w := multiLinkRequest(app, token, "/qr/"+sid+"/confirm")
		if w.Code != 409 && w.Code != 422 {
			t.Fatal(w.Code, w.Body.String())
		}
		accounts, _ := app.db.ListAccounts(ctx)
		if len(accounts) != 1 {
			t.Fatal("wrong or missing unionid wrote an account")
		}
	}
	if w := multiLinkRequest(app, token, "/finish"); w.Code != 409 {
		t.Fatal("empty link finished", w.Code)
	}
	seedMultiQR(app, link, "correct", "pcmgr:correct", "same-wechat")
	// Duplicate confirmations race against the same QR. Both receive the same
	// result, but only one account is allocated and one progress row is written.
	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			w := multiLinkRequest(app, token, "/qr/correct/confirm")
			if w.Code != 200 {
				t.Errorf("confirm %d %s", w.Code, w.Body.String())
			}
		}()
	}
	wg.Wait()
	accounts, _ := app.db.ListAccounts(ctx)
	if len(accounts) != 2 {
		t.Fatal("retry allocated duplicates", len(accounts))
	}
	root, _ := app.db.GetAccount(ctx, base.ID)
	if root.OpenID != base.OpenID || root.LoginBuffer != "old-buffer" {
		t.Fatal("primary authorization replaced")
	}
	members, _ := app.db.FleetMembers(ctx, base.ID)
	if len(members) != 2 {
		t.Fatal("not bound into one fleet")
	}
	scans, _ := app.db.AccountLinkScans(ctx, link.ID)
	if len(scans) != 1 || scans[0].Product != "pcmgr" {
		t.Fatal("progress missing", scans)
	}
	if _, err = app.accountLinkForToken(ctx, token); err != nil {
		t.Fatal("first channel consumed multi link")
	}
	meta := httptest.NewRecorder()
	app.handleAccountLink(meta, httptest.NewRequest("GET", "/account-link/"+token+"/meta", nil))
	if !strings.Contains(meta.Body.String(), `"allow_multi_channel":true`) || !strings.Contains(meta.Body.String(), `"completed_channels":[`) {
		t.Fatal(meta.Body.String())
	}
	if w := multiLinkRequest(app, token, "/finish"); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	if w := multiLinkRequest(app, token, "/qr/correct/confirm"); w.Code != 410 {
		t.Fatal("closed link accepted confirmation", w.Code)
	}
}

func TestMultiAddLinkOwnershipAndExistingAccountRefusal(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite", RequestTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	user, err := app.auth.CreateUser(ctx, "multi-owner", "Owner", "password-for-test", "user")
	if err != nil {
		t.Fatal(err)
	}
	base, err := app.db.UpsertAccount(ctx, "base", "buffer", nil, nil, nil, map[string]any{"unionid": "base-wechat"}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = app.auth.ClaimAccount(ctx, base.ID, user.ID); err != nil {
		t.Fatal(err)
	}
	token, link := seedMultiLink(t, app, "add", base, &user.ID)
	seedMultiQR(app, link, "add", "pcmgr:added", "new-wechat")
	if w := multiLinkRequest(app, token, "/qr/add/confirm"); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	fresh, _ := app.db.GetAccountLink(ctx, link.ID)
	if fresh.MultiPrimaryID == nil || *fresh.MultiPrimaryID == base.ID {
		t.Fatal("add linked new WeChat into template fleet")
	}
	owner, err := app.auth.AccountOwner(ctx, *fresh.MultiPrimaryID)
	if err != nil || owner != user.ID {
		t.Fatal("ownership not inherited", err)
	}
	// An existing independent identity cannot be taken over via a public link,
	// including one owned by this same user.
	other, err := app.db.UpsertAccount(ctx, "pcmgr:other", "old", nil, nil, nil, nil, map[string]any{"provider": "pcmgr"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = app.auth.ClaimAccount(ctx, other.ID, user.ID); err != nil {
		t.Fatal(err)
	}
	seedMultiQR(app, link, "existing", "pcmgr:other", "new-wechat")
	if w := multiLinkRequest(app, token, "/qr/existing/confirm"); w.Code != 409 {
		t.Fatal(w.Code, w.Body.String())
	}
	actual, _ := app.db.GetAccount(ctx, other.ID)
	if actual.LoginBuffer != "old" {
		t.Fatal("existing account overwritten")
	}
}

func TestAccountLinkMultiOptInAndSingleCannotFinish(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	base, err := app.db.UpsertAccount(ctx, "base", "b", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, multi := range []bool{false, true} {
		w := httptest.NewRecorder()
		body := fmt.Sprintf(`{"kind":"update","ref":"%d","allow_multi_channel":%t}`, base.ID, multi)
		app.handleAccountLinksAPI(w, httptest.NewRequest("POST", "/api/account-links", strings.NewReader(body)))
		if w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
		var env struct {
			Data struct {
				URL   string
				Multi bool `json:"allow_multi_channel"`
			}
		}
		if err = json.Unmarshal(w.Body.Bytes(), &env); err != nil {
			t.Fatal(err)
		}
		if env.Data.Multi != multi {
			t.Fatal("opt-in not returned")
		}
		token := env.Data.URL[strings.LastIndex(env.Data.URL, "/")+1:]
		link, err := app.accountLinkForToken(ctx, token)
		if err != nil {
			t.Fatal(err)
		}
		if link.AllowMultiChannel != multi {
			t.Fatal("opt-in not persisted")
		}
		if !multi {
			if r := multiLinkRequest(app, token, "/finish"); r.Code != 422 {
				t.Fatal("single link accepted multi finish", r.Code)
			}
		}
		if err = app.db.DeleteAccountLink(ctx, link.ID, nil); err != nil {
			t.Fatal(err)
		}
	}
}
