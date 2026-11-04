package store

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestCredentialRefreshMatchesOriginalJSONAndAccountIdentity(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "accounts.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	status := "alive"
	acc, err := db.UpsertAccount(ctx, "original-account", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}
	// Imported databases need not use json.Marshal's key order or whitespace.
	if _, err := db.sql.ExecContext(ctx, `UPDATE wechat_accounts SET credentials=? WHERE id=?`, `{ "refreshtoken": "old", "accesstoken": "old" }`, acc.ID); err != nil {
		t.Fatal(err)
	}
	acc, _ = db.GetAccount(ctx, acc.ID)
	updated := map[string]any{"refreshtoken": "new", "accesstoken": "new"}
	if applied, err := db.ApplyCredentialRefresh(ctx, acc, "new-buffer", updated, "alive"); err != nil || !applied {
		t.Fatalf("imported JSON refresh = %v, %v", applied, err)
	}
	if applied, err := db.ApplyCredentialRefresh(ctx, acc, "stale-buffer", updated, "expired"); err != nil || applied {
		t.Fatalf("stale refresh accepted = %v, %v", applied, err)
	}
	current, _ := db.GetAccount(ctx, acc.ID)
	if err := db.PutSession(ctx, acc.ID, nil, map[string]any{"marker": "active"}, time.Now().Add(time.Hour).Unix(), ""); err != nil {
		t.Fatal(err)
	}
	if applied, err := db.ApplyCredentialRefresh(ctx, current, "", nil, "expired"); err != nil || !applied {
		t.Fatalf("expiration = %v, %v", applied, err)
	}
	if _, err := db.GetSession(ctx, acc.ID, ""); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("expiration did not clear the protocol session")
	}
	if err := db.DeleteAccount(ctx, acc.ID); err != nil {
		t.Fatal(err)
	}
	replacement, err := db.UpsertAccount(ctx, "replacement-account", "replacement-buffer", nil, nil, nil, nil, updated, &status)
	if err != nil || replacement.ID != acc.ID {
		t.Fatal("failed to reproduce ID reuse")
	}
	if applied, err := db.ApplyCredentialRefresh(ctx, current, "stale-buffer", updated, "alive"); err != nil || applied {
		t.Fatalf("old account refresh overwrote reused ID: %v, %v", applied, err)
	}
}

// Limiting or expiring an account wipes its tokens but must keep the one thing
// that says which channel a GameAssist account belongs to: it has a real WeChat
// openId, so nothing else tells it apart from an App Store account.
func TestCredentialWipeKeepsOnlyTheGameAssistMarker(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "accounts.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	status := "alive"
	secret := map[string]any{"provider": "gameassist", "openid": "o-real", "accesstoken": "1_secret", "refreshtoken": "1_secretr", "refresh_token_observed_at": int64(1700000000)}
	for _, wipe := range []string{StatusSessionOnly, "expired"} {
		acc, err := db.UpsertAccount(ctx, "o-real-"+wipe, "buffer", nil, nil, nil, nil, secret, &status)
		if err != nil {
			t.Fatal(err)
		}
		acc, _ = db.GetAccount(ctx, acc.ID)
		if applied, err := db.ApplyCredentialRefresh(ctx, acc, "buffer", nil, wipe); err != nil || !applied {
			t.Fatalf("%s: applied=%v err=%v", wipe, applied, err)
		}
		wiped, _ := db.GetAccount(ctx, acc.ID)
		if wiped.Credentials["provider"] != "gameassist" || wiped.Credentials["accesstoken"] != nil || wiped.Credentials["refreshtoken"] != nil || wiped.Credentials["openid"] != nil {
			t.Fatalf("%s: credentials after the wipe = %v", wipe, wiped.Credentials)
		}
		wantKeys := 1
		if wipe == StatusSessionOnly {
			wantKeys = 2 // the marker and the scan time
		}
		if len(wiped.Credentials) != wantKeys || wiped.Public().ScanProduct != "gameassist" || wiped.LoginBuffer != "" {
			t.Fatalf("%s: credentials=%v product=%s buffer=%q", wipe, wiped.Credentials, wiped.Public().ScanProduct, wiped.LoginBuffer)
		}
	}
	// App Store accounts carry no marker and keep ending up with nothing.
	plain, err := db.UpsertAccount(ctx, "o-plain", "buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "1_a", "refreshtoken": "1_r"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	plain, _ = db.GetAccount(ctx, plain.ID)
	if applied, err := db.ApplyCredentialRefresh(ctx, plain, "", nil, "expired"); err != nil || !applied {
		t.Fatalf("appstore expiry: %v, %v", applied, err)
	}
	if after, _ := db.GetAccount(ctx, plain.ID); after.Credentials != nil {
		t.Fatalf("appstore credentials survived expiry: %v", after.Credentials)
	}
}
