package protocol

import (
	"encoding/json"
	"testing"
	"time"
	"yyb_go/internal/qbbrowser"
)

func TestQQStandaloneLifetimeAndCredentialsRoundTrip(t *testing.T) {
	for _, expires := range []int64{0, time.Now().Add(time.Hour).Unix()} {
		c := LoginBufferCredentials{Provider: "qbbrowser", OpenID: "qbbrowser:synthetic", AccessToken: "access", RefreshToken: "refresh", LoginType: "WX", QBBrowser: qbbrowser.Credentials{OpenID: "synthetic", QBAccessToken: "access", WXRefreshToken: "refresh"}, ExpiresAt: expires, ExpirySource: "upstream", RefreshTokenObservedAt: 123}
		if expires > 0 {
			c.ExpiresIn = 7200
		}
		raw, _ := json.Marshal(c.ToMap())
		var values map[string]any
		if err := json.Unmarshal(raw, &values); err != nil {
			t.Fatal(err)
		}
		if got := CredentialsFromMap(values); got != c {
			t.Fatal("QQ standalone credentials or upstream lifetime changed during persistence")
		}
	}
	c := LoginBufferCredentials{Provider: "qbbrowser", QBBrowser: qbbrowser.Credentials{OpenID: "synthetic", QBAccessToken: "access", WXRefreshToken: "refresh"}, ExpiresAt: time.Now().Add(-time.Minute).Unix()}
	if !c.Expired(0) {
		t.Fatal("native access expiry ignored")
	}
	c.ExpiresAt = 0
	if c.Expired(0) {
		t.Fatal("unknown upstream lifetime became expired")
	}
}
