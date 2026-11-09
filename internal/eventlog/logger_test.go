package eventlog

import (
	"context"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"yyb_go/internal/store"
)

func TestSafeFieldsPersistAcrossRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "logs.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	l, err := New(db)
	if err != nil {
		t.Fatal(err)
	}
	l.Emit("protocol.failed", Fields{AccountID: 1, Channel: "pcmgr", ResponseStage: "private-token", Route: "/account-link/private-token?secret=value", Method: "private-token", Rejections: []Rejection{{Stage: "native_autoauth_business", Code: -13}, {Stage: "private-token", Code: 42}}})
	l.Emit("private-token", Fields{})
	l.Close()
	db.Close()
	db, err = store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	entries, err := db.ListSystemLogs(context.Background(), store.SystemLogFilter{Limit: 100})
	if err != nil || len(entries) != 1 {
		t.Fatalf("persisted entries=%d error=%v", len(entries), err)
	}
	got := entries[0]
	if got.AccountID != 1 || got.Channel != "pcmgr" || got.Level != "error" || got.Category != "protocol" {
		t.Fatal("lost event classification")
	}
	if strings.Contains(string(got.Details), "private-token") || !strings.Contains(string(got.Details), "-13") {
		t.Fatalf("unsafe or incomplete details: %s", got.Details)
	}
}

func TestConcurrentEmitAndClose(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "logs.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	l, err := New(db)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 40; j++ {
				l.Emit("system.started", Fields{})
			}
		}()
	}
	l.Close()
	wg.Wait()
	l.Close()
}

func TestTargetFieldsKeepOnlyPlainIdentifiers(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "logs.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	l, err := New(db)
	if err != nil {
		t.Fatal(err)
	}
	l.Emit("protocol.failed", Fields{AccountID: 1, Channel: "yyb", ResponseStage: "session_payload"}.WithTarget("wx91d27dbf599dff74", OpOperateWXData, "getUserInfo"))
	l.Emit("protocol.failed", Fields{AccountID: 2, Channel: "yyb"}.WithTarget("wx?token=secret value", "private-token", "bad name!"))
	l.Close()
	entries, err := db.ListSystemLogs(context.Background(), store.SystemLogFilter{Limit: 10})
	if err != nil || len(entries) != 2 {
		t.Fatalf("entries=%d error=%v", len(entries), err)
	}
	for _, entry := range entries {
		details := string(entry.Details)
		switch entry.AccountID {
		case 1:
			for _, want := range []string{`"app_id":"wx91d27dbf599dff74"`, `"operation":"operate_wx_data"`, `"api":"getUserInfo"`, `"response_stage":"session_payload"`} {
				if !strings.Contains(details, want) {
					t.Fatalf("valid target dropped %s: %s", want, details)
				}
			}
		case 2:
			for _, bad := range []string{"app_id", "operation", "api", "secret", "private-token"} {
				if strings.Contains(details, bad) {
					t.Fatalf("unsafe target text %q kept: %s", bad, details)
				}
			}
		}
	}
}
