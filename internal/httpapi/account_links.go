package httpapi

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/qr"
	"yyb_go/internal/store"
)

const (
	accountLinkKindUpdate       = "update"
	accountLinkKindAdd          = "add"
	accountLinkUpdateDefaultTTL = 10 * time.Minute
	accountLinkAddDefaultTTL    = 30 * time.Minute
	accountLinkMaxTTL           = 7 * 24 * time.Hour
)

const accountLinkCleanupInterval = 10 * time.Minute

func (a *App) startAccountLinkCleanup() {
	ctx, cancel := context.WithCancel(context.Background())
	a.accountLinkCancel = cancel
	a.accountLinkDone = make(chan struct{})
	go func() {
		defer close(a.accountLinkDone)
		cleanup := func() {
			if removed, err := a.db.PurgeExpiredAccountLinks(ctx); err != nil && ctx.Err() == nil {
				// Cleanup is best-effort; a failed maintenance pass must not stop
				// the API or make otherwise valid links unusable.
				log.Printf("account-links: cleanup failed: %v", err)
			} else if removed > 0 {
				log.Printf("account-links: removed %d consumed/revoked/expired links", removed)
			}
		}
		cleanup()
		ticker := time.NewTicker(accountLinkCleanupInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				cleanup()
			}
		}
	}()
}

func (a *App) stopAccountLinkCleanup() {
	if a.accountLinkCancel == nil {
		return
	}
	a.accountLinkCancel()
	<-a.accountLinkDone
	a.accountLinkCancel = nil
	a.accountLinkDone = nil
}

func newAccountLinkToken() (string, string, error) {
	buffer := make([]byte, 24)
	if _, err := rand.Read(buffer); err != nil {
		return "", "", fmt.Errorf("generate account link token: %w", err)
	}
	raw := base64.RawURLEncoding.EncodeToString(buffer)
	hash := sha256.Sum256([]byte(raw))
	return raw, hex.EncodeToString(hash[:]), nil
}

func accountLinkTokenHash(raw string) string {
	hash := sha256.Sum256([]byte(strings.TrimSpace(raw)))
	return hex.EncodeToString(hash[:])
}

func validAccountLinkToken(raw string) bool {
	if len(raw) != 32 {
		return false
	}
	for _, char := range raw {
		if (char < 'a' || char > 'z') && (char < 'A' || char > 'Z') && (char < '0' || char > '9') && char != '-' && char != '_' {
			return false
		}
	}
	return true
}

func (a *App) accountLinkForToken(ctx context.Context, raw string) (*store.AccountLink, error) {
	if !validAccountLinkToken(raw) {
		return nil, sql.ErrNoRows
	}
	link, err := a.db.GetAccountLinkByHash(ctx, accountLinkTokenHash(raw))
	if err != nil {
		return nil, err
	}
	if link.UsedAt != nil || link.RevokedAt != nil || link.ExpiresAt <= time.Now().Unix() {
		return nil, errors.New("账号授权链接已失效或已使用")
	}
	if link.Kind != accountLinkKindUpdate && link.Kind != accountLinkKindAdd {
		return nil, errors.New("账号授权链接类型无效")
	}
	return link, nil
}

