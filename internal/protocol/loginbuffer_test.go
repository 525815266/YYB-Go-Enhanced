package protocol

import (
	"testing"
	"time"
)

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
