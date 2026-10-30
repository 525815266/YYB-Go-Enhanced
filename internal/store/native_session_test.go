package store

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestNativeSessionCASAllowsLimitedModeButRejectsStaleState(t *testing.T) {
	ctx := context.Background()
	for _, outcome := range []string{"limited", "rescan", "credential_rotation", "expired", "deleted", "other_route", "same_route_race"} {
		t.Run(outcome, func(t *testing.T) {
			db, err := Open(filepath.Join(t.TempDir(), "test.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			status := "alive"
			acc, err := db.UpsertAccount(ctx, "synthetic-cas", "buffer", nil, nil, nil, nil, map[string]any{"last_scan_at": 123}, &status)
			if err != nil {
				t.Fatal(err)
			}
			expiry := time.Now().Add(time.Hour).Unix()
			if err := db.PutSessionForAccount(ctx, acc, 123, map[string]any{"native_key": "synthetic"}, expiry, ""); err != nil {
				t.Fatal(err)
			}
			if _, err := db.ApplyCredentialRefresh(ctx, acc, "", nil, StatusSessionOnly); err != nil {
				t.Fatal(err)
			}
			acc, _ = db.GetAccount(ctx, acc.ID)
			row, err := db.GetSessionForRecovery(ctx, acc.ID, "")
			if err != nil {
				t.Fatal(err)
			}
			switch outcome {
			case "rescan":
				_ = db.SetAccountCredential(ctx, acc.ID, "new-buffer", map[string]any{"last_scan_at": 456})
			case "credential_rotation":
				_, _ = db.ApplyCredentialRefresh(ctx, acc, "", map[string]any{"new": "key"}, "alive")
			case "expired":
				_ = db.SetAccountStatus(ctx, acc.ID, "expired")
			case "deleted":
				_ = db.DeleteAccount(ctx, acc.ID)
			case "other_route":
				row.TCPProxy = "socks5://example.invalid:1080"
			case "same_route_race":
				if _, err := db.ReplaceNativeSessionForAccount(ctx, acc, row, 123, map[string]any{"attempted_at": 1}, expiry); err != nil {
					t.Fatal(err)
				}
			}
			next, err := db.ReplaceNativeSessionForAccount(ctx, acc, row, 123, map[string]any{"native_key": "rotated"}, expiry)
			if outcome != "limited" {
				if !errors.Is(err, ErrSessionChanged) {
					t.Fatal("stale native write not rejected", err)
				}
				return
			}
			if err != nil || next == nil {
				t.Fatal("limited-mode native rotation blocked", err)
			}
			current, _ := db.GetAccount(ctx, acc.ID)
			if !current.SessionOnly() || !current.SameAuthorization(acc) {
				t.Fatal("native rotation changed account authorization")
			}
			if err := db.PutSessionForAccount(ctx, acc, 123, map[string]any{"manual": "forbidden"}, expiry, ""); !errors.Is(err, ErrAccountAuthorizationChanged) {
				t.Fatal("ManualAuth guard was relaxed")
			}
		})
	}
}

func TestNativeKeyCanOnlyBeClaimedOnceAcrossWorkers(t *testing.T) {
	ctx := context.Background()
	db, err := Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	acc, _ := db.UpsertAccount(ctx, "synthetic-workers", "buffer", nil, nil, nil, nil, nil, nil)
	expiry := time.Now().Add(time.Hour).Unix()
	if err := db.PutSessionForAccount(ctx, acc, 123, map[string]any{"key": "old"}, expiry, ""); err != nil {
		t.Fatal(err)
	}
	row, _ := db.GetSessionForRecovery(ctx, acc.ID, "")
	results := make(chan error, 2)
	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := db.ReplaceNativeSessionForAccount(ctx, acc, row, 123, map[string]any{"attempted_at": 1}, expiry)
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	winners, stale := 0, 0
	for err := range results {
		if err == nil {
			winners++
		} else if errors.Is(err, ErrSessionChanged) {
			stale++
		} else {
			t.Fatal(err)
		}
	}
	if winners != 1 || stale != 1 {
		t.Fatal("rotating key sent more than once")
	}
}
