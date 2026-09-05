package httpapi

import (
	"encoding/json"
	"net/http"

)

// handleAccountStatus lets an operator resolve an inconclusive account check.
// Expired accounts remain visible locally, while their QingLong bindings and
// cached sessions are removed so scripts cannot call them accidentally.
func (a *App) handleAccountStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if a.auth != nil && !requireAdmin(w, r) {
		return
	}
	var body struct {
		Ref    string `json:"ref"`
		Status string `json:"status"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON")
		return
	}
	if body.Status != "alive" && body.Status != "expired" {
		writeError(w, http.StatusBadRequest, "status must be alive or expired")
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	if err := a.db.SetAccountStatus(r.Context(), acc.ID, body.Status); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	cleanup := qingLongAccountCleanup{Status: "skipped"}
	if body.Status == "expired" {
		var err error
		cleanup, err = a.cleanupAccountFromQingLong(r.Context(), acc)
		if err != nil {
			writeError(w, http.StatusBadGateway, "账号已标记失效，但青龙清理失败："+err.Error())
			return
		}
		_ = a.db.InvalidateAccountSessions(r.Context(), acc.ID)
		a.invalidateProxyLease(acc.ID)
		a.clearKeepAliveRetry(acc.ID)
	}
	updated, _ := a.db.GetAccount(r.Context(), acc.ID)
	if updated == nil {
		updated = acc
	}
	writeJSON(w, http.StatusOK, map[string]any{"account": updated.Public(), "status": body.Status, "qinglong_cleanup": cleanup})
}
