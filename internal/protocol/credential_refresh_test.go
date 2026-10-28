package protocol

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func credentialTestResponse(body string) *http.Response {
	return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body))}
}

func TestRefreshUsesCredentialBusinessAndMatchingSignature(t *testing.T) {
	for _, source := range []string{"", "pc_yyb", "pc_yyb_auth"} {
		t.Run("source="+source, func(t *testing.T) {
			business, endpoint, key := "pc_yyb", mainRefreshURL, ""
			if source == "pc_yyb_auth" {
				business, endpoint, key = loginBusinessID, refreshTokenURL, loginAccessKey
			}
			client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(req *http.Request) (*http.Response, error) {
				body, _ := io.ReadAll(req.Body)
				if req.URL.String() != endpoint || req.Header.Get("Ual-Access-Businessid") != business {
					t.Fatalf("wrong refresh route: %s / %s", req.URL, req.Header.Get("Ual-Access-Businessid"))
				}
				if want := sign(string(body), req.Header.Get("Ual-Access-Nonce"), req.Header.Get("Ual-Access-Timestamp"), key); req.Header.Get("Ual-Access-Signature") != want {
					t.Fatal("signature does not match the selected business")
				}
				if string(body) != `{"userInfo":{"openId":"account","refreshToken":"old-refresh","accessToken":"old-access","loginType":"WX"}}` {
					t.Fatal("unexpected credential request shape")
				}
				return credentialTestResponse(`{"code":0,"user_info":{"open_id":"account","access_token":"new-access","refresh_token":"new-refresh","expires_in":7200}}`), nil
			})}, time.Second)
			old := LoginBufferCredentials{OpenID: "account", AccessToken: "old-access", RefreshToken: "old-refresh", RefreshBusinessID: source, RefreshTokenObservedAt: 123}
			fresh, err := client.RefreshCredentials(context.Background(), old)
			if err != nil || fresh.AccessToken != "new-access" || fresh.RefreshToken != "new-refresh" || fresh.RefreshBusinessID != business || fresh.RefreshTokenObservedAt != 123 {
				t.Fatalf("credential rotation failed: %v", err)
			}
			if got := CredentialsFromMap(fresh.ToMap()); got != fresh {
				t.Fatal("credential source/lifecycle metadata did not survive persistence")
			}
		})
	}
}

func TestAmbiguousRefreshUsesOfficialCheckLogin(t *testing.T) {
	for _, recoverable := range []bool{false, true} {
		t.Run(map[bool]string{false: "explicit rejection", true: "rotated credentials"}[recoverable], func(t *testing.T) {
			calls := 0
			client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(req *http.Request) (*http.Response, error) {
				calls++
				if calls == 1 && req.URL.String() == mainRefreshURL {
					return credentialTestResponse(`{"code":-109,"msg":"RC_PARAMS_INVALID"}`), nil
				}
				if calls != 2 || req.URL.String() != checkLoginURL {
					t.Fatalf("unexpected request: %s", req.URL)
				}
				if recoverable {
					return credentialTestResponse(`{"code":0,"user_info":{"access_token":"checked-access","refresh_token":"checked-refresh","expires_in":7200}}`), nil
				}
				return credentialTestResponse(`{"code":-109,"msg":"40030 invalid refresh_token"}`), nil
			})}, time.Second)
			fresh, err := client.RefreshCredentials(context.Background(), LoginBufferCredentials{OpenID: "account", RefreshToken: "refresh"})
			if calls != 2 {
				t.Fatalf("requests = %d", calls)
			}
			if recoverable {
				if err != nil || fresh.AccessToken != "checked-access" || fresh.RefreshToken != "checked-refresh" {
					t.Fatalf("check-login rotation lost: %v", err)
				}
			} else {
				var rejected *RefreshRejectedError
				if !errors.As(err, &rejected) || !strings.Contains(rejected.Message, "40030") || !strings.Contains(err.Error(), "RC_PARAMS_INVALID") {
					t.Fatalf("underlying rejection lost: %v", err)
				}
			}
		})
	}
}

func TestRefreshDoesNotProbeAfterTransportFailure(t *testing.T) {
	calls := 0
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		return nil, context.DeadlineExceeded
	})}, time.Second)
	_, err := client.RefreshCredentials(context.Background(), LoginBufferCredentials{RefreshToken: "refresh"})
	if !errors.Is(err, context.DeadlineExceeded) || calls != 1 {
		t.Fatalf("transport failure retried: calls=%d err=%v", calls, err)
	}
}

func TestRefreshRejectsDifferentAccount(t *testing.T) {
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(*http.Request) (*http.Response, error) {
		return credentialTestResponse(`{"code":0,"user_info":{"open_id":"other-account","access_token":"other-token"}}`), nil
	})}, time.Second)
	if _, err := client.RefreshCredentials(context.Background(), LoginBufferCredentials{OpenID: "account", RefreshToken: "refresh"}); err == nil {
		t.Fatal("accepted credentials belonging to another account")
	}
}

func TestRefreshBufferFailureReturnsRotatedCredentials(t *testing.T) {
	client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		if req.URL.String() == mainRefreshURL {
			return credentialTestResponse(`{"code":0,"user_info":{"access_token":"rotated-access","refresh_token":"rotated-refresh","expires_in":7200}}`), nil
		}
		return nil, context.DeadlineExceeded
	})}, time.Second)
	result, err := client.RefreshLoginBuffer(context.Background(), LoginBufferCredentials{OpenID: "account", RefreshToken: "refresh"})
	if err == nil || !result.Refreshed || result.Credentials.RefreshToken != "rotated-refresh" {
		t.Fatal("buffer failure discarded a successful token rotation")
	}
}
