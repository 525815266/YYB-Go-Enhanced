package httpapi

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/pcmgr"
	"yyb_go/internal/protocol"
	"yyb_go/internal/qr"
	"yyb_go/internal/store"
)

func TestMultiLinkRenewsExistingChannelWithoutPrimaryUnionID(t *testing.T) {
	for _, product := range []string{"appstore", "pcmgr", "gameassist", "qbbrowser", "ldplayer"} {
		for _, status := range []string{"alive", "session_only", "expired"} {
			t.Run(product+"/"+status, func(t *testing.T) {
				app := newKeepAliveTestApp(t)
				defer app.Close()
				ctx := context.Background()
				// The original primary has no UnionID and cannot be refreshed.
				rootOpenID := "synthetic-root"
				var rootCredentials map[string]any
				if product == "appstore" {
					rootOpenID, rootCredentials = "pcmgr:synthetic-root", map[string]any{"provider": "pcmgr"}
				}
				base, err := app.db.UpsertAccount(ctx, rootOpenID, "root-ticket", nil, nil, nil, nil, rootCredentials, nil)
				if err != nil {
					t.Fatal(err)
				}
				openid := product + ":synthetic-member"
				member, err := app.db.UpsertAccount(ctx, openid, "old-ticket", nil, nil, nil, map[string]any{"unionid": "member-platform-union", "identity_binding": "operator_confirmed"}, map[string]any{"provider": product}, &status)
				if err != nil {
					t.Fatal(err)
				}
				for _, id := range []int64{base.ID, member.ID} {
					if err := app.db.SetAccountFleet(ctx, id, base.ID); err != nil {
						t.Fatal(err)
					}
				}
				if err := app.db.SetAccountRemark(ctx, member.ID, "keep-member-settings"); err != nil {
					t.Fatal(err)
				}
				// Exercise both a primary-created link and a member-created link.
				target := base
				if status == "expired" {
					target = member
				}
				token, hash, _ := newAccountLinkToken()
				link, err := app.db.CreateAccountLinkWithChannels(ctx, hash, "", "update", target.ID, nil, target.OpenID, time.Now().Add(time.Hour).Unix(), product, false, true, &base.ID)
				if err != nil {
					t.Fatal(err)
				}
				app.fetchUserInfo = func(context.Context, protocol.LoginBufferCredentials) (map[string]any, error) {
					t.Fatal("same-channel renewal must not require the primary's profile")
					return nil, errors.New("primary unavailable")
				}
				app.qr.LoginBuffers().HTTPClient().Transport = roundTripFunc(func(*http.Request) (*http.Response, error) {
					return nil, errors.New("synthetic profile unavailable")
				})
				app.prepareScanSession = func(context.Context, *store.WechatAccount, string, bool) error { return nil }
				creds := &protocol.LoginBufferCredentials{Provider: product, OpenID: openid}
				app.qrSessions["renew-member"] = &qrLoginSession{Session: &qr.Session{ID: "renew-member", Product: product, Credentials: creds, LoginBuffer: "fresh-ticket"}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash}
				for i := 0; i < 2; i++ {
					w := multiLinkRequest(app, token, "/qr/renew-member/confirm")
					if w.Code != 200 {
						t.Fatal(w.Code, w.Body.String())
					}
				}
				rows, _ := app.db.ListAccounts(ctx)
				fresh, _ := app.db.GetAccount(ctx, member.ID)
				root, _ := app.db.GetAccount(ctx, base.ID)
				if len(rows) != 2 || fresh.OpenID != openid || fresh.LoginBuffer != "fresh-ticket" || fresh.FleetPrimaryID == nil || *fresh.FleetPrimaryID != base.ID || fresh.Remark == nil || *fresh.Remark != "keep-member-settings" {
					t.Fatal("renewal changed the member ID, binding or settings")
				}
				if root.LoginBuffer != base.LoginBuffer || root.OpenID != base.OpenID {
					t.Fatal("member renewal overwrote primary authorization")
				}
				if fresh.UserInfo["unionid"] != "member-platform-union" || fresh.UserInfo["identity_binding"] != "operator_confirmed" {
					t.Fatal("partial profile erased the existing identity binding")
				}
			})
		}
	}
}

