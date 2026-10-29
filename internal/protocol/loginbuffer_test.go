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

type loginBufferRoundTripFunc func(*http.Request) (*http.Response, error)

func (f loginBufferRoundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func TestLoginBufferRejectionPreservesBusinessErrorType(t *testing.T) {
	for _, status := range []int{http.StatusOK, http.StatusBadGateway} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(req *http.Request) (*http.Response, error) {
				if req.URL.String() != loginBufferURL {
					t.Fatalf("unexpected endpoint: %v", req.URL)
				}
				return &http.Response{
					StatusCode: status,
					Body:       io.NopCloser(strings.NewReader(`{"code":-101,"msg":"GetLoginBuffer error [40188] [invalid scope]"}`)),
					Header:     make(http.Header),
				}, nil
			})}, time.Second)
			_, err := client.FetchLoginBuffer(context.Background(), LoginBufferCredentials{})
			if err == nil {
				t.Fatal("expected rejection")
			}
			var rejected *LoginBufferRejectedError
			if status == http.StatusOK {
				if !errors.As(err, &rejected) || rejected.Code != -101 || rejected.Message != "GetLoginBuffer error [40188] [invalid scope]" {
					t.Fatalf("business rejection = %v", err)
				}
			} else if errors.As(err, &rejected) {
				t.Fatalf("HTTP error must not be a business rejection: %v", err)
			}
		})
	}
}

func TestCredentialResponsesRequireExplicitIntegerCode(t *testing.T) {
	for _, tc := range []struct {
		name, code string
		wantCode   int
		wantOK     bool
	}{
		{name: "numeric success", code: `"code":0,`, wantOK: true},
		{name: "string success", code: `"code":"0",`, wantOK: true},
		{name: "numeric rejection", code: `"code":42007,`, wantCode: 42007},
		{name: "string rejection", code: `"code":"42007",`, wantCode: 42007},
		{name: "negative string rejection", code: `"code":"-101",`, wantCode: -101},
		{name: "missing"},
		{name: "null", code: `"code":null,`},
		{name: "boolean", code: `"code":false,`},
		{name: "object", code: `"code":{},`},
		{name: "fraction", code: `"code":0.5,`},
		{name: "decimal zero", code: `"code":0.0,`},
		{name: "exponent", code: `"code":0e0,`},
		{name: "invalid string", code: `"code":"invalid-secret",`},
		{name: "overflow", code: `"code":2147483648,`},
	} {
		for _, operation := range []string{"login_buffer", "refresh_token", "check_login"} {
			t.Run(tc.name+"/"+operation, func(t *testing.T) {
				calls := 0
				client := NewLoginBufferClientWithHTTPClient(&http.Client{Transport: loginBufferRoundTripFunc(func(*http.Request) (*http.Response, error) {
					calls++
					// Even a rejected or malformed response may include plausible data.
					return credentialTestResponse(`{` + tc.code + `"msg":"access-secret refresh-secret account-secret","user_info":{"access_token":"untrusted-token"},"ext_info":{"list_s":{"login_buffer":{"value":["untrusted-buffer"]}}}}`), nil
				})}, time.Second)
				credentials := LoginBufferCredentials{OpenID: "account-secret", AccessToken: "access-secret", RefreshToken: "refresh-secret"}
				var err error
				var value string
				if operation == "login_buffer" {
					value, err = client.FetchLoginBuffer(context.Background(), credentials)
				} else {
					var refreshed LoginBufferCredentials
					if operation == "check_login" {
						refreshed, err = client.CheckLogin(context.Background(), credentials)
					} else {
						refreshed, err = client.RefreshCredentials(context.Background(), credentials)
					}
					value = refreshed.AccessToken
				}
				if calls != 1 {
					t.Fatalf("unexpected retry: %d requests", calls)
				}
				if tc.wantOK {
					if err != nil || value == "" {
						t.Fatalf("valid success rejected: %v", err)
					}
					return
				}
				if err == nil || value != "" {
					t.Fatal("accepted rejected or malformed response data")
				}
				var bufferRejected *LoginBufferRejectedError
				var refreshRejected *RefreshRejectedError
				isBusinessError := errors.As(err, &bufferRejected) || errors.As(err, &refreshRejected)
				if tc.wantCode == 0 {
					if isBusinessError {
						t.Fatal("malformed response must not establish credential rejection")
					}
				} else if operation == "login_buffer" {
					if bufferRejected == nil || bufferRejected.Code != tc.wantCode {
						t.Fatalf("business rejection lost: %v", err)
					}
				} else if refreshRejected == nil || refreshRejected.Code != tc.wantCode || refreshRejected.Operation != operation {
					t.Fatalf("business rejection lost: %v", err)
				}
				for _, secret := range []string{"access-secret", "refresh-secret", "account-secret", "invalid-secret", "untrusted-token", "untrusted-buffer"} {
					if strings.Contains(err.Error(), secret) {
						t.Fatal("response error exposes credentials or payload")
					}
				}
			})
		}
	}
}

func TestCredentialsMapPreservesRefreshTokenObservation(t *testing.T) {
	observedAt := time.Now().Add(-24 * time.Hour).Unix()
	credentials := LoginBufferCredentials{
		OpenID:                 "openid",
		AccessToken:            "access",
		RefreshToken:           "refresh",
		RefreshTokenObservedAt: observedAt,
		ExpiresAt:              time.Now().Add(time.Hour).Unix(),
	}

	got := CredentialsFromMap(credentials.ToMap())
	if got.RefreshTokenObservedAt != observedAt {
		t.Fatalf("RefreshTokenObservedAt = %d, want %d", got.RefreshTokenObservedAt, observedAt)
	}
}

func TestCredentialsMapForScanStartsRefreshTokenObservation(t *testing.T) {
	before := time.Now().Unix()
	values := LoginBufferCredentials{RefreshToken: "refresh"}.ToMapForScan()
	after := time.Now().Unix()

	observedAt := int64FromMap(values, "refresh_token_observed_at")
	if observedAt < before || observedAt > after {
		t.Fatalf("refresh_token_observed_at = %d, want between %d and %d", observedAt, before, after)
	}
}

func TestCredentialsMapRefreshPreservesMissingObservation(t *testing.T) {
	values := LoginBufferCredentials{RefreshToken: "refresh"}.ToMap()
	if observed := int64FromMap(values, "refresh_token_observed_at"); observed != 0 {
		t.Fatalf("refresh_token_observed_at = %d, want 0 for non-scan serialization", observed)
	}
}

func TestCredentialsMapDerivesMissingExpiryFromRefreshTimestamp(t *testing.T) {
	refreshedAt := time.Now().Add(-30 * time.Minute).Unix()
	got := CredentialsFromMap(map[string]any{
		"expires_in":           int64(7200),
		"refresh_refreshed_at": refreshedAt,
	})
	want := refreshedAt + 7200
	if got.ExpiresAt != want {
		t.Fatalf("ExpiresAt = %d, want %d", got.ExpiresAt, want)
	}
}
