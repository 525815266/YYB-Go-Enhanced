package httpapi

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/qbbrowser"
	"yyb_go/internal/store"
)

// QQ Browser token push: the companion extension (extensions/qbbrowser-token)
// calls chrome.account / browser.login inside QQ Browser and posts the outer
// access token here. The server exchanges it for an ilink login buffer and
// keeps the account's credentials current — no QR scan involved, the browser
// sign-in IS the authorization.

type qbTokenPush struct {
	AccessToken string `json:"access_token"`
	UserID      string `json:"user_id"`
	OpenID      string `json:"openid"`
	GUID        string `json:"guid"`
	Nickname    string `json:"nickname"`
	QBRNCookie  string `json:"qbrn_cookie"`
}

func (a *App) handleQBBrowserToken(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body qbTokenPush
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	creds := qbbrowser.Credentials{
		QBAccessToken: strings.TrimSpace(body.AccessToken),
		UserID:        strings.TrimSpace(body.UserID),
		OpenID:        strings.TrimSpace(body.OpenID),
		GUID:          strings.TrimSpace(body.GUID),
		Nickname:      strings.TrimSpace(body.Nickname),
		QBRNCookie:    strings.TrimSpace(body.QBRNCookie),
	}
	if err := creds.Validate(); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.RequestTimeout)
	defer cancel()
	// Validate the token against the buffer gateway before touching the store:
	// the extension learns immediately when its token is stale.
	buffer, err := qbbrowser.FetchBuffer(ctx, a.qr.LoginBuffers().HTTPClient(), creds)
	if err != nil {
		var rejected *qbbrowser.TokenRejectedError
		if errors.As(err, &rejected) {
			writeError(w, http.StatusUnauthorized, fmt.Sprintf("token 被网关拒绝（code=%d），请在 QQ 浏览器重新登录微信", rejected.Code))
			return
		}
		writeError(w, http.StatusBadGateway, "登录票据换取失败: "+err.Error())
		return
	}
	openID := creds.Identity()
	status := "alive"
	credMap := protocol.LoginBufferCredentials{
		Provider: "qbbrowser", OpenID: openID,
		QBBrowser:              creds,
		LoginType:              "WX",
		RefreshTokenObservedAt: time.Now().Unix(),
	}.ToMap()
	acc, err := a.db.UpsertAccount(ctx, openID, buffer, nil, nil, nil, nil, credMap, &status)
	if err != nil {
		if errors.Is(err, store.ErrQQBrowserBridgeDowngrade) {
			writeError(w, http.StatusConflict, "此 QQ 账号已由独立扫码管理，请停用扩展推送，使用扫码页更新授权")
			return
		}
		writeError(w, http.StatusInternalServerError, "保存账号失败: "+err.Error())
		return
	}
	a.events.Emit("management.completed", eventlog.Fields{Route: "/api/qbbrowser/token", Method: http.MethodPost})
	writeJSON(w, http.StatusOK, acc.Public())
}
