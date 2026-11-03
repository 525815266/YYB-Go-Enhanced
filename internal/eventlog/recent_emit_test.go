package eventlog

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/store"
)

// Events that report a death carry the list; the logger keeps only safe
// identifiers in it and never more than the cap.
func TestEmitCleansTheRecentListOfADeathEvent(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "logs.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	l, err := New(db)
	if err != nil {
		t.Fatal(err)
	}
	l.Emit("protocol.failed", Fields{AccountID: 5, AppID: "wxaaaaaaaaaaaaaaaa", Operation: OpGetCode, Rejections: []Rejection{{Stage: "shortlink_application", Code: -14}}})
	recent := l.Recent(5, time.Minute, recentMaxInEvent)
	recent = append(recent,
		RecentRequest{AppID: "has space/and?query", Event: "failed"},
		RecentRequest{AppID: "wxbbbbbbbbbbbbbbbb", Event: "private-token"},
		RecentRequest{AppID: "wxcccccccccccccccc", Event: "failed", AgeS: -3},
		RecentRequest{AppID: "wxdddddddddddddddd", Op: "secret operation", Event: "failed"},
	)
	l.Emit("account.expired", Fields{AccountID: 5, Recent: recent})
	l.Close()
	entries, err := db.ListSystemLogs(context.Background(), store.SystemLogFilter{Limit: 20})
	if err != nil {
		t.Fatal(err)
	}
	var details string
	for _, e := range entries {
		if e.Event == "account.expired" {
			details = string(e.Details)
		}
	}
	if !strings.Contains(details, `"app_id":"wxaaaaaaaaaaaaaaaa"`) || !strings.Contains(details, `"code":-14`) {
		t.Fatalf("the genuine entry is missing: %s", details)
	}
	for _, bad := range []string{"has space/and?query", "private-token", "wxbbbbbbbbbbbbbbbb", "wxcccccccccccccccc", "secret operation", "wxdddddddddddddddd"} {
		if strings.Contains(details, bad) {
			t.Fatalf("unsafe entry %q survived: %s", bad, details)
		}
	}
}
