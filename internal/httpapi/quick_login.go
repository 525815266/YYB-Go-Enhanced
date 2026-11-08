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
	Product   string
	State     string
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
	product, err := normalizeLoginProduct(proxyBody.Product)
	if err != nil || product != "appstore" && product != "qbbrowser" {
		writeError(w, http.StatusUnprocessableEntity, "本机微信快捷授权支持应用宝和 QQ 浏览器，其他渠道请扫码")
		return
	}
	oauth, err := qr.OAuthURL(product)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "生成授权参数失败")
		return
	}
	authorize, _ := url.Parse(oauth)
	params := authorize.Query()
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
	a.quickSessions[sessionID] = quickLoginSession{CreatedAt: time.Now(), Product: product, State: params.Get("state"), ProxySpec: proxySpec, ProxyIn: normalizedBody}
	a.mu.Unlock()

	writeJSON(w, http.StatusOK, map[string]any{
		"session_id":   sessionID,
		"appid":        params.Get("appid"),
		"scope":        params.Get("scope"),
		"redirect_uri": params.Get("redirect_uri"),
		"state":        params.Get("state"),
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
	a.mu.Lock()
	expected, exists := a.quickSessions[parts[0]]
	a.mu.Unlock()
	if !exists || time.Since(expected.CreatedAt) > a.cfg.QRSessionTTL {
		writeError(w, http.StatusNotFound, "quick login session expired or not found")
		return
	}
	code, err := parseQuickAuthorizeRedirectForProduct(body.RedirectURL, expected.Product, expected.State)
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
	if session.ProxySpec.Mode == "direct" && session.Product != "qbbrowser" {
		result, err = a.exchangeAuthCode(r.Context(), code)
	} else {
		client, _, err = a.qrClientForSpec(r.Context(), session.ProxySpec)
		if err == nil {
			result, err = client.GetLoginBufferFromCodeForProduct(r.Context(), code, session.Product)
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
	a.autoSyncAfterScan(account)
	writeJSON(w, http.StatusOK, account.Public())
}

func parseQuickAuthorizeRedirect(raw string) (string, error) {
	return parseQuickAuthorizeRedirectForProduct(raw, "appstore", qr.OAuthState)
}

func parseQuickAuthorizeRedirectForProduct(raw, product, state string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	host, path := "yybadaccess.3g.qq.com", "/pc_yyb/pcyyb_oauth"
	if product == "qbbrowser" {
		host, path = "go.browser.qq.com", "/wx-login.html"
	}
	if err != nil || u.Scheme != "https" || !strings.EqualFold(u.Hostname(), host) || u.Port() != "" || u.User != nil || u.Path != path || u.Fragment != "" {
		return "", fmt.Errorf("invalid quick authorization redirect")
	}
	query := u.Query()
	if state == "" && product != "qbbrowser" {
		state = qr.OAuthState
	}
	if query.Get("state") != state || state == "" || len(query["state"]) != 1 || len(query["code"]) != 1 || product != "qbbrowser" && query.Get("login_type") != "WX" {
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
