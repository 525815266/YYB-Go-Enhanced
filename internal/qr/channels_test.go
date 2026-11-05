package qr

import (
	"os"
	"path/filepath"
	"testing"
)

// A channel in the list is shown to the user, so it must be one the QR flow can
// actually start and must have its badge image; otherwise adding a channel to
// the list alone would put a broken entry on every page.
func TestEveryListedChannelCanStartAScan(t *testing.T) {
	seen := map[string]bool{}
	for _, channel := range ScanChannels() {
		if channel.ID == "" || channel.Name == "" || channel.Badge == "" {
			t.Fatalf("incomplete channel %+v", channel)
		}
		if seen[channel.ID] {
			t.Fatalf("channel %q is listed twice", channel.ID)
		}
		seen[channel.ID] = true
		if !IsScanChannel(channel.ID) {
			t.Fatalf("IsScanChannel(%q) = false", channel.ID)
		}
		if _, err := OAuthURL(channel.ID); err != nil {
			t.Fatalf("channel %q is listed but has no authorization URL: %v", channel.ID, err)
		}
		image := filepath.Join("..", "..", "resource", "static", "images", "scan-"+channel.Badge+".png")
		if _, err := os.Stat(image); err != nil {
			t.Fatalf("channel %q has no badge image: %v", channel.ID, err)
		}
	}
	if len(seen) < 3 {
		t.Fatalf("expected at least the three known channels, got %d", len(seen))
	}
	if IsScanChannel("") || IsScanChannel("nope") {
		t.Fatal("unknown channels must not pass")
	}
	// The list handed out is a copy.
	first := ScanChannels()
	first[0].Name = "changed"
	if ScanChannels()[0].Name == "changed" {
		t.Fatal("ScanChannels returned the internal slice")
	}
}
