package httpapi

import (
	"net/http"

	"yyb_go/internal/version"
)

func (a *App) handleVersion(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	buildVersion, commit, buildDate := version.Info()
	writeJSON(w, http.StatusOK, map[string]any{
		"version":    buildVersion,
		"commit":     commit,
		"build_date": buildDate,
		"update_url": "https://github.com/525815266/YYB-Go-Enhanced/releases",
	})
}
