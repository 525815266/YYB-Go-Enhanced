package httpapi

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

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
	if link.UsedAt != nil || link.ExpiresAt <= time.Now().Unix() {
		return nil, errors.New("账号授权链接已失效或已使用")
	}
	if link.Kind != accountLinkKindUpdate && link.Kind != accountLinkKindAdd {
		return nil, errors.New("账号授权链接类型无效")
	}
	return link, nil
}

func (a *App) handleAccountLinksAPI(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/api/account-links" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body struct {
		Kind       string `json:"kind"`
		Ref        string `json:"ref"`
		TTLSeconds int64  `json:"ttl_seconds"`
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
	rawToken, tokenHash, err := newAccountLinkToken()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	link, err := a.db.CreateAccountLink(r.Context(), tokenHash, body.Kind, account.ID, ownerUserID, expectedOpenID, time.Now().Add(time.Duration(body.TTLSeconds)*time.Second).Unix())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	_, _ = a.db.PurgeExpiredAccountLinks(r.Context())
	writeJSON(w, http.StatusOK, map[string]any{
		"kind":       link.Kind,
		"url":        a.accountLinkURL(r, rawToken),
		"expires_at": link.ExpiresAt,
		"one_time":   true,
	})
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
	if len(parts) != 4 || parts[1] != "qr" {
		writeError(w, http.StatusNotFound, "account link route not found")
		return
	}
	sessionID, action := parts[2], parts[3]
	login := a.getQRSession(sessionID)
	if login == nil || login.AccountLinkID != link.ID {
		writeError(w, http.StatusNotFound, "二维码会话不存在或不属于此链接")
		return
	}
	switch action {
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
	img, err := client.GetQRCodeImage(r.Context())
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	a.mu.Lock()
	a.qrSessions[img.Session.ID] = &qrLoginSession{
		Session: img.Session, Client: client, ImageBytes: append([]byte(nil), img.ImageBytes...),
		AccountLinkID: link.ID, AccountLinkMode: link.Kind, BaseAccountID: link.AccountID,
	}
	a.mu.Unlock()
	path := a.resources.qrPath(img.Session.ID)
	_ = os.WriteFile(path, img.ImageBytes, 0o644)
	writeJSON(w, http.StatusOK, map[string]any{
		"session_id": img.Session.ID, "status": img.Session.Status,
		"image_url":       "/account-link/" + tokenForLinkPath(r.URL.Path) + "/qr/" + img.Session.ID + "/image",
		"expires_in":      int64(a.cfg.QRSessionTTL.Seconds()),
		"qr_expires_at":   time.Now().Add(a.cfg.QRSessionTTL).Unix(),
		"link_expires_at": link.ExpiresAt,
		"mode":            link.Kind,
	})
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
	if link.Kind == accountLinkKindUpdate && result.Credentials.OpenID != link.ExpectedOpenID {
		dropAfterConfirm = true
		writeError(w, http.StatusForbidden, "扫码账号与目标账号不匹配，未更新任何数据")
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
