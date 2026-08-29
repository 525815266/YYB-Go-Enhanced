package httpapi

import (
	"database/sql"
	"errors"
	"net/http"
	"strings"

	"yyb_go/internal/store"
)

type accountRepairIn struct {
	Confirm bool `json:"confirm"`
}

type accountRepairCandidate struct {
	ID     int64  `json:"id"`
	Status string `json:"status"`
	Reason string `json:"reason"`
}

func (a *App) handleAccountRepair(w http.ResponseWriter, r *http.Request) {
	if a.auth != nil && !requireAdmin(w, r) {
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body accountRepairIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}

	candidates, err := a.db.ListIncompleteAccounts(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if !body.Confirm {
		writeJSON(w, http.StatusOK, map[string]any{
			"mode":             "preview",
			"confirm_required": len(candidates) > 0,
			"candidates":       repairCandidates(candidates),
			"note":             "仅会清理未完成扫码残留，不会重排正常账号 ID；释放的 ID 会在下次成功扫码时自动复用。",
		})
		return
	}

	ids := make([]int64, 0, len(candidates))
	skipped := make([]accountRepairCandidate, 0)
	for _, candidate := range candidates {
		// A repaired record must not be owned by a user. This also protects a
		// record that changed between the preview and confirmation requests.
		if a.auth != nil {
			_, ownerErr := a.auth.AccountOwner(r.Context(), candidate.ID)
			if ownerErr == nil {
				skipped = append(skipped, accountRepairCandidate{ID: candidate.ID, Status: accountStatusValue(candidate), Reason: "已绑定用户，未清理"})
				continue
			}
			if !errors.Is(ownerErr, sql.ErrNoRows) && ownerErr != nil {
				skipped = append(skipped, accountRepairCandidate{ID: candidate.ID, Status: accountStatusValue(candidate), Reason: "读取账号归属失败，未清理"})
				continue
			}
		}
		ids = append(ids, candidate.ID)
	}

	removed, err := a.db.DeleteIncompleteAccounts(r.Context(), ids)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	removedSet := make(map[int64]struct{}, len(removed))
	for _, id := range removed {
		removedSet[id] = struct{}{}
		a.invalidateProxyLease(id)
		a.clearKeepAliveRetry(id)
	}
	for _, candidate := range candidates {
		if _, ok := removedSet[candidate.ID]; !ok {
			found := false
			for _, item := range skipped {
				if item.ID == candidate.ID {
					found = true
					break
				}
			}
			if !found {
				skipped = append(skipped, accountRepairCandidate{ID: candidate.ID, Status: accountStatusValue(candidate), Reason: "记录已变化或不存在，未清理"})
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"mode":          "cleaned",
		"removed_count": len(removed),
		"removed_ids":   removed,
		"skipped":       skipped,
		"note":          "正常账号 ID 未重排；释放的 ID 会由后续成功扫码自动复用。",
	})
}

func repairCandidates(accounts []*store.WechatAccount) []accountRepairCandidate {
	out := make([]accountRepairCandidate, 0, len(accounts))
	for _, account := range accounts {
		out = append(out, accountRepairCandidate{ID: account.ID, Status: accountStatusValue(account), Reason: incompleteReason(account)})
	}
	return out
}

func accountStatusValue(account *store.WechatAccount) string {
	if account == nil || account.Status == nil || strings.TrimSpace(*account.Status) == "" {
		return "unknown"
	}
	return strings.TrimSpace(*account.Status)
}

func incompleteReason(account *store.WechatAccount) string {
	if account == nil || strings.TrimSpace(account.OpenID) == "" {
		return "缺少 OpenID"
	}
	return "未完成扫码授权，缺少登录凭据"
}
