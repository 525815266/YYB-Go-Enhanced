package store

import (
	"context"
	"path/filepath"
	"testing"
)

func TestCircuitSettingsSurviveRestartCompactionAndDeletion(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "yyb.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	first, err := db.UpsertAccount(ctx, "first", "buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	second, err := db.UpsertAccount(ctx, "second", "buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = db.SetAccountCircuitEnabled(ctx, second.ID, false); err != nil {
		t.Fatal(err)
	}
	if err = db.SetSetting(ctx, CircuitEnabledKey, "true"); err != nil {
		t.Fatal(err)
	}
	if err = db.DeleteAccount(ctx, first.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = db.CompactAccountIDs(ctx); err != nil {
		t.Fatal(err)
	}
	if err = db.Close(); err != nil {
		t.Fatal(err)
	}
	db, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	s, err := db.CircuitSettings(ctx, 1, false)
	if err != nil || !s.GlobalEnabled || s.AccountEnabled || s.Effective {
		t.Fatalf("policy lost after restart/remap: %+v %v", s, err)
	}
	if err = db.DeleteAccount(ctx, 1); err != nil {
		t.Fatal(err)
	}
	fresh, err := db.UpsertAccount(ctx, "fresh", "buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	s, err = db.CircuitSettings(ctx, fresh.ID, false)
	if err != nil || !s.AccountEnabled || !s.Effective {
		t.Fatalf("deleted account policy leaked: %+v %v", s, err)
	}
}

func TestCircuitSettingsMergeKeepsTargetPolicy(t *testing.T) {
	ctx := context.Background()
	db, err := Open(filepath.Join(t.TempDir(), "merge.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	status := "alive"
	source, err := db.UpsertAccount(ctx, "source", "source-buffer", nil, nil, nil, nil, map[string]any{"access_token": "synthetic"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	target, err := db.UpsertAccount(ctx, "target", "target-buffer", nil, nil, nil, nil, map[string]any{"access_token": "synthetic-target"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	if err = db.SetAccountCircuitEnabled(ctx, source.ID, true); err != nil {
		t.Fatal(err)
	}
	if err = db.SetAccountCircuitEnabled(ctx, target.ID, false); err != nil {
		t.Fatal(err)
	}
	if _, err = db.MergeAccountAuthorization(ctx, source, target); err != nil {
		t.Fatal(err)
	}
	s, err := db.CircuitSettings(ctx, target.ID, true)
	if err != nil || s.AccountEnabled || s.Effective {
		t.Fatal("merge replaced target policy", s, err)
	}
}