func (a *App) handleAccountLinksAPI(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/api/account-links")
	if path == "" || path == "/" {
		if r.Method == http.MethodGet {
			a.listAccountLinks(w, r)
			return
		}
	} else {
		parts := strings.Split(strings.Trim(path, "/"), "/")
		if len(parts) == 2 && parts[1] == "revoke" && r.Method == http.MethodPost {
			a.revokeAccountLink(w, r, parts[0])
			return
		}
		if len(parts) == 1 && r.Method == http.MethodDelete {
			a.deleteAccountLink(w, r, parts[0])
			return
		}
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body struct {
		Kind          string `json:"kind"`
		Ref           string `json:"ref"`
		TTLSeconds    int64  `json:"ttl_seconds"`
		Product       string `json:"product"`
		ConfirmSwitch bool   `json:"confirm_switch"`
	}
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	body.Kind = strings.ToLower(strings.TrimSpace(body.Kind))
	if body.Kind != accountLinkKindUpdate && body.Kind != accountLinkKindAdd {
		writeError(w, http.StatusBadRequest, "kind must be update or add")
		return
	}
	if body.TTLSeconds == 0 {
		defaultTTL := accountLinkUpdateDefaultTTL
		if body.Kind == accountLinkKindAdd {
			defaultTTL = accountLinkAddDefaultTTL
		}
		body.TTLSeconds = int64(defaultTTL / time.Second)
	}
	if body.TTLSeconds < 60 || body.TTLSeconds > int64(accountLinkMaxTTL/time.Second) {
		writeError(w, http.StatusBadRequest, "ttl_seconds must be between 60 and 604800")
		return
	}
	account, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	product := body.Product
	if product == "" && body.Kind == accountLinkKindUpdate {
		product = account.Public().ScanProduct
	}
	product, err := a.scanProduct(r, product)
	if err != nil {
		writeError(w, 422, err.Error())
		return
	}
	if body.Kind == accountLinkKindUpdate && product != account.Public().ScanProduct && !body.ConfirmSwitch {
		writeError(w, 422, "请确认切换渠道：将保留账号 ID，并替换 OpenID、凭据和会话")
		return
	}
	var ownerUserID *int64
	if a.auth != nil {
		user := a.browserUser(r)
		if user != nil && user.Role != "admin" {
			ownerUserID = &user.ID
		} else if owner, err := a.auth.AccountOwner(r.Context(), account.ID); err == nil {
			ownerUserID = &owner
		}
	}
	expectedOpenID := ""
	if body.Kind == accountLinkKindUpdate {
		expectedOpenID = account.OpenID
	}
	// Serialize the check and INSERT. Without this guard two browser clicks (or
	// two clients racing) could both observe an empty set and create duplicate
	// one-time links for the same target.
	a.accountLinkMu.Lock()
	defer a.accountLinkMu.Unlock()
	// Remove stale rows before checking. This makes an expired/revoked/used
	// link immediately reusable instead of making the operator wait for the
	// background maintenance pass.
	if _, err := a.db.PurgeExpiredAccountLinks(r.Context()); err != nil {
		writeError(w, http.StatusInternalServerError, "清理旧授权链接失败："+err.Error())
		return
	}
	active, err := a.db.ListActiveAccountLinksForTarget(r.Context(), body.Kind, account.ID, ownerUserID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "读取已有授权链接失败："+err.Error())
		return
	}
	existing := make([]map[string]any, 0, len(active))
	for _, item := range active {
		existing = append(existing, a.accountLinkManagementJSON(r, item))
	}
	if len(existing) > 0 {
		writeRawJSON(w, http.StatusConflict, apiEnvelope{
			Code: http.StatusConflict,
			Msg:  "该账号已有未消费授权链接，请先使用或作废旧链接",
			Data: map[string]any{
				"code":           "active_link_exists",
				"message":        "该账号已有未消费授权链接，请先使用或作废旧链接",
				"existing":       existing,
				"existing_count": len(existing),
			},
		})
		return
	}
	rawToken, tokenHash, err := newAccountLinkToken()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	link, err := a.db.CreateAccountLinkForProduct(r.Context(), tokenHash, a.encryptAccountLinkToken(rawToken), body.Kind, account.ID, ownerUserID, expectedOpenID, time.Now().Add(time.Duration(body.TTLSeconds)*time.Second).Unix(), product, body.ConfirmSwitch)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"kind":           link.Kind,
		"product":        product,
		"url":            a.accountLinkURL(r, rawToken),
		"expires_at":     link.ExpiresAt,
		"one_time":       true,
		"existing":       existing,
		"existing_count": len(existing),
	})
}

func (a *App) accountLinkKey() []byte {
	seed := os.Getenv("YYB_ACCOUNT_LINK_KEY")
	if seed == "" {
		seed = a.cfg.IntegrationToken + ":" + a.cfg.AdminUser + ":" + a.cfg.AdminPassword
	}
	h := sha256.Sum256([]byte("yyb-account-link-key:" + seed))
	return h[:]
}

