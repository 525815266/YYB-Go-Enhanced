package httpapi

import (
	"net/http"

	"yyb_go/internal/qr"
)

// handleScanChannels lists the channels an account can be scanned through. The
// scan page, multi-channel scanning and the account cards read it, so a channel
// added to qr.ScanChannels appears in all of them without a page change.
func (a *App) handleScanChannels(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"channels": qr.ScanChannels()})
}
