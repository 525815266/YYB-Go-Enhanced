package httpapi

import (
	"context"
	"errors"
	"testing"
	"time"

	"yyb_go/internal/protocol"
)

func TestRefreshSavesRotationBeforeBufferFailureOrCancellation(t *testing.T) {
	for _, cancelRequest := range []bool{false, true} {
		t.Run(map[bool]string{false: "buffer failure", true: "cancelled request"}[cancelRequest], func(t *testing.T) {
			app := newKeepAliveTestApp(t)
			defer app.Close()
			acc := insertKeepAliveTestAccount(t, app, "rotation", time.Now().Add(time.Minute))
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
				creds.AccessToken, creds.RefreshToken = "new-access", "new-refresh"
				creds.ExpiresAt = time.Now().Add(2 * time.Hour).Unix()
				if cancelRequest {
					cancel()
				}
				return creds, nil
			}
			app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
				stored, err := app.db.GetAccount(context.Background(), acc.ID)
				if err != nil || stored.Credentials["refreshtoken"] != "new-refresh" {
					t.Fatal("buffer request started before token was saved")
				}
				return "", context.DeadlineExceeded
			}
			status, refreshed, err := app.refreshAccount(ctx, acc, true)
			if status != "alive" || !refreshed || !errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("refresh = %q, %v, %v", status, refreshed, err)
			}
			stored, err := app.db.GetAccount(context.Background(), acc.ID)
			if err != nil || stored.Credentials["refreshtoken"] != "new-refresh" || stored.LoginBuffer != acc.LoginBuffer {
				t.Fatal("successful rotation was lost on subsequent failure")
			}
		})
	}
}

func TestBackgroundRefreshDoesNotFetchLoginBuffer(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "token-only", time.Now().Add(time.Minute))
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		creds.AccessToken = "new-access"
		creds.ExpiresAt = time.Now().Add(2 * time.Hour).Unix()
		return creds, nil
	}
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
		t.Fatal("background token renewal must not fetch a login buffer")
		return "", nil
	}
	if status, renewed, err := app.refreshAccount(context.Background(), acc, false); status != "alive" || !renewed || err != nil {
		t.Fatalf("background refresh = %q, %v, %v", status, renewed, err)
	}
}

func TestRefreshRetryFitsInsideExpiryWindow(t *testing.T) {
	now := time.Now()
	for _, remaining := range []time.Duration{time.Hour, 4 * time.Minute, time.Minute, -time.Minute} {
		creds := protocol.LoginBufferCredentials{ExpiresAt: now.Add(remaining).Unix()}
		delay := keepAliveFailureBackoff(creds, now)
		if delay < 30*time.Second || delay > keepAliveRetryBackoff || (remaining > time.Minute && delay >= remaining) {
			t.Fatalf("retry delay %s does not fit expiry window %s", delay, remaining)
		}
	}
}

func TestInFlightRefreshCannotOverwriteRescanOrManualExpiration(t *testing.T) {
	for _, transition := range []string{"rescan-success", "rescan-rejection", "manual-expiration"} {
		t.Run(transition, func(t *testing.T) {
			app := newKeepAliveTestApp(t)
			defer app.Close()
			acc := insertKeepAliveTestAccount(t, app, "concurrent-account", time.Now().Add(time.Minute))
			ctx := context.Background()
			app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
				if transition == "manual-expiration" {
					if err := app.setAccountStatus(ctx, acc.ID, "expired"); err != nil {
						t.Fatal(err)
					}
				} else {
					rescanned := creds
					rescanned.AccessToken, rescanned.RefreshToken = "scanned-access", "scanned-refresh"
					status := "alive"
					if _, err := app.db.UpsertAccount(ctx, acc.OpenID, "scanned-buffer", nil, nil, nil, nil, rescanned.ToMapForScan(), &status); err != nil {
						t.Fatal(err)
					}
				}
				if transition == "rescan-rejection" {
					return protocol.LoginBufferCredentials{}, &protocol.RefreshRejectedError{Code: 40030, Message: "invalid refresh_token"}
				}
				creds.AccessToken = "old-request-access"
				return creds, nil
			}
			if _, _, err := app.refreshAccount(ctx, acc, false); err == nil {
				t.Fatal("stale request should report a conflict")
			}
			stored, err := app.db.GetAccount(ctx, acc.ID)
			if err != nil {
				t.Fatal(err)
			}
			if transition == "manual-expiration" {
				if accountStatus(stored) != "expired" || stored.Credentials != nil {
					t.Fatal("old request revived a manually expired account")
				}
			} else if accountStatus(stored) != "alive" || stored.Credentials["accesstoken"] != "scanned-access" || stored.LoginBuffer != "scanned-buffer" {
				t.Fatal("old refresh overwrote or expired a rescanned account")
			}
		})
	}
}
