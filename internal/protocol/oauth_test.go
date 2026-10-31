package protocol

import "testing"

func TestBuildOAuthAuthorizeURL(t *testing.T) {
	got := buildOAuthAuthorizeURL(
		"wx2fc566211d016af2",
		"https://lvmall.yilvke.com/buyer/passport/connect/connect/callback/WECHAT?a=1&b=2",
		"snsapi_userinfo",
		"state-value",
		"",
	)
	want := "https://open.weixin.qq.com/connect/oauth2/authorize?appid=wx2fc566211d016af2&redirect_uri=https%3A%2F%2Flvmall.yilvke.com%2Fbuyer%2Fpassport%2Fconnect%2Fconnect%2Fcallback%2FWECHAT%3Fa%3D1%26b%3D2&response_type=code&scope=snsapi_userinfo&state=state-value#wechat_redirect"
	if got != want {
		t.Fatalf("OAuth URL = %q, want %q", got, want)
	}
}

func TestFindOAuthPayload(t *testing.T) {
	base := pbLen(1, pbVar(1, 0))
	payload := append(base, pbLen(6, []byte("https://lvmall.yilvke.com/buyer/passport/connect/connect/callback/WECHAT?code=oauth-code&state=s"))...)
	wrapped := pbLen(3, pbLen(2, payload))

	got := findOAuthPayload(wrapped, 0)
	if string(oauthFirstBytes(got, 6)) != "https://lvmall.yilvke.com/buyer/passport/connect/connect/callback/WECHAT?code=oauth-code&state=s" {
		t.Fatalf("OAuth payload redirect URL = %q", oauthFirstBytes(got, 6))
	}
	if code := oauthQueryValue(string(oauthFirstBytes(got, 6)), "code"); code != "oauth-code" {
		t.Fatalf("OAuth code = %q, want oauth-code", code)
	}
}

func TestOAuthBaseRequestUsesPlaceholderIdentity(t *testing.T) {
	fields := pbParse(oauthBaseRequest())
	if got := string(fields[5].([]byte)); got != "Windows" {
		t.Fatalf("OAuth base request platform = %q, want Windows", got)
	}
}

func TestDefaultOAuthScopeAndState(t *testing.T) {
	if got := defaultOAuthScope(""); got != "snsapi_base" {
		t.Fatalf("default scope = %q, want snsapi_base", got)
	}
	if got := defaultOAuthScope(" snsapi_userinfo "); got != "snsapi_userinfo" {
		t.Fatalf("scope = %q, want snsapi_userinfo", got)
	}
	if got := defaultOAuthState(""); got != "STATE" {
		t.Fatalf("default state = %q, want STATE", got)
	}
}
