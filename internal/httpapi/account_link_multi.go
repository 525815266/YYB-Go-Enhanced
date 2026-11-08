package httpapi

import (
	"context"
	"database/sql"
	"errors"
	"net/http"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

// A multi-channel capability stays anchored to its original fleet. QQBrowser
// and LDPlayer can require explicit confirmation because their identity values
// are not always comparable with the primary channel's open-platform identity.
func (a *App) confirmMultiAccountLinkQR(w http.ResponseWriter, r *http.Request, token string, login *qrLoginSession) {
	var consent struct {
		ConfirmSameWechat bool `json:"confirm_same_wechat"`
	}
	if err := decodeOptionalJSON(r, &consent); err != nil {
		writeError(w, 400, "invalid JSON")
		return
	}
	result, err := a.getLoginBufferWithRetry(r.Context(), login)
	if err != nil {
		writeError(w, 409, "buffer not ready: "+err.Error())
		return
	}
	login.mu.Lock()
	defer login.mu.Unlock()
	if login.cancelled {
		writeError(w, 409, "qr session cancelled")
		return
	}
	a.accountLinkMu.Lock()
	defer a.accountLinkMu.Unlock()
	link, err := a.accountLinkForToken(r.Context(), token)
	if err != nil || !link.AllowMultiChannel {
		writeError(w, 410, "授权链接已失效")
		return
	}
	if login.LinkSavedAccount != nil {
		fresh, err := a.db.GetAccount(r.Context(), login.LinkSavedAccount.ID)
		if err != nil || fresh.OpenID != login.LinkSavedAccount.OpenID || a.checkMultiLinkOwner(r.Context(), link, fresh.ID) != nil {
			writeError(w, 409, "已保存的账号已变化，请刷新页面核对")
			return
		}
		if accountStatus(fresh) != "alive" {
			writeError(w, 502, "授权已归入原账号，但协议会话未就绪，请在控制台核对状态")
			return
		}
		writeJSON(w, 200, fresh.Public())
		return
	}
	product, err := normalizeLoginProduct(result.Credentials.Provider)
	if err != nil || product != login.Session.Product {
		writeError(w, 409, "授权渠道与二维码不一致")
		return
	}
	info, infoErr := login.Client.LoginBuffers().FetchUserInfo(r.Context(), result.Credentials)
	manualChannel := product == "ldplayer" || product == "qbbrowser"
	confirmed := manualChannel && consent.ConfirmSameWechat
	needsConfirmation := func() {
		writeRawJSON(w, 422, apiEnvelope{Code: 422, Msg: "此渠道身份无法与原渠道自动核对，请确认各渠道使用同一微信后继续，无需重新扫码", Data: map[string]any{"identity_confirmation_required": true}})
	}
	base, err := a.db.GetAccount(r.Context(), link.AccountID)
	if err != nil {
		writeError(w, 410, "基础账号已不存在")
		return
	}
	if err = a.checkMultiLinkOwner(r.Context(), link, base.ID); err != nil {
		writeError(w, 409, "基础账号归属已变化，请重新生成链接")
		return
	}
	var primary, target *store.WechatAccount
	if link.MultiPrimaryID != nil {
		primary, err = a.db.GetAccount(r.Context(), *link.MultiPrimaryID)
		if err != nil {
			writeError(w, 410, "主账号已不存在")
			return
		}
		if err = a.checkMultiLinkOwner(r.Context(), link, primary.ID); err != nil {
			writeError(w, 409, "主账号归属与链接不一致")
			return
		}
		manualChannel = manualChannel || primary.ScanProduct() == "ldplayer" || primary.ScanProduct() == "qbbrowser"
		confirmed = manualChannel && consent.ConfirmSameWechat
		uid := fleetUnionIDFromMap(primary.UserInfo)
		var identityErr error
		if uid == "" && !confirmed {
			uid, identityErr = a.multiLinkUnionIDOf(r.Context(), primary)
		}
		sameChannelIdentity := primary.ScanProduct() == product && product != "ldplayer" && primary.OpenID == result.Credentials.OpenID
		if !sameChannelIdentity && (infoErr != nil || identityErr != nil || uid == "" || uid != fleetUnionIDFromMap(info)) {
			if !manualChannel || primary.ScanProduct() == product && product != "ldplayer" {
				writeError(w, 409, "请使用原微信扫码：微信身份无法核对，账号未修改")
				return
			}
			if !confirmed {
				needsConfirmation()
				return
			}
		}
		if primary.ScanProduct() == product && product != "ldplayer" && primary.OpenID != result.Credentials.OpenID {
			writeError(w, 409, "同渠道微信身份不一致，原账号未修改")
			return
		}
		if link.Kind == accountLinkKindUpdate && base.ID != primary.ID && !confirmed {
			baseUID, err := a.multiLinkUnionIDOf(r.Context(), base)
			if (err != nil || baseUID == "" || baseUID != uid) && !confirmed {
				writeError(w, 409, "基础账号与主账号身份无法核对，请先在控制台处理归组")
				return
			}
		}
		members, err := a.db.FleetMembers(r.Context(), primary.ID)
		if err != nil {
			writeError(w, 500, "读取多渠道账号失败")
			return
		}
		for _, member := range members {
			if member.Public().ScanProduct != product {
				continue
			}
			if target != nil || member.OpenID != result.Credentials.OpenID && !(product == "ldplayer" && confirmed && accountStatus(member) == "expired") {
				writeError(w, 409, "该渠道已有不同授权，请在控制台核对；原账号未修改")
				return
			}
			target = member
		}
	}
	if confirmed {
		if info == nil {
			info = make(map[string]any)
		}
		info["identity_binding"] = "operator_confirmed"
	}
	if existing, err := a.db.GetAccountByOpenID(r.Context(), result.Credentials.OpenID); err == nil {
		if target == nil || target.ID != existing.ID {
			writeError(w, 409, "该渠道身份已录入，请在控制台核对归组，链接未消耗")
			return
		}
	} else if !errors.Is(err, sql.ErrNoRows) {
		writeError(w, 500, "读取已有账号失败")
		return
	}
	if target != nil {
		if err = a.checkMultiLinkOwner(r.Context(), link, target.ID); err != nil {
			writeError(w, 409, "渠道账号归属与链接不一致")
			return
		}
	}
	var claimedID int64
	claim := func(id int64, created bool) error {
		if !created {
			return a.checkMultiLinkOwner(r.Context(), link, id)
		}
		if a.auth == nil {
			return nil
		}
		if _, err := a.auth.AccountOwner(r.Context(), id); !errors.Is(err, sql.ErrNoRows) {
			return errors.New("账号 ID 存在归属记录，请在控制台处理")
		}
		if link.OwnerUserID == nil {
			return nil
		}
		if err := a.auth.ClaimAccount(r.Context(), id, *link.OwnerUserID); err != nil {
			return err
		}
		claimedID = id
		return a.checkMultiLinkOwner(r.Context(), link, id)
	}
	var lock chan struct{}
	if target != nil {
		lock = a.refreshLockFor(target.ID)
		select {
		case lock <- struct{}{}:
		case <-r.Context().Done():
			return
		}
		defer func() { <-lock }()
	}
	nick := pickNickname(info, result.Credentials.Nickname)
	avatar := a.resolveAvatar(r.Context(), result.Credentials.OpenID, info)
	acc, err := a.db.SaveAccountLinkScan(r.Context(), link, primary, target, product, result.Credentials.OpenID, result.LoginBuffer,
		stringPtrMaybe(nick), stringPtrMaybe(avatar), info, result.Credentials.ToMapForScan(), claim, confirmed)
	if err != nil {
		if claimedID > 0 {
			_ = a.auth.RemoveAccountOwner(context.Background(), claimedID, *link.OwnerUserID)
		}
		if errors.Is(err, store.ErrAccountLinkChanged) || errors.Is(err, store.ErrAccountIdentityConflict) {
			writeError(w, 409, "链接或账号已变化，请刷新页面后重试")
		} else {
			writeError(w, 500, "保存渠道授权失败，未完成本次写入")
		}
		return
	}
	public := acc.Public()
	login.LinkSavedAccount = &public
	a.invalidateProxyLease(acc.ID)
	a.clearKeepAliveRetry(acc.ID)
	a.circuit.forget(acc.ID)
	a.events.Emit("account.scanned", eventlog.ForAccount(acc))
	if err := a.activateScannedAccount(r.Context(), acc); err != nil {
		writeProtocolCallError(w, err)
		return
	}
	// Fleet members are saved separately but exported as their primary only.
	a.autoSyncAfterScan(acc)
	writeJSON(w, 200, public)
}

func (a *App) checkMultiLinkOwner(ctx context.Context, link *store.AccountLink, id int64) error {
	if a.auth == nil {
		return nil
	}
	owner, err := a.auth.AccountOwner(ctx, id)
	if link.OwnerUserID == nil && errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if link.OwnerUserID == nil || owner != *link.OwnerUserID {
		return errors.New("账号归属不同")
	}
	return nil
}

func (a *App) multiLinkUnionIDOf(ctx context.Context, acc *store.WechatAccount) (string, error) {
	if uid := fleetUnionIDFromMap(acc.UserInfo); uid != "" {
		return uid, nil
	}
	proxy, fallback, err := a.resolveAccountProxy(ctx, acc.ID)
	if err != nil {
		return "", err
	}
	info, err := a.fetchUserInfoWithProxy(ctx, protocol.CredentialsFromMap(acc.Credentials), proxy, fallback)
	if err != nil {
		return "", err
	}
	return fleetUnionIDFromMap(info), nil
}

func (a *App) finishMultiAccountLink(w http.ResponseWriter, r *http.Request, token string) {
	if r.Method != http.MethodPost {
		writeError(w, 405, "method not allowed")
		return
	}
	a.accountLinkMu.Lock()
	defer a.accountLinkMu.Unlock()
	link, err := a.accountLinkForToken(r.Context(), token)
	if err != nil {
		writeError(w, 410, "授权链接已失效")
		return
	}
	if !link.AllowMultiChannel {
		writeError(w, 422, "该链接未开放多渠道授权")
		return
	}
	ok, err := a.db.FinishAccountLinkScans(r.Context(), link)
	if err != nil {
		writeError(w, 500, "结束授权失败，请重试")
		return
	}
	if !ok {
		writeError(w, 409, "请至少完成一个渠道授权后再结束")
		return
	}
	writeJSON(w, 200, map[string]any{"completed": true})
}
