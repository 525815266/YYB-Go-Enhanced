package httpapi

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strings"
	"time"

	"yyb_go/internal/oauth"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

type publicOAuthRequest struct {
	Ref            string `json:"ref"`
	AppID          string `json:"appid"`
	LegacyAppID    string `json:"app_id"`
	RedirectURI    string `json:"redirect_uri"`
	Scope          string `json:"scope"`
	State          string `json:"state"`
	ComponentAppID string `json:"component_appid"`
	// Mode selects how the request is served. The default keeps the original
	// behaviour and only builds a browser authorization link. "code", "authorize"
	// and "auto" ask for a real OAuth code obtained through the account session.
	Mode      string `json:"mode"`
	LoginType string `json:"login_type"`
}

// wantsOAuthCode reports whether the caller asked for a real OAuth code through
// the authenticated WeChat session instead of a browser authorization link.
// Only an explicit mode/type selects it, so the default stays backward compatible.
func (r publicOAuthRequest) wantsOAuthCode() bool {
	for _, raw := range []string{r.Mode, r.LoginType} {
		switch strings.ToLower(strings.TrimSpace(raw)) {
		case "code", "authorize", "auto":
			return true
		}
	}
	return false
}

func (a *App) handlePublicOAuth(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/wx/oauth" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	var body publicOAuthRequest
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if body.Ref == "" {
		writeError(w, http.StatusBadRequest, "ref is required")
		return
	}
	if body.AppID == "" {
		body.AppID = body.LegacyAppID
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	result, err := oauth.Build(oauth.Request{
		AppID:          body.AppID,
		RedirectURI:    body.RedirectURI,
		Scope:          body.Scope,
		State:          body.State,
		ComponentAppID: body.ComponentAppID,
	})
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	if body.wantsOAuthCode() {
		a.handlePublicOAuthCode(w, r, acc, body, result.Scope, result.State)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"account_id":             acc.ID,
		"openid":                 acc.OpenID,
		"appid":                  body.AppID,
		"scope":                  result.Scope,
		"state":                  result.State,
		"url":                    result.URL,
		"full_url":               result.FullURL,
		"code":                   nil,
		"authorization_required": true,
		"callback_code_source":   "redirect_uri",
		"mode":                   "authorization_url",
		"next_step":              "请在微信内打开 url 并完成授权；授权后微信会请求 redirect_uri?code=...&state=...，当前响应的 code=null 是正常状态。",
		"callback_params":        []string{"code", "state"},
	})
}

// handlePublicOAuthCode obtains a real OAuth code from WeChat using the account
// session (call_source=mmbiz-bin/oauth_authorize). It is only reached when the
// caller explicitly selects mode=code/authorize/auto.
func (a *App) handlePublicOAuthCode(w http.ResponseWriter, r *http.Request, acc *store.WechatAccount, body publicOAuthRequest, scope, state string) {
	redirectURI := strings.TrimSpace(body.RedirectURI)
	if redirectURI == "" {
		writeError(w, http.StatusBadRequest, "redirect_uri is required")
		return
	}
	if _, err := url.ParseRequestURI(redirectURI); err != nil {
		writeError(w, http.StatusBadRequest, "redirect_uri is invalid")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()

	result, err := a.invokeWXApp(ctx, acc, body.AppID, nil, func(ctx context.Context, account *store.WechatAccount, appID string, _ map[string]any, proxyValue string, fallbackDirect bool) (map[string]any, error) {
		res, callErr := a.pool.OAuthAuthorize(ctx, account.LoginBuffer, appID, redirectURI, body.Scope, body.State, body.ComponentAppID, account.ID, proxyValue, fallbackDirect)
		if callErr == nil || !protocol.IsApplicationRequestRejection(callErr) {
			return res, callErr
		}
		// oauth_authorize consumes the shortlink ticket that backs the session,
		// so the next call on the same session is always rejected with -14.
		// Rebuild the session and retry once; a failed recovery keeps the
		// original cache and the original error.
		return a.pool.RecoverOAuthSession(ctx, account, appID, redirectURI, body.Scope, body.State, body.ComponentAppID, proxyValue, fallbackDirect, callErr, func(ctx context.Context) (string, error) {
			return a.fetchAccountLoginBuffer(ctx, account)
		})
	})
	if err != nil {
		var expired accountExpiredError
		var networkErr interface{ Timeout() bool }
		if errors.As(err, &expired) {
			writeError(w, http.StatusConflict, "account login_buffer expired (refresh failed); re-scan required")
			return
		}
		if errors.As(err, &networkErr) {
			writeError(w, http.StatusGatewayTimeout, "call failed: "+err.Error())
			return
		}
		writeError(w, http.StatusBadGateway, "call failed: "+err.Error())
		return
	}

	code, _ := result["code"].(string)
	fullURL, _ := result["full_url"].(string)
	authorizeURL, _ := result["url"].(string)

	writeJSON(w, http.StatusOK, map[string]any{
		"account_id":             acc.ID,
		"openid":                 acc.OpenID,
		"appid":                  body.AppID,
		"scope":                  scope,
		"state":                  state,
		"url":                    authorizeURL,
		"full_url":               fullURL,
		"code":                   code,
		"authorization_required": code == "",
		"callback_code_source":   "mmbiz-bin/oauth_authorize",
		"mode":                   "authorize",
		"next_step":              "code 已通过账号会话真实授权获得，可直接换取 access_token/openid。",
	})
}
