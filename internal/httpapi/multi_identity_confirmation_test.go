package httpapi

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"

	"yyb_go/internal/protocol"
	"yyb_go/internal/qbbrowser"
	"yyb_go/internal/qr"
	"yyb_go/internal/store"
)

func TestMultiLinkExplicitIdentityConfirmationKeepsOriginalFleet(t *testing.T) {
	for _, product := range []string{"ldplayer", "qbbrowser"} {
		t.Run(product, func(t *testing.T) {
			app := newKeepAliveTestApp(t)
			defer app.Close()
			ctx := context.Background()
			base, err := app.db.UpsertAccount(ctx, "synthetic-root", "root-ticket", nil, nil, nil, map[string]any{"unionid": "root-platform-identity"}, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			token, link := seedMultiLink(t, app, "update", base, nil)
			creds := &protocol.LoginBufferCredentials{Provider: product, OpenID: product + ":synthetic-new", QBBrowser: qbbrowser.Credentials{OpenID: "synthetic-new", UnionID: "another-platform-identity", QBAccessToken: "synthetic"}}
			app.qrSessions["manual"] = &qrLoginSession{Session: &qr.Session{ID: "manual", Product: product, Credentials: creds, LoginBuffer: "fresh-ticket"}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash}
			prepares := 0
			app.prepareScanSession = func(context.Context, *store.WechatAccount, string, bool) error { prepares++; return nil }
			w := multiLinkRequest(app, token, "/qr/manual/confirm")
			if w.Code != 422 || !strings.Contains(w.Body.String(), `"identity_confirmation_required":true`) {
				t.Fatal(w.Code, w.Body.String())
			}
			rows, _ := app.db.ListAccounts(ctx)
			if len(rows) != 1 || prepares != 0 {
				t.Fatal("unconfirmed scan wrote or logged in")
			}
			w = httptest.NewRecorder()
			app.handleAccountLink(w, httptest.NewRequest("POST", "/account-link/"+token+"/qr/manual/confirm", strings.NewReader(`{"confirm_same_wechat":true}`)))
			if w.Code != 200 {
				t.Fatal(w.Code, w.Body.String())
			}
			members, _ := app.db.FleetMembers(ctx, base.ID)
			if len(members) != 2 {
				t.Fatal("confirmed channel left independent")
			}
			member := members[0]
			if member.ID == base.ID {
				member = members[1]
			}
			if member.FleetPrimaryID == nil || *member.FleetPrimaryID != base.ID || member.UserInfo["identity_binding"] != "operator_confirmed" {
				t.Fatal("lost anchor or confirmation provenance")
			}
			original, _ := app.db.GetAccount(ctx, base.ID)
			if original.LoginBuffer != "root-ticket" || original.OpenID != base.OpenID {
				t.Fatal("primary credentials replaced")
			}
			w = multiLinkRequest(app, token, "/qr/manual/confirm")
			rows, _ = app.db.ListAccounts(ctx)
			if w.Code != 200 || len(rows) != 2 || product == "ldplayer" && prepares != 1 {
				t.Fatal("retry created another ID or replayed login")
			}
		})
	}
}

func TestMultiLinkQQPrimaryIdentityRules(t *testing.T) {
	for _, tc := range []struct {
		name, product, openID string
		consent               bool
		want                  int
	}{
		{"stable same channel without unionid", "qbbrowser", "qbbrowser:synthetic-root", false, 200},
		{"different same channel cannot be forced", "qbbrowser", "qbbrowser:synthetic-other", true, 409},
		{"QQ primary cross channel requires consent", "ldplayer", "ldplayer:synthetic-new", false, 422},
		{"QQ primary cross channel explicit consent", "ldplayer", "ldplayer:synthetic-new", true, 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			app := newKeepAliveTestApp(t)
			defer app.Close()
			base, err := app.db.UpsertAccount(context.Background(), "qbbrowser:synthetic-root", "root-ticket", nil, nil, nil, nil, map[string]any{"provider": "qbbrowser"}, nil)
			if err != nil {
				t.Fatal(err)
			}
			token, link := seedMultiLink(t, app, "update", base, nil)
			creds := &protocol.LoginBufferCredentials{Provider: tc.product, OpenID: tc.openID, QBBrowser: qbbrowser.Credentials{OpenID: strings.TrimPrefix(tc.openID, "qbbrowser:"), QBAccessToken: "synthetic"}}
			app.qrSessions["identity-rule"] = &qrLoginSession{Session: &qr.Session{ID: "identity-rule", Product: tc.product, Credentials: creds, LoginBuffer: "fresh-ticket"}, Client: app.qr, AccountLinkID: link.ID, AccountLinkHash: link.TokenHash}
			app.prepareScanSession = func(context.Context, *store.WechatAccount, string, bool) error { return nil }
			body := `{}`
			if tc.consent {
				body = `{"confirm_same_wechat":true}`
			}
			w := httptest.NewRecorder()
			app.handleAccountLink(w, httptest.NewRequest("POST", "/account-link/"+token+"/qr/identity-rule/confirm", strings.NewReader(body)))
			if w.Code != tc.want {
				t.Fatal(w.Code, w.Body.String())
			}
			if tc.want != 200 {
				rows, _ := app.db.ListAccounts(context.Background())
				if len(rows) != 1 {
					t.Fatal("rejected identity changed accounts")
				}
			}
		})
	}
}
