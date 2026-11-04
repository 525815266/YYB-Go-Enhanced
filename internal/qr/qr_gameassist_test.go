package qr

import (
	"net/url"
	"strings"
	"testing"

	"yyb_go/internal/gameassist"
)

func TestOAuthURLGameAssist(t *testing.T) {
	raw, err := OAuthURL("gameassist")
	if err != nil {
		t.Fatalf("OAuthURL(gameassist): %v", err)
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Host != "open.weixin.qq.com" || parsed.Path != "/connect/qrconnect" {
		t.Fatalf("unexpected OAuth URL: %s", raw)
	}
	q := parsed.Query()
	if q.Get("appid") != gameassist.OAuthAppID {
		t.Fatalf("unexpected appid: %s", q.Get("appid"))
	}
	if q.Get("scope") != gameassist.OAuthScope {
		t.Fatalf("unexpected scope: %s", q.Get("scope"))
	}
	redirect := q.Get("redirect_uri")
	if !strings.HasPrefix(redirect, gameassist.OAuthRedirectURI+"?") {
		t.Fatalf("unexpected redirect_uri: %s", redirect)
	}
	rq, err := url.ParseQuery(strings.TrimPrefix(redirect, gameassist.OAuthRedirectURI+"?"))
	if err != nil || rq.Get("login_type") != "wx" || rq.Get("domain") != "1" {
		t.Fatalf("unexpected syzs callback params: %s", redirect)
	}
	if q.Get("self_redirect") != "true" || q.Get("fast_login") != "1" {
		t.Fatalf("unexpected qrconnect flags: %s", raw)
	}
}

func TestOAuthURLUnsupportedProductStillRejected(t *testing.T) {
	if _, err := OAuthURL("bogus"); err == nil {
		t.Fatal("unknown product must fail")
	}
	if _, err := OAuthURL(""); err != nil {
		t.Fatalf("empty product must default to appstore: %v", err)
	}
}