func (a *App) encryptAccountLinkToken(token string) string {
	block, err := aes.NewCipher(a.accountLinkKey())
	if err != nil {
		return ""
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return ""
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err = rand.Read(nonce); err != nil {
		return ""
	}
	sealed := gcm.Seal(nonce, nonce, []byte(token), nil)
	return base64.RawURLEncoding.EncodeToString(sealed)
}

func (a *App) decryptAccountLinkToken(value string) (string, error) {
	data, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil {
		return "", err
	}
	block, err := aes.NewCipher(a.accountLinkKey())
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	if len(data) < gcm.NonceSize() {
		return "", errors.New("invalid token ciphertext")
	}
	plain, err := gcm.Open(nil, data[:gcm.NonceSize()], data[gcm.NonceSize():], nil)
	if err != nil {
		return "", err
	}
	return string(plain), nil
}

func (a *App) accountLinkManagementJSON(r *http.Request, item store.AccountLinkRecord) map[string]any {
	entry := map[string]any{"id": item.ID, "kind": item.Kind, "account_id": item.AccountID, "owner_user_id": item.OwnerUserID, "expires_at": item.ExpiresAt, "created_at": item.CreatedAt, "status": item.Status, "used_at": item.UsedAt, "revoked_at": item.RevokedAt, "url": "", "product": item.TargetProduct}
	if item.TokenCiphertext != "" {
		if token, err := a.decryptAccountLinkToken(item.TokenCiphertext); err == nil && validAccountLinkToken(token) {
			entry["url"] = a.accountLinkURL(r, token)
		}
	}
	return entry
}

func (a *App) listAccountLinks(w http.ResponseWriter, r *http.Request) {
	var owner *int64
	if user := a.browserUser(r); user != nil && user.Role != "admin" {
		owner = &user.ID
	}
	items, err := a.db.ListAccountLinks(r.Context(), owner)
	if err != nil {
		writeError(w, 500, err.Error())
		return
	}
	accounts, _ := a.db.ListAccounts(r.Context())
	accountMap := map[int64]map[string]any{}
	for _, acc := range accounts {
		if a.accountVisible(r, acc.ID) {
			accountMap[acc.ID] = map[string]any{"id": acc.ID, "openid": acc.OpenID, "nickname": acc.Nickname, "remark": acc.Remark, "alias": acc.Alias}
		}
	}
	out := make([]map[string]any, 0, len(items))
	counts := map[string]int{"active": 0, "used": 0, "expired": 0, "revoked": 0}
	for _, item := range items {
		entry := a.accountLinkManagementJSON(r, item)
		entry["account"] = accountMap[item.AccountID]
		out = append(out, entry)
		counts[item.Status]++
	}
	writeJSON(w, 200, map[string]any{"links": out, "counts": counts})
}

func (a *App) revokeAccountLink(w http.ResponseWriter, r *http.Request, rawID string) {
	id, err := strconv.ParseInt(rawID, 10, 64)
	if err != nil {
		writeError(w, 400, "invalid link id")
		return
	}
	var owner *int64
	if user := a.browserUser(r); user != nil && user.Role != "admin" {
		owner = &user.ID
	}
	if err = a.db.RevokeAccountLink(r.Context(), id, owner); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeError(w, 404, "链接不存在或无权操作")
			return
		}
		writeError(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"revoked": true})
}

func (a *App) deleteAccountLink(w http.ResponseWriter, r *http.Request, rawID string) {
	id, err := strconv.ParseInt(rawID, 10, 64)
	if err != nil {
		writeError(w, 400, "invalid link id")
		return
	}
	var owner *int64
	if user := a.browserUser(r); user != nil && user.Role != "admin" {
		owner = &user.ID
	}
	if err = a.db.DeleteAccountLink(r.Context(), id, owner); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeError(w, 404, "链接不存在或无权操作")
			return
		}
		writeError(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"deleted": true})
}

func (a *App) accountLinkURL(r *http.Request, token string) string {
	scheme := "http"
	if strings.EqualFold(forwardedValue(r.Header.Get("X-Forwarded-Proto")), "https") || strings.EqualFold(forwardedParameter(r.Header.Get("Forwarded"), "proto"), "https") || a.cfg.CookieSecure {
		scheme = "https"
	}
	host := forwardedValue(r.Header.Get("X-Forwarded-Host"))
	if host == "" {
		host = forwardedParameter(r.Header.Get("Forwarded"), "host")
	}
	if !validPublicHost(host) {
		host = r.Host
	}
	if !validPublicHost(host) {
		host = "localhost"
	}
	return scheme + "://" + host + "/account-link/" + token
}

// forwardedValue returns the first value from a comma-separated proxy header.
// Reverse proxies commonly append their own hop, while the first value is the
// public address seen by the browser that initiated the request.
func forwardedValue(value string) string {
	value = strings.TrimSpace(strings.Split(value, ",")[0])
	return strings.Trim(value, "\"")
}

func forwardedParameter(value, name string) string {
	for _, part := range strings.Split(forwardedValue(value), ";") {
		key, raw, ok := strings.Cut(strings.TrimSpace(part), "=")
		if !ok || !strings.EqualFold(strings.TrimSpace(key), name) {
			continue
		}
		return strings.Trim(strings.TrimSpace(raw), "\"")
	}
	return ""
}

