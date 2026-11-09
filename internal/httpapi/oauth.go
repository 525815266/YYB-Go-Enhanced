package httpapi

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/oauth"
	"yyb_go/internal/store"
)

type publicOAuthRequest struct {
	Ref            string `json:"ref"`
	OpenID         string `json:"openid"`
	AppID          string `json:"appid"`
	LegacyAppID    string `json:"app_id"`
	RedirectURI    string `json:"redirect_uri"`
	Scope          string `json:"scope"`
	State          string `json:"state"`
	ComponentAppID string `json:"component_appid"`
	// ReturnURLOnly keeps this endpoint's legacy behavior: build the
	// official-account authorization URL without touching the session. The
	// default auto-exchanges a real web OAuth code through the account's own
	// session (command 1254, the same machinery as /wx/h5code), matching the
	// calling convention field H5 scripts already use: they POST
	// appid+redirect_uri and read the code, which stops them from pushing
	// official-account AppIDs through /wx/code and burning authorizations.
	ReturnURLOnly bool `json:"return_url_only"`
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
	ref := strings.TrimSpace(body.Ref)
	if ref == "" {
		// Field scripts and upstream docker callers use the openid field.
		ref = strings.TrimSpace(body.OpenID)
	}
	if ref == "" {
		writeError(w, http.StatusBadRequest, "ref is required")
		return
	}
	appID := strings.TrimSpace(body.AppID)
	if appID == "" {
		appID = strings.TrimSpace(body.LegacyAppID)
	}
	if appID == "" {
		writeError(w, http.StatusBadRequest, "appid is required")
		return
	}
	if len(appID) != 18 || !strings.HasPrefix(appID, "wx") {
		writeError(w, http.StatusBadRequest, "appid must be an 18-character official-account appid")
		return
	}
	acc, ok := a.resolveAccountRef(w, r, ref)
	if !ok {
		return
	}
	if !body.ReturnURLOnly {
		if strings.TrimSpace(body.RedirectURI) == "" {
			writeError(w, http.StatusBadRequest, "redirect_uri is required for automatic code exchange; send return_url_only=true to only build the authorization URL")
			return
		}
		invoke := a.oauthH5Invoke
		if invoke == nil {
			invoke = a.invokeH5Code
		}
		a.oauthAutoExchange(w, r, acc, invoke, appID, body)
		return
	}
	result, err := oauth.Build(oauth.Request{
		AppID:          appID,
		RedirectURI:    body.RedirectURI,
		Scope:          body.Scope,
		State:          body.State,
		ComponentAppID: body.ComponentAppID,
	})
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"account_id":             acc.ID,
		"openid":                 acc.OpenID,
		"appid":                  appID,
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

// oauthAutoExchange serves the drop-in convention field H5 scripts use: one
// POST carrying appid+redirect_uri returns a real web-authorize code fetched
// through the account's own session. The call is routed through /wx/h5code's
// invoke so every gate (appid deny, pacing, circuit breaker, login budget,
// account locks) applies unchanged, and the reply is mapped onto the field
// response shape. Business rejections and transport failures pass through
// with their original status and body.
func (a *App) oauthAutoExchange(w http.ResponseWriter, r *http.Request, acc *store.WechatAccount, invoke wxappCall, appID string, body publicOAuthRequest) {
	payload := map[string]any{"redirect_uri": strings.TrimSpace(body.RedirectURI)}
	if strings.TrimSpace(body.Scope) != "" {
		payload["scope"] = strings.TrimSpace(body.Scope)
	}
	if strings.TrimSpace(body.State) != "" {
		payload["state"] = strings.TrimSpace(body.State)
	}
	if strings.TrimSpace(body.ComponentAppID) != "" {
		payload["component_appid"] = strings.TrimSpace(body.ComponentAppID)
	}
	h5Body, err := json.Marshal(map[string]any{"ref": strconv.FormatInt(acc.ID, 10), "app_id": appID, "payload": payload})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	h5Req := r.Clone(r.Context())
	h5Req.URL.Path = "/wx/h5code"
	h5Req.URL.RawPath = ""
	h5Req.Body = io.NopCloser(bytes.NewReader(h5Body))
	h5Req.Header = r.Header.Clone()
	h5Req.Header.Set("Content-Type", "application/json")
	h5Req.ContentLength = int64(len(h5Body))
	rec := httptest.NewRecorder()
	a.callWXApp(rec, withWXOperation(h5Req, eventlog.OpH5Code), false, invoke)

	var envelope struct {
		Code int `json:"code"`
		Data struct {
			Result map[string]any `json:"result"`
		} `json:"data"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &envelope)
	if rec.Code != http.StatusOK || envelope.Code != 0 || envelope.Data.Result == nil {
		// Keep the richer h5 error semantics (business rejections, deny
		// lists, circuit states) untouched instead of flattening them.
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(rec.Code)
		_, _ = w.Write(rec.Body.Bytes())
		return
	}
	result := envelope.Data.Result
	redirectURI := strings.TrimSpace(body.RedirectURI)
	urls, buildErr := oauth.Build(oauth.Request{
		AppID:          appID,
		RedirectURI:    redirectURI,
		Scope:          stringOf(result["scope"]),
		State:          stringOf(result["state"]),
		ComponentAppID: strings.TrimSpace(body.ComponentAppID),
	})
	if buildErr != nil {
		// The authorize URL is informational here; a build failure must not
		// discard an exchanged code.
		urls.URL, urls.FullURL = "", ""
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"account_id": acc.ID,
		"openid":     acc.OpenID,
		"appid":      appID,
		"scope":      result["scope"],
		"state":      result["state"],
		"code":       result["code"],
		"url":        urls.URL,
		"full_url":   urls.FullURL,
		"mode":       "authorization_code",
		"account": map[string]any{
			"id":       acc.ID,
			"alias":    acc.Alias,
			"nickname": acc.Nickname,
			"remark":   acc.Remark,
		},
	})
}

func stringOf(value any) string {
	if s, ok := value.(string); ok {
		return s
	}
	return ""
}
