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
