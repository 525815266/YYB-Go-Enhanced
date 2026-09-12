package httpapi

import (
	"crypto/subtle"
	"database/sql"
	"errors"
	"net/http"
	"strconv"
	"strings"

	"yyb_go/internal/proxysource"
)

type integrationField struct {
	Name        string `json:"name"`
	Label       string `json:"label"`
	Type        string `json:"type"`
	Required    bool   `json:"required,omitempty"`
	Default     string `json:"default,omitempty"`
	Placeholder string `json:"placeholder,omitempty"`
}

func (a *App) handleIntegrationManifest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writeRawJSON(w, http.StatusOK, map[string]any{
		"schema_version":  1,
		"id":              "yyb",
		"name":            "YYB 协议",
		"description":     "微信账号、协议能力与青龙运行入口",
		"version":         "1.0.0",
		"icon":            "radio-tower",
		"refresh_seconds": 60,
		"pages": []map[string]any{
			{
				"id": "accounts", "label": "微信账号", "icon": "users", "type": "resource",
				"resource": map[string]any{
					"id": "accounts", "path": "/integration/accounts", "method": "GET",
					"columns": []map[string]string{
						{"key": "id", "label": "账号 ID"}, {"key": "display_name", "label": "名称"},
						{"key": "remark", "label": "备注"}, {"key": "status", "label": "协议状态"},
						{"key": "openid_masked", "label": "OpenID"}, {"key": "updated_at", "label": "更新时间", "format": "datetime"},
					},
				},
			},
			{
				"id": "capabilities", "label": "能力调用", "icon": "terminal", "type": "actions",
				"actions": []map[string]any{
					{
						"id": "get-code", "label": "获取 wx.login code", "description": "为指定微信账号和小程序获取一次性 code。", "path": "/integration/actions/get-code", "method": "POST",
						"fields": []integrationField{{Name: "ref", Label: "账号 ID 或 OpenID", Type: "text", Required: true, Placeholder: "例如 1"}, {Name: "app_id", Label: "小程序 AppID", Type: "text", Required: true, Placeholder: "wx..."}},
					},
					{
						"id": "refresh-account", "label": "刷新账号状态", "description": "立即检查指定账号的协议存活状态。", "path": "/integration/actions/refresh-account", "method": "POST",
						"fields": []integrationField{{Name: "ref", Label: "账号 ID 或 OpenID", Type: "text", Required: true, Placeholder: "例如 1"}},
					},
				},
			},
			{"id": "add-account", "label": "添加账号", "icon": "scan-line", "type": "external", "path": "/scan", "description": "使用本机微信或手机扫码添加账号。"},
			{"id": "runs", "label": "运行管理", "icon": "scroll-text", "type": "external", "path": "/runs", "description": "管理账号级青龙任务和运行日志。"},
		},
	})
}

