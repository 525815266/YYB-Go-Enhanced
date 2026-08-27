package httpapi

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/proxysource"
	"yyb_go/internal/qr"
)

var desktopWechatPorts = []int{14013, 14014, 14015, 13013, 13014, 13015}

type quickLoginSession struct {
	CreatedAt time.Time
	ProxySpec proxysource.Spec
	ProxyIn   accountProxyIn
}

func (a *App) handleQuickLoginRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/quick-login" {
		writeError(w, http.StatusNotFound, "quick login session not found")
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if !a.cfg.EnablePCLogin {
		writeError(w, http.StatusNotFound, "PC 登录已关闭，请使用微信扫码授权")
		return
	}

	a.pruneQuickSessions()
	var proxyBody accountProxyIn
	if err := decodeOptionalJSON(r, &proxyBody); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if _, err := normalizeLoginProduct(proxyBody.Product); err != nil {
		writeError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	normalizedBody, proxySpec, err := a.normalizeAccountProxyInput(r.Context(), proxyBody)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	sessionID, err := randomSessionID()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.mu.Lock()
	a.quickSessions[sessionID] = quickLoginSession{CreatedAt: time.Now(), ProxySpec: proxySpec, ProxyIn: normalizedBody}
	a.mu.Unlock()

	writeJSON(w, http.StatusOK, map[string]any{
		"session_id":   sessionID,
		"appid":        qr.AppID,
		"scope":        qr.OAuthScope,
		"redirect_uri": qr.OAuthRedirectURI,
		"state":        qr.OAuthState,
		"ports":        desktopWechatPorts,
		"expires_in":   int64(a.cfg.QRSessionTTL.Seconds()),
	})
}

func (a *App) handleQuickLogin(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/quick-login/"), "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] != "confirm" {
		writeError(w, http.StatusNotFound, "quick login session not found")
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if !a.cfg.EnablePCLogin {
		writeError(w, http.StatusNotFound, "PC 登录已关闭，请使用微信扫码授权")
		return
	}

	var body struct {
		RedirectURL string `json:"redirect_url"`
	}
	decoder := json.NewDecoder(io.LimitReader(r.Body, 8<<10))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if err := ensureJSONEOF(decoder); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	code, err := parseQuickAuthorizeRedirect(body.RedirectURL)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	session, ok := a.takeQuickSession(parts[0])
	if !ok {
		writeError(w, http.StatusNotFound, "quick login session expired or not found")
		return
	}

	client := a.qr
	var result protocol.LoginBufferResult
	if session.ProxySpec.Mode == "direct" {
		result, err = a.exchangeAuthCode(r.Context(), code)
	} else {
		client, _, err = a.qrClientForSpec(r.Context(), session.ProxySpec)
		if err == nil {
			result, err = client.GetLoginBufferFromCode(r.Context(), code)
		}
	}
	if err != nil {
		writeError(w, http.StatusConflict, "quick authorization failed: "+err.Error())
		return
	}
	var userInfo map[string]any
	if session.ProxySpec.Mode == "direct" {
		if ui, err := a.fetchUserInfo(r.Context(), result.Credentials); err == nil {
			userInfo = ui
		}
	} else if ui, err := client.LoginBuffers().FetchUserInfo(r.Context(), result.Credentials); err == nil {
		userInfo = ui
	}
	existed, err := a.accountExistsBeforeScan(r.Context(), result.Credentials.OpenID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := a.ensureScannedAccountAllowed(r, result.Credentials.OpenID); err != nil {
		writeError(w, http.StatusForbidden, err.Error())
		return
	}
	account, err := a.storeFromScan(r.Context(), result.LoginBuffer, result.Credentials, userInfo)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := a.claimScannedAccount(r, account.ID); err != nil {
		if !existed {
			_ = a.db.DeleteAccount(r.Context(), account.ID)
		}
		writeError(w, http.StatusForbidden, err.Error())
		return
	}
	if err := a.saveNewAccountProxy(r.Context(), account.ID, existed, session.ProxyIn, session.ProxySpec); err != nil {
		if !existed {
			_ = a.db.DeleteAccount(r.Context(), account.ID)
		}
		writeError(w, http.StatusInternalServerError, "保存账号代理失败: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, account.Public())
}

func parseQuickAuthorizeRedirect(raw string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme != "https" || !strings.EqualFold(u.Hostname(), "yybadaccess.3g.qq.com") || u.Port() != "" || u.User != nil || u.Path != "/pc_yyb/pcyyb_oauth" {
		return "", fmt.Errorf("invalid quick authorization redirect")
	}
	query := u.Query()
	if query.Get("login_type") != "WX" || query.Get("state") != qr.OAuthState {
		return "", fmt.Errorf("invalid quick authorization state")
	}
	code := strings.TrimSpace(query.Get("code"))
	if code == "" || len(code) > 2048 {
		return "", fmt.Errorf("quick authorization code is missing or invalid")
	}
	return code, nil
}

func ensureJSONEOF(decoder *json.Decoder) error {
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return fmt.Errorf("unexpected trailing JSON")
	}
	return nil
}

func randomSessionID() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("generate quick login session: %w", err)
	}
	return hex.EncodeToString(b), nil
}

func (a *App) takeQuickSession(sessionID string) (quickLoginSession, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	session, ok := a.quickSessions[sessionID]
	if !ok {
		return quickLoginSession{}, false
	}
	delete(a.quickSessions, sessionID)
	return session, time.Since(session.CreatedAt) <= a.cfg.QRSessionTTL
}

func (a *App) pruneQuickSessions() {
	a.mu.Lock()
	defer a.mu.Unlock()
	for sessionID, session := range a.quickSessions {
		if time.Since(session.CreatedAt) > a.cfg.QRSessionTTL {
			delete(a.quickSessions, sessionID)
		}
	}
}