func validPublicHost(host string) bool {
	host = strings.TrimSpace(host)
	if host == "" || strings.ContainsAny(host, "/\\?#@\r\n") {
		return false
	}
	if strings.HasPrefix(host, "[") {
		end := strings.IndexByte(host, ']')
		if end < 0 || net.ParseIP(host[1:end]) == nil {
			return false
		}
		if len(host) > end+1 && host[end+1] != ':' {
			return false
		}
		return true
	}
	if strings.Count(host, ":") > 1 {
		return false
	}
	if strings.Contains(host, ":") {
		_, port, err := net.SplitHostPort(host)
		if err != nil || port == "" {
			return false
		}
	}
	return true
}

func (a *App) handleAccountLinkPage(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	token := strings.TrimPrefix(r.URL.Path, "/account-link/")
	if strings.Contains(token, "/") {
		writeError(w, http.StatusNotFound, "account link not found")
		return
	}
	if _, err := a.accountLinkForToken(r.Context(), token); err != nil {
		writeError(w, http.StatusGone, "账号授权链接已失效或不存在")
		return
	}
	path := filepath.Join(a.resources.Templates, "account-link.html")
	data, err := os.ReadFile(path)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "account link page unavailable")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(data)
}

func (a *App) handleAccountLink(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/account-link/")
	parts := strings.Split(path, "/")
	if len(parts) < 2 || parts[0] == "" {
		writeError(w, http.StatusNotFound, "account link not found")
		return
	}
	token := parts[0]
	link, err := a.accountLinkForToken(r.Context(), token)
	if err != nil {
		writeError(w, http.StatusGone, "账号授权链接已失效或不存在")
		return
	}
	if parts[1] == "qr" && len(parts) == 2 {
		a.handleAccountLinkQRCreate(w, r, link)
		return
	}
	if parts[1] == "meta" && len(parts) == 2 && r.Method == http.MethodGet {
		base, err := a.db.GetAccount(r.Context(), link.AccountID)
		if err != nil {
			writeError(w, 410, "基础账号已不存在")
			return
		}
		product, err := a.linkScanProduct(r.Context(), link, base, "")
		if err != nil {
			writeError(w, 500, "读取扫码渠道失败")
			return
		}
		writeJSON(w, 200, map[string]any{"product": product, "source_product": base.Public().ScanProduct, "switch_confirmed": link.SwitchConfirmed, "locked": false, "kind": link.Kind, "target": firstAccountLabel(base.Remark, base.Nickname), "account_id": base.ID, "expires_at": link.ExpiresAt})
		return
	}
	if len(parts) != 4 || parts[1] != "qr" {
		writeError(w, http.StatusNotFound, "account link route not found")
		return
	}
	sessionID, action := parts[2], parts[3]
	login := a.getQRSession(sessionID)
	if login == nil || login.AccountLinkID != link.ID || login.AccountLinkHash != link.TokenHash {
		writeError(w, http.StatusNotFound, "二维码会话不存在或不属于此链接")
		return
	}
	switch action {
	case "cancel":
		if r.Method != http.MethodPost {
			writeError(w, 405, "method not allowed")
			return
		}
		login.mu.Lock()
		login.cancelled = true
		login.mu.Unlock()
		a.dropQRSession(sessionID)
		writeJSON(w, 200, map[string]any{"cancelled": true})
	case "image":
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		w.Header().Set("Content-Type", http.DetectContentType(login.ImageBytes))
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(login.ImageBytes)
	case "poll":
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		result, err := login.Client.PollQRCode(r.Context(), login.Session)
		if err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		if terminalQR(result.Status) {
			a.dropQRSession(sessionID)
		}
		writeJSON(w, http.StatusOK, result)
	case "confirm":
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		a.confirmAccountLinkQR(w, r, token, link, sessionID, login)
	default:
		writeError(w, http.StatusNotFound, "account link route not found")
	}
}

