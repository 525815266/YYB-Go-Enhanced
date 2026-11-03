package eventlog

import (
	"strings"
	"testing"
	"time"
)

func testBook() (*recentBook, *time.Time) {
	now := time.Unix(1_700_000_000, 0)
	b := newRecentBook()
	b.now = func() time.Time { return now }
	return b, &now
}

func TestRecentBookKeepsOnlyProtocolEventsOfAnAccountThatNameAnAppID(t *testing.T) {
	b, _ := testBook()
	f := Fields{AccountID: 7, AppID: "wxaaaaaaaaaaaaaaaa", Operation: OpGetCode}
	b.observe("protocol.succeeded", f)
	b.observe("keepalive.renewed", f)
	b.observe("protocol.failed", Fields{AccountID: 7})
	b.observe("protocol.failed", Fields{AppID: "wxbbbbbbbbbbbbbbbb"})
	b.observe("protocol.failed", Fields{AccountID: 8, AppID: "wxcccccccccccccccc"})
	b.observe("protocol.failed", Fields{AccountID: 7, AppID: "wxdddddddddddddddd", Operation: OpGetCode, Rejections: []Rejection{{Stage: "shortlink_application", Code: -14}}})
	got := b.snapshot(7, time.Minute, 12)
	if len(got) != 2 || got[0].AppID != "wxaaaaaaaaaaaaaaaa" || got[0].Event != "succeeded" || got[1].AppID != "wxdddddddddddddddd" || got[1].Event != "failed" || got[1].Code != -14 {
		t.Fatalf("recent requests = %+v", got)
	}
	if other := b.snapshot(8, time.Minute, 12); len(other) != 1 || other[0].AppID != "wxcccccccccccccccc" {
		t.Fatalf("another account must keep its own list, got %+v", other)
	}
}

func TestRecentBookReportsAgesAndForgetsOldEntries(t *testing.T) {
	b, now := testBook()
	b.observe("protocol.failed", Fields{AccountID: 1, AppID: "wxaaaaaaaaaaaaaaaa"})
	*now = now.Add(90 * time.Second)
	b.observe("protocol.failed", Fields{AccountID: 1, AppID: "wxbbbbbbbbbbbbbbbb"})
	*now = now.Add(30 * time.Second)
	got := b.snapshot(1, 2*time.Minute, 12)
	if len(got) != 2 || got[0].AgeS != 120 || got[1].AgeS != 30 {
		t.Fatalf("ages = %+v", got)
	}
	if only := b.snapshot(1, time.Minute, 12); len(only) != 1 || only[0].AppID != "wxbbbbbbbbbbbbbbbb" {
		t.Fatalf("the window must drop the older request, got %+v", only)
	}
	*now = now.Add(recentKeep + time.Second)
	b.observe("protocol.failed", Fields{AccountID: 1, AppID: "wxcccccccccccccccc"})
	if got := b.snapshot(1, time.Hour, 12); len(got) != 1 || got[0].AppID != "wxcccccccccccccccc" {
		t.Fatalf("entries older than the keep time must be forgotten, got %+v", got)
	}
}

func TestRecentBookIsBoundedAndReturnsTheNewest(t *testing.T) {
	b, now := testBook()
	for i := 0; i < recentPerAccount+25; i++ {
		*now = now.Add(time.Second)
		b.observe("protocol.failed", Fields{AccountID: 3, AppID: "wx" + strings.Repeat("a", 15) + string(rune('a'+i%20))})
	}
	b.mu.Lock()
	kept := len(b.byAccount[3])
	b.mu.Unlock()
	if kept != recentPerAccount {
		t.Fatalf("kept %d entries, want %d", kept, recentPerAccount)
	}
	got := b.snapshot(3, time.Hour, 5)
	if len(got) != 5 || got[4].AgeS != 0 || got[0].AgeS != 4 {
		t.Fatalf("the newest five, oldest first, expected; got %+v", got)
	}
	var nilBook *recentBook
	nilBook.observe("protocol.failed", Fields{AccountID: 1, AppID: "wxaaaaaaaaaaaaaaaa"})
	if nilBook.snapshot(1, time.Hour, 5) != nil {
		t.Fatal("a nil book must stay silent")
	}
	var nilLogger *Logger
	if nilLogger.Recent(1, time.Hour, 5) != nil {
		t.Fatal("a nil logger must stay silent")
	}
}
