package eventlog

import (
	"context"
	"path/filepath"
	"testing"

	"yyb_go/internal/store"
)

func TestChannelForProviders(t *testing.T) {
	cases := map[string]string{
		"":           "yyb",
		"appstore":   "yyb",
		"pcmgr":      "pcmgr",
		"gameassist": "gameassist",
		"qbbrowser":  "qbbrowser",
		"ldplayer":   "ldplayer",
		"bogus":      "yyb",
	}
	for provider, want := range cases {
		if got := ChannelFor(provider); got != want {
			t.Fatalf("ChannelFor(%q) = %q, want %q", provider, got, want)
		}
	}
}

func TestWipedChannelIdentityPersistsInEvents(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "events.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	logger, err := New(db)
	if err != nil {
		t.Fatal(err)
	}
	for _, provider := range []string{"qbbrowser", "ldplayer"} {
		logger.Emit("account.scanned", ForAccount(&store.WechatAccount{ID: 1, OpenID: provider + ":synthetic"}))
	}
	logger.Close()
	for _, provider := range []string{"qbbrowser", "ldplayer"} {
		entries, err := db.ListSystemLogs(context.Background(), store.SystemLogFilter{Channel: provider, Limit: 10})
		if err != nil || len(entries) != 1 {
			t.Errorf("%s classification missing: entries=%d err=%v", provider, len(entries), err)
		}
	}
}