func (a *App) handleIntegrationAccounts(w http.ResponseWriter, r *http.Request) {
	if !a.authorizeIntegration(w, r) {
		return
	}
	accounts, err := a.db.ListAccounts(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]map[string]any, 0, len(accounts))
	for _, account := range accounts {
		public := account.Public()
		out = append(out, map[string]any{
			"id": public.ID, "display_name": firstNonEmpty(deref(public.Remark), deref(public.Nickname), deref(public.Alias), "账号"),
			"remark": deref(public.Remark), "status": deref(public.Status), "openid_masked": maskIntegrationValue(public.OpenID),
			"updated_at": public.UpdatedAt,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": out, "count": len(out)})
}

func (a *App) handleIntegrationAccountProxy(w http.ResponseWriter, r *http.Request) {
	if !a.authorizeIntegration(w, r) {
		return
	}
	account, ok := a.resolveAccountRef(w, r, r.URL.Query().Get("ref"))
	if !ok {
		return
	}
	if r.URL.Query().Get("refresh") == "1" {
		a.invalidateProxyLease(account.ID)
	}
	setting, err := a.db.AccountProxySettingOrDefault(r.Context(), account.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	proxyValue := ""
	mode := setting.Mode
	proxyType := setting.ProxyType
	source := "account"
	if setting.Mode == "direct" && strings.TrimSpace(r.URL.Query().Get("fallback_profile_id")) != "" {
		profileID, parseErr := strconv.ParseInt(r.URL.Query().Get("fallback_profile_id"), 10, 64)
		if parseErr != nil || profileID <= 0 {
			writeError(w, http.StatusBadRequest, "fallback_profile_id must be a positive integer")
			return
		}
		profile, profileErr := a.db.GetProxyProviderProfile(r.Context(), profileID)
		if errors.Is(profileErr, sql.ErrNoRows) {
			writeError(w, http.StatusBadRequest, "fallback proxy profile not found")
			return
		}
		if profileErr != nil {
			writeError(w, http.StatusInternalServerError, profileErr.Error())
			return
		}
		apiURL, profileErr := proxyProfileURLForRegion(profile, r.URL.Query().Get("region_code"), r.URL.Query().Get("region_province"), r.URL.Query().Get("region_city"))
		if profileErr != nil {
			writeError(w, http.StatusBadRequest, profileErr.Error())
			return
		}
		proxyValue, profileErr = a.resolveProxySpec(r.Context(), proxysource.Spec{Mode: "api", ProxyType: profile.ProxyType, APIURL: apiURL})
		if profileErr != nil {
			writeError(w, http.StatusBadGateway, "resolve fallback proxy failed: "+profileErr.Error())
			return
		}
		mode, proxyType, source = "api", profile.ProxyType, "fallback_profile"
	} else {
		proxyValue, _, err = a.resolveAccountProxy(r.Context(), account.ID)
	}
	if err != nil {
		writeError(w, http.StatusBadGateway, "resolve account proxy failed: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"account_id": account.ID,
		"configured": proxyValue != "",
		"mode":       mode,
		"proxy_type": proxyType,
		"source":     source,
		"proxy":      proxyValue,
		"masked":     proxysource.Mask(proxyValue),
	})
}

func (a *App) handleIntegrationGetCode(w http.ResponseWriter, r *http.Request) {
	if !a.authorizeIntegration(w, r) {
		return
	}
	var body struct {
		Ref   string `json:"ref"`
		AppID string `json:"app_id"`
	}
	if decodeOptionalJSON(r, &body) != nil || strings.TrimSpace(body.AppID) == "" {
		writeError(w, http.StatusBadRequest, "ref and app_id are required")
		return
	}
	account, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	result, err := a.invokeWXApp(r.Context(), account, strings.TrimSpace(body.AppID), nil, a.invokeGetCode)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"account_id": account.ID, "result": result})
}

func (a *App) handleIntegrationRefreshAccount(w http.ResponseWriter, r *http.Request) {
	if !a.authorizeIntegration(w, r) {
		return
	}
	var body accountRefIn
	if decodeOptionalJSON(r, &body) != nil || strings.TrimSpace(body.Ref) == "" {
		writeError(w, http.StatusBadRequest, "ref is required")
		return
	}
	account, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	status, refreshErr := a.refreshLiveness(r.Context(), account)
	writeJSON(w, http.StatusOK, refreshOut(account, status, refreshErr))
}

func (a *App) authorizeIntegration(w http.ResponseWriter, r *http.Request) bool {
	expected := strings.TrimSpace(a.cfg.IntegrationToken)
	if expected == "" {
		writeError(w, http.StatusServiceUnavailable, "integration API is disabled")
		return false
	}
	provided := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	if subtle.ConstantTimeCompare([]byte(provided), []byte(expected)) != 1 {
		writeError(w, http.StatusUnauthorized, "invalid integration token")
		return false
	}
	return true
}

func maskIntegrationValue(value string) string {
	runes := []rune(value)
	if len(runes) <= 10 {
		return strings.Repeat("*", len(runes))
	}
	return string(runes[:6]) + "***" + string(runes[len(runes)-4:])
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
