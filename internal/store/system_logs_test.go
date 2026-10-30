package store

import (
	"context"
	"encoding/json"
	"path/filepath"
	"testing"
	"time"
)

func TestSystemLogRetentionFiltersAndStablePages(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "logs.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	if err := db.InitSystemLogs(ctx); err != nil {
		t.Fatal(err)
	}
	entries := make([]SystemLog, 10004)
	for i := range entries {
		entries[i] = SystemLog{CreatedAt: time.Now().UnixMilli(), Level: "info", Category: "protocol", Event: "test", Details: json.RawMessage(`{}`), AccountID: 1, Channel: "pcmgr"}
	}
	entries[len(entries)-1].CreatedAt = time.Now().Add(-8 * 24 * time.Hour).UnixMilli()
	if err := db.AppendSystemLogs(ctx, entries); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := db.sql.QueryRow(`SELECT COUNT(*) FROM system_logs`).Scan(&count); err != nil || count != 9999 {
		t.Fatalf("retention count=%d err=%v", count, err)
	}
	f := SystemLogFilter{Limit: 2, Category: "protocol", Level: "info", AccountID: 1, Channel: "pcmgr"}
	page, err := db.ListSystemLogs(ctx, f)
	if err != nil || len(page) != 2 {
		t.Fatal("filter failed", err)
	}
	f.Before = page[1].ID
	newer := entries[0]
	if err := db.AppendSystemLogs(ctx, []SystemLog{newer}); err != nil {
		t.Fatal(err)
	}
	next, err := db.ListSystemLogs(ctx, f)
	if err != nil || len(next) != 2 || next[0].ID >= page[1].ID {
		t.Fatal("cursor overlapped after insert", err)
	}
	f.Level = "error"
	next, err = db.ListSystemLogs(ctx, f)
	if err != nil || len(next) != 0 {
		t.Fatal("level filter ignored", err)
	}
}
