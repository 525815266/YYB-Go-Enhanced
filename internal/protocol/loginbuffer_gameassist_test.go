package protocol

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

// GameAssist shares the YYB login-buffer request path end to end; only the
// provider marker differs, so the gateway sees the identical pc_yyb_auth call.
func TestFetchLoginBufferAcceptsGameAssistProvider(t *testing.T) {
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		if req.URL.String() != loginBufferURL {
			t.Fatalf("unexpected endpoint: %s", req.URL)
		}
		if req.Header.Get("Ual-Access-Businessid") != loginBusinessID {
			t.Fatal("expected pc_yyb_auth business header")
		}
		body, _ := io.ReadAll(req.Body)
		if want := sign(string(body), req.Header.Get("Ual-Access-Nonce"), req.Header.Get("Ual-Access-Timestamp"), loginAccessKey); req.Header.Get("Ual-Access-Signature") != want {
			t.Fatal("signature mismatch")
		}
		if cookie := req.Header.Get("Cookie"); cookie != "openid=o-ga; accesstoken=1_ga; refreshtoken=1_gar" {
			t.Fatalf("unexpected cookie header: %s", cookie)
		}
		return credentialTestResponse(`{"code":0,"ext_info":{"list_s":{"login_buffer":{"value":["CgUIARICGhoEMTIzNA=="]}}}}`), nil
	})}, time.Second)
	creds := LoginBufferCredentials{Provider: "gameassist", OpenID: "o-ga", AccessToken: "1_ga", RefreshToken: "1_gar"}
	buffer, err := client.FetchLoginBuffer(context.Background(), creds)
	if err != nil || buffer == "" {
		t.Fatalf("gameassist login buffer fetch failed: %v", err)
	}
}

// GameAssist credentials are refreshed and checked on the syzs endpoints under
// the syzslogin business, not on the pc_yyb ones. A refresh on the pc_yyb route
// answered "invalid refresh_token" as soon as the access token had lapsed, which
// is how account 15 lost its authorization twice on 2026-11-04.
type syzsGateway struct {
	t     *testing.T
	calls []string
	sent  []map[string]string
	reply map[string]string // endpoint -> response body
}

func (g *syzsGateway) RoundTrip(req *http.Request) (*http.Response, error) {
	g.calls = append(g.calls, req.URL.String())
	if got := req.Header.Get("Ual-Access-Businessid"); got != syzsBusinessID {
		g.t.Fatalf("%s sent business %q, want %q", req.URL, got, syzsBusinessID)
	}
	body, _ := io.ReadAll(req.Body)
	if want := sign(string(body), req.Header.Get("Ual-Access-Nonce"), req.Header.Get("Ual-Access-Timestamp"), ""); req.Header.Get("Ual-Access-Signature") != want {
		g.t.Fatalf("%s signature mismatch", req.URL)
	}
	var sent struct {
		UserInfo map[string]string `json:"userInfo"`
	}
	if err := json.Unmarshal(body, &sent); err != nil {
		g.t.Fatalf("body is not the camelCase userInfo envelope: %v", err)
	}
	g.sent = append(g.sent, sent.UserInfo)
	answer, ok := g.reply[req.URL.String()]
	if !ok {
		g.t.Fatalf("unexpected endpoint %s", req.URL)
	}
	return credentialTestResponse(answer), nil
}

func gameAssistCreds() LoginBufferCredentials {
	return LoginBufferCredentials{Provider: "gameassist", OpenID: "o-ga", AccessToken: "1_ga", RefreshToken: "1_gar", LoginType: "WX"}
}

func TestRefreshCredentialsGameAssistUsesSyzsRoute(t *testing.T) {
	for name, tc := range map[string]struct {
		answer      string
		wantAccess  string
		wantRefresh string
		wantIn      int64
	}{
		"camelCase answer with new tokens": {
			answer:     `{"code":0,"msg":"","userInfo":{"openId":"o-ga","accessToken":"1_new","refreshToken":"1_newr","loginType":"WX","expiresIn":7200},"extInfo":null}`,
			wantAccess: "1_new", wantRefresh: "1_newr", wantIn: 7200,
		},
		"camelCase answer that only moves the expiry": {
			answer:     `{"code":0,"msg":"","userInfo":{"openId":"o-ga","expiresIn":3600}}`,
			wantAccess: "1_ga", wantRefresh: "1_gar", wantIn: 3600,
		},
		// Captured from the real gateway on 2026-11-04 (lab account, tokens replaced):
		// openId comes back empty, the tokens are the ones sent and expiresIn is a string.
		"real answer, tokens unchanged and expiresIn as a string": {
			answer:     `{"code":0,"msg":"RC_SUCCESS","userInfo":{"uid":"0","loginType":"WX","openId":"","accessToken":"1_ga","refreshToken":"1_gar","guid":"","imei":"","email":"","nickName":"","avatar":"","password":"","newPassword":"","expiresIn":"3600"},"extInfo":null}`,
			wantAccess: "1_ga", wantRefresh: "1_gar", wantIn: 3600,
		},
		"snake_case answer is read as well": {
			answer:     `{"code":0,"user_info":{"open_id":"o-ga","access_token":"1_snake","refresh_token":"1_snaker","expires_in":1800}}`,
			wantAccess: "1_snake", wantRefresh: "1_snaker", wantIn: 1800,
		},
	} {
		t.Run(name, func(t *testing.T) {
			gw := &syzsGateway{t: t, reply: map[string]string{syzsRefreshURL: tc.answer}}
			client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: gw}, time.Second)
			before := time.Now().Unix()
			fresh, err := client.RefreshCredentials(context.Background(), gameAssistCreds())
			if err != nil {
				t.Fatalf("gameassist refresh failed: %v", err)
			}
			if len(gw.calls) != 1 || gw.calls[0] != syzsRefreshURL {
				t.Fatalf("gameassist refresh must use the syzs endpoint only, calls = %v", gw.calls)
			}
			if sent := gw.sent[0]; sent["openId"] != "o-ga" || sent["refreshToken"] != "1_gar" || sent["accessToken"] != "1_ga" || sent["loginType"] != "WX" {
				t.Fatalf("request user info = %v", sent)
			}
			if fresh.AccessToken != tc.wantAccess || fresh.RefreshToken != tc.wantRefresh || fresh.ExpiresIn != tc.wantIn {
				t.Fatalf("refreshed credentials = %+v", fresh)
			}
			if fresh.ExpiresAt < before+tc.wantIn || fresh.ExpiresAt > time.Now().Unix()+tc.wantIn {
				t.Fatalf("expiry %d is not now + %d", fresh.ExpiresAt, tc.wantIn)
			}
			if fresh.Provider != "gameassist" || fresh.RefreshBusinessID != syzsBusinessID {
				t.Fatalf("provider or business lost: %+v", fresh)
			}
		})
	}
}