func TestMultiLinkNewChannelMissingIdentityCanConfirmSameQR(t *testing.T) {
	for _, product := range []string{"pcmgr", "gameassist"} {
		t.Run(product, func(t *testing.T) {
			app := newKeepAliveTestApp(t)
			defer app.Close()
			ctx := context.Background()
			base, _ := app.db.UpsertAccount(ctx, "synthetic-root", "root-ticket", nil, nil, nil, map[string]any{"unionid": "root-union"}, nil, nil)
			token, link := seedMultiLink(t, app, "update", base, nil)
			app.qr.LoginBuffers().HTTPClient().Transport = roundTripFunc(func(*http.Request) (*http.Response, error) { return nil, errors.New("synthetic profile unavailable") })
			creds := &protocol.LoginBufferCredentials{Provider: product, OpenID: product + ":synthetic-new"}
			app.qrSessions["new-channel"] = &qrLoginSession{Session: &qr.Session{ID: "new-channel", Product: product, Credentials: creds, LoginBuffer: "fresh-ticket"}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash}
			w := multiLinkRequest(app, token, "/qr/new-channel/confirm")
			rows, _ := app.db.ListAccounts(ctx)
			if w.Code != 422 || !strings.Contains(w.Body.String(), `"identity_confirmation_required":true`) || len(rows) != 1 {
				t.Fatal("missing identity did not offer a non-writing confirmation", w.Code, w.Body.String())
			}
			w = httptest.NewRecorder()
			app.handleAccountLink(w, httptest.NewRequest("POST", "/account-link/"+token+"/qr/new-channel/confirm", strings.NewReader(`{"confirm_same_wechat":true}`)))
			if w.Code != 200 {
				t.Fatal(w.Code, w.Body.String())
			}
			members, _ := app.db.FleetMembers(ctx, base.ID)
			if len(members) != 2 {
				t.Fatal("confirmed channel was not bound to original fleet")
			}
		})
	}
}

func TestMultiLinkExplicitConfirmationCannotOverrideKnownConflict(t *testing.T) {
	for _, existing := range []bool{false, true} {
		app := newKeepAliveTestApp(t)
		ctx := context.Background()
		base, _ := app.db.UpsertAccount(ctx, "synthetic-root", "root-ticket", nil, nil, nil, map[string]any{"unionid": "root-union"}, nil, nil)
		if existing {
			member, _ := app.db.UpsertAccount(ctx, "pcmgr:original-member", "old-ticket", nil, nil, nil, nil, map[string]any{"provider": "pcmgr"}, nil)
			_ = app.db.SetAccountFleet(ctx, base.ID, base.ID)
			_ = app.db.SetAccountFleet(ctx, member.ID, base.ID)
		}
		token, link := seedMultiLink(t, app, "update", base, nil)
		creds := &protocol.LoginBufferCredentials{Provider: "pcmgr", OpenID: "pcmgr:other-wechat", PCMgr: pcmgr.Credentials{UnionID: "different-union"}}
		app.qrSessions["conflict"] = &qrLoginSession{Session: &qr.Session{ID: "conflict", Product: "pcmgr", Credentials: creds, LoginBuffer: "fresh-ticket"}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash}
		w := httptest.NewRecorder()
		app.handleAccountLink(w, httptest.NewRequest("POST", "/account-link/"+token+"/qr/conflict/confirm", strings.NewReader(`{"confirm_same_wechat":true}`)))
		if w.Code != 409 {
			t.Fatal("explicit consent overrode a known identity conflict", w.Code)
		}
		scans, _ := app.db.AccountLinkScans(ctx, link.ID)
		if len(scans) != 0 {
			t.Fatal("conflicting scan was recorded")
		}
		app.Close()
	}
}

func TestMultiLinkExpiredLDPlayerChangedTicketOffersConfirmation(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	base, _ := app.db.UpsertAccount(ctx, "synthetic-root", "root-ticket", nil, nil, nil, map[string]any{"unionid": "root-union"}, nil, nil)
	expired := "expired"
	member, _ := app.db.UpsertAccount(ctx, "ldplayer:old-ticket", "", nil, nil, nil, nil, map[string]any{"provider": "ldplayer"}, &expired)
	_ = app.db.SetAccountFleet(ctx, base.ID, base.ID)
	_ = app.db.SetAccountFleet(ctx, member.ID, base.ID)
	token, link := seedMultiLink(t, app, "update", base, nil)
	creds := &protocol.LoginBufferCredentials{Provider: "ldplayer", OpenID: "ldplayer:new-ticket"}
	app.qrSessions["ld-renew"] = &qrLoginSession{Session: &qr.Session{ID: "ld-renew", Product: "ldplayer", Credentials: creds, LoginBuffer: "fresh-ticket"}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash}
	app.prepareScanSession = func(context.Context, *store.WechatAccount, string, bool) error { return nil }
	w := multiLinkRequest(app, token, "/qr/ld-renew/confirm")
	if w.Code != 422 || !strings.Contains(w.Body.String(), `"identity_confirmation_required":true`) {
		t.Fatal("changed LDPlayer ticket cannot reach confirmation", w.Code, w.Body.String())
	}
	unchanged, _ := app.db.GetAccount(ctx, member.ID)
	if !unchanged.SameAuthorization(member) {
		t.Fatal("unconfirmed ticket replaced expired authorization")
	}
	w = httptest.NewRecorder()
	app.handleAccountLink(w, httptest.NewRequest("POST", "/account-link/"+token+"/qr/ld-renew/confirm", strings.NewReader(`{"confirm_same_wechat":true}`)))
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	updated, _ := app.db.GetAccount(ctx, member.ID)
	rows, _ := app.db.ListAccounts(ctx)
	if len(rows) != 2 || updated.OpenID != creds.OpenID || updated.FleetPrimaryID == nil || *updated.FleetPrimaryID != base.ID {
		t.Fatal("confirmed LDPlayer ticket lost its original member ID or fleet")
	}
}