func (a *App) handleAccountLinkQRCreate(w http.ResponseWriter, r *http.Request, link *store.AccountLink) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	base, err := a.db.GetAccount(r.Context(), link.AccountID)
	if err != nil {
		writeError(w, http.StatusGone, "基础账号已不存在")
		return
	}
	var body struct {
		Product       string `json:"product"`
		ConfirmSwitch bool   `json:"confirm_switch"`
	}
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, 400, "请求格式错误")
		return
	}
	product, err := a.linkScanProduct(r.Context(), link, base, body.Product)
	if err != nil {
		writeError(w, 422, err.Error())
		return
	}
	switching := link.Kind == accountLinkKindUpdate && product != base.Public().ScanProduct
	confirmed := body.ConfirmSwitch || (link.SwitchConfirmed && link.TargetProduct == product)
	if switching && !confirmed {
		writeError(w, 422, "请先确认切换渠道后再生成二维码")
		return
	}
	a.pruneQR()
	proxyValue, fallbackDirect, err := a.resolveAccountProxy(r.Context(), base.ID)
	if err != nil {
		writeError(w, http.StatusBadGateway, "解析账号代理失败："+err.Error())
		return
	}
	client := a.qr
	if proxyValue != "" {
		client, err = qr.NewClientWithProxy(a.cfg.RequestTimeout, proxyValue, fallbackDirect)
		if err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
	}
	img, err := client.GetQRCodeImageForProduct(r.Context(), product)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	a.mu.Lock()
	a.qrSessions[img.Session.ID] = &qrLoginSession{
		Session: img.Session, Client: client, ImageBytes: append([]byte(nil), img.ImageBytes...),
		AccountLinkID: link.ID, AccountLinkMode: link.Kind, BaseAccountID: link.AccountID,
		AccountLinkHash: link.TokenHash,
		SwitchConfirmed: switching && confirmed,
	}
	a.mu.Unlock()
	path := a.resources.qrPath(img.Session.ID)
	_ = os.WriteFile(path, img.ImageBytes, 0o644)
	writeJSON(w, http.StatusOK, map[string]any{
		"product":    product,
		"session_id": img.Session.ID, "status": img.Session.Status,
		"image_url":       "/account-link/" + tokenForLinkPath(r.URL.Path) + "/qr/" + img.Session.ID + "/image",
		"expires_in":      int64(a.cfg.QRSessionTTL.Seconds()),
		"qr_expires_at":   time.Now().Add(a.cfg.QRSessionTTL).Unix(),
		"link_expires_at": link.ExpiresAt,
		"mode":            link.Kind,
	})
}

func (a *App) linkScanProduct(ctx context.Context, link *store.AccountLink, base *store.WechatAccount, explicit string) (string, error) {
	if explicit != "" {
		return normalizeLoginProduct(explicit)
	}
	if link.TargetProduct != "" {
		return normalizeLoginProduct(link.TargetProduct)
	}
	if link.Kind == accountLinkKindUpdate {
		return base.Public().ScanProduct, nil
	}
	var owner int64
	if link.OwnerUserID != nil {
		owner = *link.OwnerUserID
	}
	global, personal, err := a.scanDefaults(ctx, owner)
	if err != nil {
		return "", err
	}
	if personal != "" {
		return personal, nil
	}
	return global, nil
}

func tokenForLinkPath(path string) string {
	parts := strings.Split(strings.TrimPrefix(path, "/account-link/"), "/")
	if len(parts) > 0 {
		return parts[0]
	}
	return ""
}

