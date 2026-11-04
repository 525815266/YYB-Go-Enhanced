package protocol

import (
	"context"
	"net/http"
	"testing"
	"time"
)

// GameAssist tokens are issued by the syzs business; when the pc_yyb
// user-info endpoint rejects them the caller still gets the locally captured
// profile instead of an error, mirroring the native-channel behavior.
func TestFetchUserInfoGameAssistFallsBackToLocalProfile(t *testing.T) {
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(*http.Request) (*http.Response, error) {
		return credentialTestResponse(`{"code":-109,"msg":"RC_PARAMS_INVALID"}`), nil
	})}, time.Second)
	creds := LoginBufferCredentials{Provider: "gameassist", OpenID: "o-ga", AccessToken: "1_ga", Nickname: "助手用户", LoginType: "WX"}
	info, err := client.FetchUserInfo(context.Background(), creds)
	if err != nil {
		t.Fatalf("gameassist user info must degrade, got error: %v", err)
	}
	if info["nickname"] != "助手用户" || info["openid"] != "o-ga" {
		t.Fatalf("unexpected fallback profile: %+v", info)
	}
}

// The appstore channel keeps the gateway's answer as-is: its tokens come from
// the same pc_yyb business, so the map is the caller's diagnostic.
func TestFetchUserInfoAppStoreKeepsRejectionMap(t *testing.T) {
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(*http.Request) (*http.Response, error) {
		return credentialTestResponse(`{"code":-109,"msg":"RC_PARAMS_INVALID"}`), nil
	})}, time.Second)
	creds := LoginBufferCredentials{OpenID: "o-yyb", AccessToken: "1_yyb"}
	info, err := client.FetchUserInfo(context.Background(), creds)
	if err != nil {
		t.Fatalf("existing behavior returns the rejection map, got error: %v", err)
	}
	if info["code"].(float64) != -109 {
		t.Fatalf("unexpected rejection map: %+v", info)
	}
}
