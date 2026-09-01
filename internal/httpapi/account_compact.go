package httpapi

import (
	"context"
	"net/http"
	"strconv"
	"strings"

	"yyb_go/internal/store"
)

type accountCompactIn struct {
	Confirm bool `json:"confirm"`
}

type accountCompactItem struct {
	OldID  int64  `json:"old_id"`
	NewID  int64  `json:"new_id"`
	Label  string `json:"label"`
	OpenID string `json:"openid"`
}

func accountCompactPlan(accounts []*store.WechatAccount) ([]accountCompactItem, map[int64]int64) {
	items := make([]accountCompactItem, 0, len(accounts))
	mapping := make(map[int64]int64)
	for index, account := range accounts {
		if account == nil {
			continue
		}
		newID := int64(index + 1)
		if account.ID == newID {
			continue
		}
		mapping[account.ID] = newID
		label := firstAccountLabel(account.Remark, account.Nickname, account.Alias)
		if label == "" {
			label = "未命名账号"
		}
		items = append(items, accountCompactItem{OldID: account.ID, NewID: newID, Label: label, OpenID: account.OpenID})
	}
	return items, mapping
}

// handleAccountCompact safely closes numeric holes in the account list. It is
// deliberately separate from /accounts/repair, which only removes scan junk.
func (a *App) handleAccountCompact(w http.ResponseWriter, r *http.Request) {
	if a.auth != nil && !requireAdmin(w, r) {
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body accountCompactIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	accounts, err := a.db.ListAccounts(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	items, mapping := accountCompactPlan(accounts)
	if !body.Confirm {
		writeJSON(w, http.StatusOK, map[string]any{
			"mode": "preview", "confirm_required": len(mapping) > 0,
			"changes": items,
			"note":    "仅迁移现有账号到连续 ID，不删除账号；会同步会话、代理、推送、脚本任务和用户归属。",
		})
		return
	}
	if len(mapping) == 0 {
		writeJSON(w, http.StatusOK, map[string]any{"mode": "compacted", "changed_count": 0, "changes": []accountCompactItem{}})
		return
	}
	// Ownership is stored separately. Move it first; if the YYB database fails,
	// restore ownership with the inverse map so the two stores stay aligned.
	if a.auth != nil {
		if err = a.auth.RemapAccountIDs(r.Context(), mapping); err != nil {
			writeError(w, http.StatusInternalServerError, "迁移用户归属失败："+err.Error())
			return
		}
	}
	if _, err = a.db.CompactAccountIDs(r.Context()); err != nil {
		if a.auth != nil {
			_ = a.auth.RemapAccountIDs(r.Context(), invertAccountIDMap(mapping))
		}
		writeError(w, http.StatusInternalServerError, "迁移账号 ID 失败："+err.Error())
		return
	}
	for oldID := range mapping {
		a.invalidateProxyLease(oldID)
		a.clearKeepAliveRetry(oldID)
	}
	for _, newID := range mapping {
		a.invalidateProxyLease(newID)
		a.clearKeepAliveRetry(newID)
	}
	qinglongSynced := false
	if a.qinglong.configured() {
		qinglongSynced = remapYYBServerIDs(r.Context(), a.qinglong, mapping)
		if updatedAccounts, listErr := a.db.ListAccounts(r.Context()); listErr == nil {
			jobsSynced := true
			for _, account := range updatedAccounts {
				setting, settingErr := a.db.AccountPushSettingOrDefault(r.Context(), account.ID)
				if settingErr != nil || a.refreshAccountJobCommands(r.Context(), account, setting) != nil {
					jobsSynced = false
				}
			}
			qinglongSynced = qinglongSynced || jobsSynced
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"mode": "compacted", "changed_count": len(items), "changes": items,
		"qinglong_synced": qinglongSynced,
		"note":            "账号数据已迁移到连续 ID；已配置面板时会同步 YYB_SERVER 和托管任务，未托管的外部脚本请自行检查旧 ID。",
	})
}

func invertAccountIDMap(mapping map[int64]int64) map[int64]int64 {
	out := make(map[int64]int64, len(mapping))
	for oldID, newID := range mapping {
		out[newID] = oldID
	}
	return out
}

func remapYYBServerIDs(ctx context.Context, client *qingLongClient, mapping map[int64]int64) bool {
	envs, err := client.listEnvs(ctx, "YYB_SERVER")
	if err != nil {
		return false
	}
	changed := false
	for _, env := range envs {
		if env.Name != "YYB_SERVER" {
			continue
		}
		lines := strings.Split(strings.ReplaceAll(env.Value, "\r\n", "\n"), "\n")
		envChanged := false
		for index, line := range lines {
			trimmed := strings.TrimSpace(line)
			separator := strings.LastIndex(trimmed, "@")
			if separator < 0 {
				continue
			}
			ref := strings.TrimSpace(trimmed[separator+1:])
			oldID, parseErr := strconv.ParseInt(ref, 10, 64)
			if parseErr != nil {
				continue
			}
			newID, ok := mapping[oldID]
			if !ok {
				continue
			}
			lines[index] = trimmed[:separator+1] + strconv.FormatInt(newID, 10)
			changed = true
			envChanged = true
		}
		if envChanged {
			if err := client.updateEnvEntry(ctx, env, strings.Join(lines, "\n")); err != nil {
				return false
			}
		}
	}
	return changed
}
