package protocol

import (
	"testing"

	"yyb_go/internal/store"
)

func TestProviderOfHistoricalWipedIdentity(t *testing.T) {
	for _, provider := range []string{"pcmgr", "qbbrowser", "ldplayer"} {
		if got := ProviderOf(&store.WechatAccount{OpenID: provider + ":synthetic"}); got != provider {
			t.Errorf("%s routed as %q after credential wipe", provider, got)
		}
	}
	if ProviderOf(nil) != "" || ProviderOf(&store.WechatAccount{OpenID: "synthetic"}) != "" {
		t.Fatal("default YYB provider changed")
	}
}
