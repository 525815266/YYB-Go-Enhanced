package gameassist

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestParseExchangeEnvelopeCamelCase(t *testing.T) {
	now := time.Now().Unix()
	body := []byte(`{"code":0,"msg":"","userInfo":{"username":"测试用户","openId":"oLvh-0xTojUQuDwUDkCG17ZRcZ6E",` +
		`"accessToken":"1_abc","refreshToken":"1_def","loginType":"WX",` +
		`"scope":"snsapi_login,snsapi_runtime_pcsdk","expireTime":` + itoa(now+3600) + `},"extInfo":null}`)
	creds, err := parseExchange(body)
	if err != nil {
		t.Fatalf("parseExchange: %v", err)
	}
	if creds.OpenID != "oLvh-0xTojUQuDwUDkCG17ZRcZ6E" || creds.AccessToken != "1_abc" || creds.RefreshToken != "1_def" {
		t.Fatalf("unexpected credentials: %+v", creds)
	}
	if creds.LoginType != "WX" || creds.Nickname != "测试用户" {
		t.Fatalf("unexpected identity fields: %+v", creds)
	}
	if creds.ExpiresAt != now+3600 {
		t.Fatalf("expireTime not honored: %d", creds.ExpiresAt)
	}
	if err := creds.Validate(); err != nil {
		t.Fatalf("validate: %v", err)
	}
}

func TestParseExchangeFlatLoginState(t *testing.T) {
	// Shape observed via SetLoginParam in GameAssist.log: no envelope, tokens at top level.
	body := []byte(`{"username":"Boom","openId":"oLvh-flat","accessToken":"1_flat",` +
		`"refreshToken":"1_flatr","loginType":"WX","scope":"snsapi_login,snsapi_runtime_pcsdk","expireTime":0}`)
	creds, err := parseExchange(body)
	if err != nil {
		t.Fatalf("parseExchange: %v", err)
	}
	if creds.OpenID != "oLvh-flat" || creds.AccessToken != "1_flat" || creds.RefreshToken != "1_flatr" {
		t.Fatalf("unexpected credentials: %+v", creds)
	}
	if creds.ExpiresIn != 0 || creds.ExpiresAt != 0 {
		t.Fatal("parser must leave missing lifetime unset for callback cookies")
	}
}

func TestParseExchangePreservesPastExpiry(t *testing.T) {
	expiry := time.Now().Add(-time.Hour).Unix()
	creds, err := parseExchange([]byte(`{"openId":"synthetic","accessToken":"synthetic","expireTime":` + itoa(expiry) + `}`))
	if err != nil || creds.ExpiresAt != expiry {
		t.Fatal("past expiry must not become a fresh authorization window")
	}
}

func TestExchangeCodeLifetimeSources(t *testing.T) {
	for _, cookieLifetime := range []string{"", "3600"} {
		t.Run("cookie_"+cookieLifetime, func(t *testing.T) {
			client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
				h := http.Header{}
				if cookieLifetime != "" {
					h.Add("Set-Cookie", "expires_in="+cookieLifetime+"; Path=/")
				}
				return &http.Response{StatusCode: 200, Header: h, Request: r,
					Body: io.NopCloser(strings.NewReader(`{"openId":"synthetic","accessToken":"synthetic"}`))}, nil
			})}
			c, err := ExchangeCode(context.Background(), client, "synthetic", "1")
			want := int64(DefaultRefreshInterval)
			if cookieLifetime != "" {
				want = 3600
			}
			if err != nil || c.ExpiresIn != want || c.ExpiresAt < time.Now().Unix()+want-5 {
				t.Fatalf("lifetime source not applied: lifetime=%d err=%v", c.ExpiresIn, err)
			}
		})
	}
}

func TestParseExchangeSnakeCaseFallback(t *testing.T) {
	body := []byte(`{"code":0,"user_info":{"open_id":"o-snake","access_token":"1_snake",` +
		`"refresh_token":"1_snaker","login_type":"WX"}}`)
	creds, err := parseExchange(body)
	if err != nil {
		t.Fatalf("parseExchange: %v", err)
	}
	if creds.OpenID != "o-snake" || creds.AccessToken != "1_snake" || creds.RefreshToken != "1_snaker" {
		t.Fatalf("snake_case fallback failed: %+v", creds)
	}
}

