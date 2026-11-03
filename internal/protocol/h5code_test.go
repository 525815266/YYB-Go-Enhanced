package protocol

import (
	"errors"
	"strings"
	"testing"

	"yyb_go/internal/oauth"
)

func oauthResultForTest(t *testing.T, appID, redirectURI, scope, state string) oauth.Result {
	t.Helper()
	result, err := oauth.Build(oauth.Request{AppID: appID, RedirectURI: redirectURI, Scope: scope, State: state})
	if err != nil {
		t.Fatalf("oauth build: %v", err)
	}
	return result
}

func TestBuildH5OAuthRequestWire(t *testing.T) {
	request := buildH5OAuthRequest("https://open.weixin.qq.com/connect/oauth2/authorize?appid=wx1234")
	base := pbLen(1, nil)
	base = append(base, pbVar(2, 0)...)
	base = append(base, pbLen(3, nil)...)
	base = append(base, pbVar(4, 0)...)
	base = append(base, pbLen(5, []byte("Windows"))...)
	base = append(base, pbVar(6, 0)...)
	body := pbLen(1, base)
	body = append(body, pbLen(2, []byte("https://open.weixin.qq.com/connect/oauth2/authorize?appid=wx1234"))...)
	body = append(body, pbLen(3, nil)...)
	body = append(body, pbVar(4, 4)...)
	if string(request) != string(body) {
		t.Fatal("oauth_authorize request wire changed")
	}
	fields, err := parseH5OAuthFields(body)
	if err != nil {
		t.Fatalf("self-parsed request rejected: %v", err)
	}
	if got := string(fields[2].b); got != "https://open.weixin.qq.com/connect/oauth2/authorize?appid=wx1234" {
		t.Fatalf("authorize URL field mismatch: %s", got)
	}
}

func TestParseH5OAuthResultCallbackValidation(t *testing.T) {
	callback := "https://example.com/login?tenant=one"
	location := "https://example.com/login?tenant=one&code=issued&state=expected"
	for _, tc := range []struct {
		name, got, state string
		ok               bool
	}{
		{"matching", location, "expected", true},
		{"wrong host", "https://other.example/login?tenant=one&code=x&state=expected", "expected", false},
		{"wrong path", "https://example.com/other?tenant=one&code=x&state=expected", "expected", false},
		{"wrong state", location, "other", false},
		{"duplicate state", callback + "&code=x&state=expected&state=other", "expected", false},
		{"missing code", callback + "&state=expected", "expected", false},
		{"empty code", callback + "&code=&state=expected", "expected", false},
		{"changed callback query", "https://example.com/login?tenant=two&code=x&state=expected", "expected", false},
		{"fragment", callback + "&code=x&state=expected#unexpected", "expected", false},
		{"user info", "https://user@example.com/login?tenant=one&code=x&state=expected", "expected", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			code, err := h5OAuthCallbackCode(tc.got, callback, tc.state)
			if tc.ok != (err == nil && code == "issued") {
				t.Fatalf("callback validation mismatch: code=%q err=%v", code, err)
			}
		})
	}
}

func TestParseH5OAuthResultRequiresExplicitBusinessStatus(t *testing.T) {
	callback := "https://example.com/login"
	location := "https://example.com/login?code=issued&state=expected"
	encoded := pbLen(6, []byte(location))
	if _, err := parseH5OAuthResult(encoded, callback, "expected", 0); err == nil || !strings.Contains(err.Error(), "business status") {
		t.Fatalf("location without explicit status accepted: %v", err)
	}
	accepted := append(pbLen(1, pbVar(1, 0)), encoded...)
	code, err := parseH5OAuthResult(accepted, callback, "expected", 0)
	if err != nil || code != "issued" {
		t.Fatalf("explicit zero status rejected: code=%q err=%v", code, err)
	}
}

func TestParseH5OAuthResultBusinessRejection(t *testing.T) {
	callback := "https://example.com/login"
	location := "https://example.com/login?code=issued&state=expected"
	payload := pbLen(1, pbVar(1, 10004))
	payload = append(payload, pbLen(6, []byte(location))...)
	code, err := parseH5OAuthResult(payload, callback, "expected", 0)
	var rejected *ErrH5OAuthBusinessRejected
	if code != "" || !errors.As(err, &rejected) || rejected.Ret != 10004 {
		t.Fatalf("expected business rejection 10004, got code=%q err=%v", code, err)
	}
}

func TestParseH5OAuthResultNestedCallback(t *testing.T) {
	callback := "https://example.com/login"
	location := "https://example.com/login?code=nested&state=expected"
	inner := append(pbLen(1, pbVar(1, 0)), pbLen(6, []byte(location))...)
	outer := pbLen(3, inner)
	code, err := parseH5OAuthResult(outer, callback, "expected", 0)
	if err != nil || code != "nested" {
		t.Fatalf("nested callback not found: code=%q err=%v", code, err)
	}
}

func TestParseH5OAuthResultRejectsGarbage(t *testing.T) {
	if _, err := parseH5OAuthResult([]byte{0x0a, 0x20, 0x01}, "https://example.com/login", "expected", 0); err == nil {
		t.Fatal("truncated payload accepted")
	}
	if _, err := parseH5OAuthResult(nil, "https://example.com/login", "expected", 0); err == nil {
		t.Fatal("empty payload accepted")
	}
}

func TestH5OAuthRedirectRef(t *testing.T) {
	result := oauthResultForTest(t, "wx1234567890abcdef", "https://example.com/login?tenant=one", "snsapi_base", "fixed")
	ref, err := h5OAuthRedirectRef(result)
	if err != nil {
		t.Fatalf("redirect ref: %v", err)
	}
	if ref != "https://example.com/login?tenant=one" {
		t.Fatalf("redirect ref mismatch: %s", ref)
	}
}
