package eventlog

import "testing"

func TestChannelForProviders(t *testing.T) {
	cases := map[string]string{
		"":           "yyb",
		"appstore":   "yyb",
		"pcmgr":      "pcmgr",
		"gameassist": "gameassist",
		"bogus":      "yyb",
	}
	for provider, want := range cases {
		if got := ChannelFor(provider); got != want {
			t.Fatalf("ChannelFor(%q) = %q, want %q", provider, got, want)
		}
	}
}