func (a *App) confirmAccountLinkQR(w http.ResponseWriter, r *http.Request, token string, link *store.AccountLink, sessionID string, login *qrLoginSession) {
	result, err := a.getLoginBufferWithRetry(r.Context(), login)
	if err != nil {
		writeError(w, http.StatusConflict, "buffer not ready: "+err.Error())
		return
	}
	login.mu.Lock()
	dropAfterConfirm := false
	defer func() {
		login.mu.Unlock()
		if dropAfterConfirm {
			a.dropQRSession(sessionID)
		}
	}()
	if login.cancelled {
		writeError(w, http.StatusConflict, "qr session cancelled")
		return
	}
	if _, err := a.accountLinkForToken(r.Context(), token); err != nil {
		writeError(w, http.StatusGone, "账号授权链接已失效或已使用")
		return
	}
	if link.Kind == accountLinkKindUpdate && !login.SwitchConfirmed && result.Credentials.OpenID != link.ExpectedOpenID {
		// Keep the QR session and one-time link alive: the operator may have
		// opened the link on another phone or scanned with the wrong account.
		writeError(w, http.StatusConflict, "请使用目标账号扫码：该链接仅限指定账号更新，当前账号未被修改")
		return
	}
	product, err := normalizeLoginProduct(result.Credentials.Provider)
	if err != nil || product != login.Session.Product {
		writeError(w, 409, "授权渠道与二维码不一致，请重新生成二维码")
		return
	}
	if link.Kind == accountLinkKindAdd {
		existed, err := a.accountExistsBeforeScan(r.Context(), result.Credentials.OpenID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		if existed {
			dropAfterConfirm = true
			writeError(w, http.StatusConflict, "该微信账号已存在，新增链接只接受未录入的 YYB 账号")
			return
		}
	}
	var userInfo map[string]any
	if ui, err := login.Client.LoginBuffers().FetchUserInfo(r.Context(), result.Credentials); err == nil {
		userInfo = ui
	}
	if link.Kind == accountLinkKindUpdate {
		before, err := a.db.GetAccount(r.Context(), link.AccountID)
		if err != nil {
			writeError(w, 410, "目标账号已不存在")
			return
		}
		if before.OpenID != link.ExpectedOpenID {
			writeError(w, 410, "账号授权已更新，请重新生成链接")
			return
		}
		if login.SwitchConfirmed && login.Session.Product == before.Public().ScanProduct {
			writeError(w, 409, "账号渠道已变化，请重新生成二维码")
			return
		}
		// Share the lock with keepalive. Its CAS also rejects any older snapshot.
		lock := a.refreshLockFor(before.ID)
		select {
		case lock <- struct{}{}:
		case <-r.Context().Done():
			return
		}
		nick := pickNickname(userInfo, result.Credentials.Nickname)
		avatar := a.resolveAvatar(r.Context(), result.Credentials.OpenID, userInfo)
		acc, err := a.db.UpdateAccountFromLink(r.Context(), link, result.Credentials.OpenID, result.LoginBuffer, stringPtrMaybe(nick), stringPtrMaybe(avatar), userInfo, result.Credentials.ToMapForScan())
		<-lock
		if errors.Is(err, store.ErrAccountIdentityConflict) {
			writeError(w, 409, "该渠道身份已属于其他账号，请先处理重复账号；原账号未修改")
			return
		}
		if errors.Is(err, store.ErrAccountLinkChanged) {
			writeError(w, 410, "授权链接已消费或目标账号已更新")
			return
		}
		if err != nil {
			writeError(w, 500, "保存账号更新失败")
			return
		}
		a.invalidateProxyLease(acc.ID)
		a.clearKeepAliveRetry(acc.ID)
		a.events.Emit("account.scanned", eventlog.ForAccount(acc))
		warning := a.syncUpdatedAccountReferences(acc)
		dropAfterConfirm = true
		writeJSON(w, 200, struct {
			store.AccountPublic
			Warning string `json:"warning,omitempty"`
		}{acc.Public(), warning})
		return
	}
	consumed, err := a.db.ConsumeAccountLink(r.Context(), link.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if !consumed {
		writeError(w, http.StatusGone, "账号授权链接已失效或已使用")
		return
	}
	existed, err := a.accountExistsBeforeScan(r.Context(), result.Credentials.OpenID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	acc, err := a.storeFromScan(r.Context(), result.LoginBuffer, result.Credentials, userInfo)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := a.claimLinkAccount(r.Context(), link, acc.ID); err != nil {
		if !existed {
			_ = a.db.DeleteAccount(r.Context(), acc.ID)
		}
		writeError(w, http.StatusForbidden, err.Error())
		return
	}
	if link.Kind == accountLinkKindAdd && !existed {
		if err := a.db.CopyAccountProxySetting(r.Context(), link.AccountID, acc.ID); err != nil {
			_ = a.db.DeleteAccount(r.Context(), acc.ID)
			writeError(w, http.StatusInternalServerError, "复制基础账号代理失败："+err.Error())
			return
		}
	}
	a.autoSyncAfterScan(acc)
	// The link is one-time. Keep the used marker only long enough to win the
	// atomic race above, then remove the row and release its management ID.
	if err := a.db.DeleteAccountLink(r.Context(), link.ID, link.OwnerUserID); err != nil {
		// The account has already been updated successfully; leave the row for
		// the periodic cleanup pass if a transient delete error occurs.
		log.Printf("account-links: delete consumed link id=%d: %v", link.ID, err)
	}
	dropAfterConfirm = true
	writeJSON(w, http.StatusOK, acc.Public())
}

func (a *App) claimLinkAccount(ctx context.Context, link *store.AccountLink, accountID int64) error {
	if a.auth == nil || link.OwnerUserID == nil {
		return nil
	}
	owner, err := a.auth.AccountOwner(ctx, accountID)
	if err == nil && owner != *link.OwnerUserID {
		return errors.New("该 YYB 账号已属于其他用户")
	}
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	return a.auth.ClaimAccount(ctx, accountID, *link.OwnerUserID)
}
