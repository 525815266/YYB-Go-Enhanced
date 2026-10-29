package httpapi

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"sort"
	"strconv"
	"time"

	"yyb_go/internal/store"
)

func (a *App) accountMergePreviewKey(source, target *store.WechatAccount) string {
	mac := hmac.New(sha256.New, a.accountLinkKey())
	for _, acc := range []*store.WechatAccount{source, target} {
		// Include the credential snapshot without exposing it in the preview response.
		encoded, _ := json.Marshal([]any{acc.ID, acc.OpenID, acc.LoginBuffer, acc.Credentials})
		mac.Write(encoded)
	}
	return hex.EncodeToString(mac.Sum(nil))
}

func (a *App) handleAccountMerge(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, 405, "method not allowed")
		return
	}
	var body struct {
		Source     string `json:"source_ref"`
		Target     string `json:"target_ref"`
		Confirm    bool   `json:"confirm"`
		PreviewKey string `json:"preview_key"`
	}
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, 400, "请求格式错误")
		return
	}
	source, ok := a.resolveAccountRef(w, r, body.Source)
	if !ok {
		return
	}
	target, ok := a.resolveAccountRef(w, r, body.Target)
	if !ok {
		return
	}
	if source.ID == target.ID {
		writeError(w, 422, "来源和保留 ID 不能相同")
		return
	}
	var owner int64
	if a.auth != nil {
		sourceOwner, sourceErr := a.auth.AccountOwner(r.Context(), source.ID)
		targetOwner, targetErr := a.auth.AccountOwner(r.Context(), target.ID)
		if sourceErr != nil && !errors.Is(sourceErr, sql.ErrNoRows) || targetErr != nil && !errors.Is(targetErr, sql.ErrNoRows) {
			writeError(w, 500, "读取账号归属失败")
			return
		}
		if sourceOwner != targetOwner {
			writeError(w, 409, "两个账号归属不同，不能合并；请先核对归属")
			return
		}
		owner = sourceOwner
	}
	jobs, err := a.db.ListAccountScriptJobs(r.Context(), source.ID)
	if err != nil {
		writeError(w, 500, "读取来源任务失败")
		return
	}
	if len(jobs) > 0 {
		writeError(w, 409, "来源账号已有专属任务，请先在账号调度中删除来源任务，再合并；目标账号任务会保留")
		return
	}
	if source.LoginBuffer == "" || len(source.Credentials) == 0 || accountStatus(source) == "expired" {
		writeError(w, 422, "来源账号授权不可用，请先扫码更新来源账号")
		return
	}
	key := a.accountMergePreviewKey(source, target)
	if !body.Confirm {
		writeJSON(w, 200, map[string]any{"mode": "preview", "preview_key": key, "source": source.Public(), "target": target.Public(), "note": "保留目标 ID、代理、归属、备注、任务和推送设置；使用来源账号的授权并删除来源 ID。无法自动证明两条记录属于同一微信，请核对身份。"})
		return
	}
	if !hmac.Equal([]byte(key), []byte(body.PreviewKey)) {
		writeError(w, 409, "账号授权已变化，请重新预览后确认")
		return
	}
	// Same order for every merge, sharing locks with credential renewal/rescan.
	ids := []int64{source.ID, target.ID}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	for _, id := range ids {
		lock := a.refreshLockFor(id)
		select {
		case lock <- struct{}{}:
			defer func() { <-lock }()
		case <-r.Context().Done():
			return
		}
	}
	if owner > 0 {
		if err = a.auth.RemoveAccountOwner(r.Context(), source.ID, owner); err != nil {
			writeError(w, 409, "来源归属已变化，请重新检查")
			return
		}
	}
	merged, err := a.db.MergeAccountAuthorization(r.Context(), source, target)
	if err != nil {
		if owner > 0 {
			rollbackCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			_ = a.auth.ClaimAccount(rollbackCtx, source.ID, owner)
			cancel()
		}
		if errors.Is(err, store.ErrAccountAuthorizationChanged) {
			writeError(w, 409, "账号授权已变化，请重新预览")
		} else if errors.Is(err, store.ErrMergeSourceHasJobs) {
			writeError(w, 409, "来源已创建专属任务，请先处理任务")
		} else {
			writeError(w, 500, "合并失败，账号未更改")
		}
		return
	}
	for _, id := range ids {
		a.invalidateProxyLease(id)
		a.clearKeepAliveRetry(id)
	}
	warning := a.syncUpdatedAccountReferences(merged)
	writeJSON(w, 200, map[string]any{"mode": "merged", "account": merged.Public(), "removed_id": source.ID, "warning": warning, "note": "原 ID 已保留，来源 ID " + strconv.FormatInt(source.ID, 10) + " 已释放；手动维护的外部引用请检查"})
}
