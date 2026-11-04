package protocol

import (
	"context"
	"io"
	"net/http"
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

func TestRefreshCredentialsAcceptsGameAssistProvider(t *testing.T) {
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		if req.URL.String() != mainRefreshURL {
			t.Fatalf("gameassist refresh must follow the default pc_yyb route, got %s", req.URL)
		}
		return credentialTestResponse(`{"code":0,"user_info":{"open_id":"o-ga","access_token":"1_new","refresh_token":"1_newr","expires_in":7200}}`), nil
	})}, time.Second)
	creds := LoginBufferCredentials{Provider: "gameassist", OpenID: "o-ga", AccessToken: "1_ga", RefreshToken: "1_gar"}
	fresh, err := client.RefreshCredentials(context.Background(), creds)
	if err != nil {
		t.Fatalf("gameassist refresh failed: %v", err)
	}
	if fresh.AccessToken != "1_new" || fresh.RefreshToken != "1_newr" {
		t.Fatalf("unexpected refreshed credentials: %+v", fresh)
	}
	if fresh.Provider != "gameassist" {
		t.Fatal("provider marker must survive refresh")
	}
}