func TestParseExchangeRejectsBusinessCode(t *testing.T) {
	for _, body := range []string{
		`{"code":-109,"msg":"RC_PARAMS_INVALID","userInfo":null,"extInfo":null}`,
		`{"code":"-107","msg":"RC_NO_AUTH_ERR","user_info":null}`,
	} {
		_, err := parseExchange([]byte(body))
		var rejected *ExchangeRejectedError
		if !errors.As(err, &rejected) {
			t.Fatalf("expected ExchangeRejectedError for %s, got %v", body, err)
		}
		if rejected.Code != -109 && rejected.Code != -107 {
			t.Fatalf("unexpected code: %d", rejected.Code)
		}
		if strings.Contains(err.Error(), "RC_") {
			t.Fatalf("error must not carry upstream text: %v", err)
		}
	}
}

func TestParseExchangeMalformed(t *testing.T) {
	if _, err := parseExchange([]byte(`not-json`)); err == nil {
		t.Fatal("expected error for non-JSON body")
	}
	if _, err := parseExchange([]byte(`{"code":"abc"}`)); err == nil {
		t.Fatal("expected error for non-integer code")
	}
}

func TestValidateScope(t *testing.T) {
	if err := validateScope(""); err != nil {
		t.Fatalf("empty scope must pass: %v", err)
	}
	if err := validateScope("snsapi_login,snsapi_runtime_pcsdk"); err != nil {
		t.Fatalf("official scope must pass: %v", err)
	}
	if err := validateScope("snsapi_login"); err == nil {
		t.Fatal("missing runtime_pcsdk must fail")
	}
}

func TestFillFromCookies(t *testing.T) {
	jar, _ := cookiejar.New(nil)
	target, _ := url.Parse(OAuthRedirectURI)
	jar.SetCookies(target, []*http.Cookie{
		{Name: "openid", Value: "o-cookie"},
		{Name: "accesstoken", Value: "1-cookie"},
		{Name: "refreshtoken", Value: "1-cookier"},
	})
	creds := Credentials{LoginType: "WX"}
	creds.fillFromCookies(jar, OAuthRedirectURI+"?code=x")
	if creds.OpenID != "o-cookie" || creds.AccessToken != "1-cookie" || creds.RefreshToken != "1-cookier" {
		t.Fatalf("cookie fallback failed: %+v", creds)
	}
	creds = Credentials{OpenID: "o-json", AccessToken: "1-json"}
	creds.fillFromCookies(jar, OAuthRedirectURI)
	if creds.OpenID != "o-json" || creds.AccessToken != "1-json" {
		t.Fatalf("cookies must not override parsed values: %+v", creds)
	}
}

func TestExchangeCodeInputValidation(t *testing.T) {
	if _, err := ExchangeCode(context.Background(), nil, "  ", "49.57735723381141"); err == nil {
		t.Fatal("empty code must fail")
	}
	if _, err := ExchangeCode(context.Background(), nil, "1_x", "web"); err == nil {
		t.Fatal("non-numeric state must fail: the gateway rejects it with -109")
	}
}

func TestIsNumericState(t *testing.T) {
	for _, ok := range []string{"49.57735723381141", "7", "0.5", "100.00000000000001"} {
		if !isNumericState(ok) {
			t.Fatalf("%q should be numeric", ok)
		}
	}
	for _, bad := range []string{"", "web", ".5", "5.", "1..2", "0x10", strings.Repeat("1", 40)} {
		if isNumericState(bad) {
			t.Fatalf("%q should not be numeric", bad)
		}
	}
}

func TestRandomStateIsNumeric(t *testing.T) {
	for i := 0; i < 5; i++ {
		if !isNumericState(RandomState()) {
			t.Fatalf("RandomState produced %q", RandomState())
		}
	}
}

func itoa(n int64) string {
	return strconv.FormatInt(n, 10)
}