func TestRefreshCredentialsGameAssistNeedsUserInfoToCountAsRenewal(t *testing.T) {
	for name, answer := range map[string]string{
		"null user info":     `{"code":0,"msg":"","userInfo":null,"extInfo":null}`,
		"empty user info":    `{"code":0,"msg":"","userInfo":{}}`,
		"unknown keys only":  `{"code":0,"msg":"","userInfo":{"foo":"bar"}}`,
		"no user info field": `{"code":0,"msg":""}`,
	} {
		t.Run(name, func(t *testing.T) {
			gw := &syzsGateway{t: t, reply: map[string]string{syzsRefreshURL: answer}}
			client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: gw}, time.Second)
			if _, err := client.RefreshCredentials(context.Background(), gameAssistCreds()); err == nil {
				t.Fatal("an answer without usable user info was taken as a renewal")
			}
		})
	}
}

func TestRefreshCredentialsGameAssistAsksSyzsCheckLoginBeforeGivingUp(t *testing.T) {
	gw := &syzsGateway{t: t, reply: map[string]string{
		syzsRefreshURL:    `{"code":-109,"msg":"RC_PARAMS_INVALID","userInfo":null,"extInfo":null}`,
		syzsCheckLoginURL: `{"code":-109,"msg":"CheckWXToken WXRefresh catch error WXRefresh got error, code: 40030, msg invalid refresh_token, rid: r1, token 1_gar","userInfo":null,"extInfo":null}`,
	}}
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: gw}, time.Second)
	_, err := client.RefreshCredentials(context.Background(), gameAssistCreds())
	if err == nil {
		t.Fatal("a rejected refresh was reported as success")
	}
	if len(gw.calls) != 2 || gw.calls[0] != syzsRefreshURL || gw.calls[1] != syzsCheckLoginURL {
		t.Fatalf("expected refresh then check-login on the syzs endpoints, calls = %v", gw.calls)
	}
	var rejected *RefreshRejectedError
	if !errors.As(err, &rejected) {
		t.Fatalf("error lost its type: %v", err)
	}
	if !strings.Contains(err.Error(), "40030") || strings.Contains(err.Error(), "1_gar") {
		t.Fatalf("expected the WeChat rejection without the token: %v", err)
	}
}

func TestCheckLoginGameAssistUsesSyzsRoute(t *testing.T) {
	gw := &syzsGateway{t: t, reply: map[string]string{syzsCheckLoginURL: `{"code":0,"userInfo":{"openId":"o-ga","accessToken":"1_checked","refreshToken":"1_gar","expiresIn":7200}}`}}
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: gw}, time.Second)
	checked, err := client.CheckLogin(context.Background(), gameAssistCreds())
	if err != nil || checked.AccessToken != "1_checked" || len(gw.calls) != 1 {
		t.Fatalf("check login = %+v, %v, calls %v", checked, err, gw.calls)
	}
}

func TestRefreshCredentialsAppStoreStaysOnPcYYB(t *testing.T) {
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		if req.URL.String() != mainRefreshURL || req.Header.Get("Ual-Access-Businessid") != "pc_yyb" {
			t.Fatalf("appstore refresh moved to %s (%s)", req.URL, req.Header.Get("Ual-Access-Businessid"))
		}
		return credentialTestResponse(`{"code":0,"user_info":{"open_id":"o-as","access_token":"1_new","refresh_token":"1_newr","expires_in":7200}}`), nil
	})}, time.Second)
	fresh, err := client.RefreshCredentials(context.Background(), LoginBufferCredentials{OpenID: "o-as", AccessToken: "1_as", RefreshToken: "1_asr", LoginType: "WX"})
	if err != nil || fresh.AccessToken != "1_new" {
		t.Fatalf("appstore refresh = %+v, %v", fresh, err)
	}
}
