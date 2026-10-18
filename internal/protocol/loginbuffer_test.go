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
