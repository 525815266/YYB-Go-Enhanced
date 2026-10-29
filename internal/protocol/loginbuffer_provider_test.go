package protocol

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/pcmgr"
)

func TestCredentialProvidersRoundTripWithoutMixing(t *testing.T) {
	c := LoginBufferCredentials{Provider: "pcmgr", OpenID: "pcmgr:42", PCMgr: pcmgr.Credentials{AccountID: 42, LoginKey: "test", ClientGUID: strings.Repeat("a", 32), RenewedAt: 100}, RefreshTokenObservedAt: 99}
	raw, err := json.Marshal(c.ToMap())
	if err != nil {
		t.Fatal(err)
	}
	var values map[string]any
	if err := json.Unmarshal(raw, &values); err != nil {
		t.Fatal(err)
	}
	if got := CredentialsFromMap(values); got != c && (got.PCMgr != c.PCMgr || got.Provider != c.Provider || got.OpenID != c.OpenID || got.RefreshTokenObservedAt != 99) {
		t.Fatal("provider credentials lost")
	}
	if c.Expired(time.Minute) {
		t.Fatal("renew schedule misidentified as credential expiry")
	}
	legacy := LoginBufferCredentials{OpenID: "test-legacy", AccessToken: "access", RefreshToken: "refresh"}.ToMap()
	if _, ok := legacy["pcmgr"]; ok {
		t.Fatal("legacy credentials contain native keys")
	}
	if CredentialsFromMap(legacy).Provider != "" {
		t.Fatal("legacy provider changed")
	}
}
