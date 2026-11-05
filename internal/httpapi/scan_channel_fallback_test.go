package httpapi

import (
	"os"
	"regexp"
	"testing"

	"yyb_go/internal/qr"
)

// The console pages read the channel list from /api/scan-channels, but the public
// one-time link page cannot, so scan-channel.js carries a built-in copy of the
// registry for it. Adding a channel to the registry without that copy would leave
// the public page unable to name it.
func TestScanChannelFallbackMatchesTheRegistry(t *testing.T) {
	source, err := os.ReadFile("../../resource/static/js/scan-channel.js")
	if err != nil {
		t.Fatal(err)
	}
	entry := regexp.MustCompile(`\{id: '([^']+)', name: '([^']+)', badge: '([^']+)', description: '([^']*)'\}`)
	found := entry.FindAllStringSubmatch(string(source), -1)
	want := qr.ScanChannels()
	if len(found) != len(want) {
		t.Fatalf("scan-channel.js lists %d channels, the registry %d: update the built-in list in resource/static/js/scan-channel.js", len(found), len(want))
	}
	for i, match := range found {
		got := qr.ScanChannel{ID: match[1], Name: match[2], Badge: match[3], Description: match[4]}
		if got != want[i] {
			t.Fatalf("channel %d: scan-channel.js has %+v, the registry %+v", i, got, want[i])
		}
	}
}
