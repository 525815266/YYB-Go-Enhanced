package httpapi

import (
	"context"
	"database/sql"
	"errors"
	"log"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"yyb_go/internal/store"
)

const (
	qingLongTypeSetting          = "qinglong_type"
	qingLongURLSetting           = "qinglong_url"
	qingLongClientIDSetting      = "qinglong_client_id"
	qingLongSecretSetting        = "qinglong_client_secret"
	qingLongServerAddrSetting    = "qinglong_server_addr"
	qingLongServerEnvNameSetting = "qinglong_server_env_name"
	defaultPanelServerEnvName    = "YYB_SERVER"
)

var (
	validPanelEnvVarName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,63}$`)
	validPanelServerAddr = regexp.MustCompile(`^[A-Za-z0-9_.:/-]+$`)
)

type accountRemarkIn struct {
	Ref    string `json:"ref"`
	Remark string `json:"remark"`
}

type qingLongConfigIn struct {
	Type          string  `json:"type"`
	URL           string  `json:"url"`
	ClientID      string  `json:"client_id"`
	ClientSecret  string  `json:"client_secret"`
	ServerAddr    *string `json:"server_addr"`
	ServerFormat  *string `json:"server_format"`
	ServerEnvName *string `json:"server_env_name"`
	Clear         bool    `json:"clear"`
}

func requestedServerAddr(body qingLongConfigIn) *string {
	if body.ServerAddr != nil {
		return body.ServerAddr
	}
	return body.ServerFormat
}

// Panel credentials are kept separately for each driver. The legacy keys are
// still written and read as a migration path for existing installations.
func panelSettingKey(panelType, key string) string {
	return "panel_" + normalizePanelType(panelType) + "_" + key
}

type qingLongSyncIn struct {
	Ref string `json:"ref"`
}

type qingLongAccountCleanup struct {
	Status            string
	EnvEntriesRemoved int
	TasksDeleted      int
}

type qingLongEnvChange struct {
	env      qingLongEnv
	newValue string
}

func (a *App) handleAccountRemark(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body accountRemarkIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	remark := strings.TrimSpace(body.Remark)
	if len([]rune(remark)) > 80 || strings.ContainsAny(remark, "\r\n\t") {
		writeError(w, http.StatusBadRequest, "账号备注不能超过 80 个字符或包含换行符")
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	if err := a.db.SetAccountRemark(r.Context(), acc.ID, remark); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	acc, err := a.db.GetAccount(r.Context(), acc.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	result := map[string]any{"account": acc.Public(), "jobs_updated": false}
	if a.qinglong.configured() {
		setting, settingErr := a.db.AccountPushSettingOrDefault(r.Context(), acc.ID)
		if settingErr == nil {
			settingErr = a.refreshAccountJobCommands(r.Context(), acc, setting)
		}
		if settingErr != nil {
			result["warning"] = "备注已保存，但面板任务名称更新失败：" + settingErr.Error()
		} else {
			result["jobs_updated"] = true
		}
	}
	writeJSON(w, http.StatusOK, result)
}

func (a *App) handleQingLongConfig(w http.ResponseWriter, r *http.Request) {
	if a.auth != nil {
		user := a.browserUser(r)
		if user == nil || user.Role != "admin" {
			if r.Method != http.MethodGet {
				requireAdmin(w, r)
				return
			}
			activeType, _, _, _ := a.qinglong.configuration()
			envName, _ := a.qingLongServerEnvName(r.Context())
			writeJSON(w, http.StatusOK, map[string]any{
				"type": activeType, "active_type": activeType,
				"configured": a.qinglong.configured(), "restricted": true,
				"server_env_name": envName,
			})
			return
		}
	}
	switch r.Method {
	case http.MethodGet:
		activeType, _, _, _ := a.qinglong.configuration()
		pType := normalizePanelType(r.URL.Query().Get("type"))
		if strings.TrimSpace(r.URL.Query().Get("type")) == "" {
			pType = activeType
		}
		baseURL, clientID, secret := a.panelConfigValues(r.Context(), pType)
		profileConfigured := strings.TrimSpace(baseURL) != "" && strings.TrimSpace(secret) != ""
		profiles := make(map[string]map[string]any, 3)
		for _, candidate := range []string{PanelTypeQingLong, PanelTypeDaidai, PanelTypeArcadia} {
			urlValue, idValue, secretValue := a.panelConfigValues(r.Context(), candidate)
			profiles[candidate] = map[string]any{
				"url": urlValue, "client_id": idValue,
				"secret_configured": strings.TrimSpace(secretValue) != "",
				"configured":        strings.TrimSpace(urlValue) != "" && strings.TrimSpace(secretValue) != "",
			}
		}
		serverAddr, serverCustom := a.qingLongServerSetting(r.Context())
		envName, envCustom := a.qingLongServerEnvName(r.Context())
		writeJSON(w, http.StatusOK, map[string]any{
			"type":                  pType,
			"active_type":           activeType,
			"url":                   baseURL,
			"client_id":             clientID,
			"secret_configured":     strings.TrimSpace(secret) != "",
			"configured":            profileConfigured,
			"server_addr":           serverAddr,
			"server_default":        strings.TrimSpace(a.cfg.QingLongServer),
			"server_customized":     serverCustom,
			"server_env_name":       envName,
			"server_env_default":    a.defaultServerEnvName(),
			"server_env_customized": envCustom,
			"profiles":              profiles,
		})
	case http.MethodPut:
		var body qingLongConfigIn
		if err := decodeOptionalJSON(r, &body); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if body.Clear {
			pType, _, _, _ := a.qinglong.configuration()
			if strings.TrimSpace(body.Type) != "" {
				pType = normalizePanelType(body.Type)
			}
			activeType, _, _, _ := a.qinglong.configuration()
			var persistErr error
			if activeType == pType {
				persistErr = a.persistQingLongConfig(r.Context(), pType, "", "", "")
			} else {
				persistErr = a.persistPanelProfile(r.Context(), pType, "", "", "")
			}
			if persistErr != nil {
				writeError(w, http.StatusInternalServerError, persistErr.Error())
				return
			}
			if activeType == pType {
				a.qinglong.reconfigure(PanelTypeQingLong, "", "", "")
			}
			writeJSON(w, http.StatusOK, map[string]any{"type": pType, "configured": false, "connected": false})
			return
		}
		serverAddrInput := requestedServerAddr(body)
		if serverAddrInput != nil {
			customAddr := strings.TrimSpace(*serverAddrInput)
			if customAddr != "" && !validPanelServerAddr.MatchString(customAddr) {
				writeError(w, http.StatusBadRequest, "对外服务地址格式不合法，仅允许字母、数字及 _ . : / -")
				return
			}
		}
		if body.ServerEnvName != nil {
			customEnvName := strings.TrimSpace(*body.ServerEnvName)
			if customEnvName != "" && !validPanelEnvVarName.MatchString(customEnvName) {
				writeError(w, http.StatusBadRequest, "面板环境变量名格式不合法，须以字母或下划线开头，仅含字母、数字和下划线")
				return
			}
		}
		pType := strings.ToLower(strings.TrimSpace(body.Type))
		pType = normalizePanelType(pType)
		baseURL := strings.TrimRight(strings.TrimSpace(body.URL), "/")
		clientID := strings.TrimSpace(body.ClientID)
		if pType == PanelTypeArcadia {
			clientID = "api-token"
		}
		secret := strings.TrimSpace(body.ClientSecret)
		if secret == "" {
			_, _, secret = a.panelConfigValues(r.Context(), pType)
		}
		if err := validatePanelConfig(pType, baseURL, clientID, secret); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		candidate := newQingLongClient(pType, baseURL, clientID, secret, a.cfg.RequestTimeout)
		if err := candidate.status(r.Context()); err != nil {
			panelName := "面板"
			if pType == PanelTypeDaidai {
				panelName = "呆呆面板"
			} else if pType == PanelTypeArcadia {
				panelName = "Arcadia 面板"
			} else {
				panelName = "青龙面板"
			}
			writeError(w, http.StatusBadGateway, panelName+"连接测试失败："+err.Error())
			return
		}
		prevAddr, _ := a.qingLongServerSetting(r.Context())
		prevEnvName, _ := a.qingLongServerEnvName(r.Context())
		if serverAddrInput != nil {
			customAddr := strings.TrimSpace(*serverAddrInput)
			if err := a.db.SetSetting(r.Context(), qingLongServerAddrSetting, customAddr); err != nil {
				writeError(w, http.StatusInternalServerError, err.Error())
				return
			}
		}
		if body.ServerEnvName != nil {
			customEnvName := strings.TrimSpace(*body.ServerEnvName)
			if err := a.db.SetSetting(r.Context(), qingLongServerEnvNameSetting, customEnvName); err != nil {
				writeError(w, http.StatusInternalServerError, err.Error())
				return
			}
		}
		if err := a.persistQingLongConfig(r.Context(), pType, baseURL, clientID, secret); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		a.qinglong.reconfigure(pType, baseURL, clientID, secret)
		serverAddr, serverCustom := a.qingLongServerSetting(r.Context())
		envName, envCustom := a.qingLongServerEnvName(r.Context())
		reply := map[string]any{
			"type": pType, "url": baseURL, "client_id": clientID, "secret_configured": true,
			"configured": true, "connected": true,
			"server_addr": serverAddr, "server_default": strings.TrimSpace(a.cfg.QingLongServer), "server_customized": serverCustom,
			"server_env_name": envName, "server_env_default": a.defaultServerEnvName(), "server_env_customized": envCustom,
		}
		if serverAddr != prevAddr || envName != prevEnvName {
			if refreshErr := a.refreshAllManagedJobCommands(r.Context()); refreshErr != nil {
				reply["warning"] = "面板配置已保存，但部分账号托管任务环境变量同步失败：" + refreshErr.Error()
			} else {
				reply["jobs_updated"] = true
			}
		}
		writeJSON(w, http.StatusOK, reply)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (a *App) persistQingLongConfig(ctx context.Context, pType, baseURL, clientID, secret string) error {
	if err := a.persistPanelProfile(ctx, pType, baseURL, clientID, secret); err != nil {
		return err
	}
	values := map[string]string{
		qingLongTypeSetting:     pType,
		qingLongURLSetting:      baseURL,
		qingLongClientIDSetting: clientID,
		qingLongSecretSetting:   secret,
	}
	for key, value := range values {
		if err := a.db.SetSetting(ctx, key, value); err != nil {
			return err
		}
	}
	return nil
}

func (a *App) persistPanelProfile(ctx context.Context, pType, baseURL, clientID, secret string) error {
	for key, value := range map[string]string{
		panelSettingKey(pType, qingLongURLSetting):      baseURL,
		panelSettingKey(pType, qingLongClientIDSetting): clientID,
		panelSettingKey(pType, qingLongSecretSetting):   secret,
	} {
		if err := a.db.SetSetting(ctx, key, value); err != nil {
			return err
		}
	}
	return nil
}

func (a *App) panelConfigValues(ctx context.Context, panelType string) (string, string, string) {
	pType := normalizePanelType(panelType)
	load := func(key string) string {
		value, err := a.db.GetSetting(ctx, panelSettingKey(pType, key))
		if err == nil {
			return value
		}
		// Before per-type settings existed, the active configuration lived in
		// the legacy keys. Only use those as a fallback for the active driver.
		activeType, _, _, _ := a.qinglong.configuration()
		if activeType == pType {
			value, _ = a.db.GetSetting(ctx, key)
		}
		return value
	}
	clientID := load(qingLongClientIDSetting)
	if pType == PanelTypeArcadia {
		clientID = "api-token"
	}
	return load(qingLongURLSetting), clientID, load(qingLongSecretSetting)
}

func validatePanelConfig(panelType, baseURL, clientID, secret string) error {
	if baseURL == "" || clientID == "" || secret == "" {
		if panelType == PanelTypeArcadia {
			return errors.New("Arcadia 地址和 OpenAPI Token 均不能为空")
		}
		return errors.New("面板地址和鉴权凭据均不能为空")
	}
	parsed, err := url.Parse(baseURL)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return errors.New("面板地址必须是有效的 http 或 https URL")
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return errors.New("面板地址不能包含查询参数或片段")
	}
	return nil
}

func (a *App) handleQingLongSync(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if !a.qinglong.configured() {
		writeError(w, http.StatusConflict, "请先配置面板 OpenAPI")
		return
	}
	var body qingLongSyncIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	value, added, err := a.syncAccountToQingLong(r.Context(), acc)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	responseValue := any(value)
	if a.auth != nil {
		user := a.browserUser(r)
		if user == nil || user.Role != "admin" {
			// The panel variable is shared by all accounts. Ordinary users may
			// sync their own account, but must not receive other users' entries.
			responseValue = nil
		}
	}
	envName, _ := a.qingLongServerEnvName(r.Context())
	writeJSON(w, http.StatusOK, map[string]any{
		"account": acc.Public(), "name": envName, "value": responseValue, "added": added,
		// The account the variable lists: the primary when acc is a fleet member.
		"synced_id": a.panelRepresentative(r.Context(), acc).ID,
	})
}

// autoSyncAfterScan reconciles a newly added or rescanned account in the
// configured automation panel. Panel synchronization is a side effect of QR
// authorization: a panel outage must never turn a successful scan into an
// error or require the user to repeat the authorization.
func (a *App) autoSyncAfterScan(acc *store.WechatAccount) {
	if acc == nil || !a.qinglong.configured() {
		return
	}
	go func(account *store.WechatAccount) {
		timeout := a.cfg.RequestTimeout * 2
		if timeout < 10*time.Second {
			timeout = 10 * time.Second
		}
		ctx, cancel := context.WithTimeout(context.Background(), timeout)
		defer cancel()
		if _, _, err := a.syncAccountToQingLong(ctx, account); err != nil {
			log.Printf("[auto-sync] account %d panel sync failed: %v", account.ID, err)
		}
	}(acc)
}

// deferredPanelSyncDelay is how long the panel sync of a multi-channel scan waits.
// The console binds the scanned accounts into a fleet right after each
// confirmation; by the time the delay is over a member is listed through its
// primary, and an account that stayed independent lists itself.
var deferredPanelSyncDelay = 15 * time.Second

// autoSyncAfterScanLater is autoSyncAfterScan for a scan that is one of several
// channels of the same WeChat.
func (a *App) autoSyncAfterScanLater(acc *store.WechatAccount, delay time.Duration) {
	if acc == nil || !a.qinglong.configured() {
		return
	}
	time.AfterFunc(delay, func() { a.autoSyncAfterScan(acc) })
}

// handleQingLongSyncAll reconciles every locally stored account into the
// shared YYB_SERVER variable. It deliberately does not renumber database
// primary keys: those IDs are referenced by sessions, proxies and managed
// cron jobs. The UI uses a separate compact display number instead.
func (a *App) handleQingLongSyncAll(w http.ResponseWriter, r *http.Request) {
	if a.auth != nil && !requireAdmin(w, r) {
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if !a.qinglong.configured() {
		writeError(w, http.StatusConflict, "请先配置面板 OpenAPI")
		return
	}
	a.panelSyncMu.Lock()
	defer a.panelSyncMu.Unlock()
	accounts, err := a.db.ListAccounts(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	envName, _ := a.qingLongServerEnvName(r.Context())
	envs, err := a.qinglong.listEnvs(r.Context(), envName)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	env, err := selectYYBServerEnv(envs, envName)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	currentValue, remarks := "", "YYB Go 账号列表"
	if env != nil {
		currentValue, remarks = env.Value, env.Remarks
	}
	remarks = managedYYBServerRemarks(remarks, accounts)
	serverAddr, _ := a.qingLongServerSetting(r.Context())
	value := currentValue
	added := 0
	pending := map[int64]string{}
	for _, acc := range accounts {
		var migrationErr error
		value, pending[acc.ID], migrationErr = a.migratePendingAccountReferences(r.Context(), value, acc)
		if migrationErr != nil {
			writeError(w, 500, "读取待同步引用失败")
			return
		}
	}
	value = reconcileYYBServerEntries(value, accounts, a.cfg.QingLongRefMode, true)
	for _, acc := range accounts {
		if fleetPrimaryOf(acc) > 0 && fleetPrimaryOf(acc) != acc.ID {
			// A member is listed through its primary, which is in this loop too.
			continue
		}
		var changed bool
		value, changed = mergeYYBServerValue(value, serverAddr, acc, a.cfg.QingLongRefMode)
		if changed {
			added++
		}
	}
	if err := a.saveYYBServerEnv(r.Context(), env, envName, value, remarks); err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	for id, snapshot := range pending {
		_ = a.db.CompleteAccountReferences(r.Context(), id, snapshot)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"name": envName, "value": value, "accounts": len(accounts), "added": added,
	})
}

// panelRepresentative is the account that stands for acc in the panel variable.
// A fleet is listed by its primary: requests for the primary already rotate
// across the members, so an entry per member would run every script once per
// channel. Everything else represents itself.
func (a *App) panelRepresentative(ctx context.Context, acc *store.WechatAccount) *store.WechatAccount {
	if primaryID := fleetPrimaryOf(acc); primaryID > 0 && primaryID != acc.ID {
		if primary, err := a.db.GetAccount(ctx, primaryID); err == nil {
			return primary
		}
	}
	return acc
}

func (a *App) syncAccountToQingLong(ctx context.Context, acc *store.WechatAccount) (string, bool, error) {
	a.panelSyncMu.Lock()
	defer a.panelSyncMu.Unlock()
	acc, err := a.db.GetAccount(ctx, acc.ID)
	if err != nil {
		return "", false, err
	}
	envName, _ := a.qingLongServerEnvName(ctx)
	envs, err := a.qinglong.listEnvs(ctx, envName)
	if err != nil {
		return "", false, err
	}
	env, err := selectYYBServerEnv(envs, envName)
	if err != nil {
		return "", false, err
	}
	currentValue, remarks := "", "YYB Go 账号列表"
	if env != nil {
		currentValue, remarks = env.Value, env.Remarks
	}
	accounts, err := a.db.ListAccounts(ctx)
	if err != nil {
		return "", false, err
	}
	remarks = managedYYBServerRemarks(remarks, accounts)
	currentValue, pending, err := a.migratePendingAccountReferences(ctx, currentValue, acc)
	if err != nil {
		return "", false, err
	}
	currentValue = reconcileYYBServerEntries(currentValue, accounts, a.cfg.QingLongRefMode, false)
	serverAddr, _ := a.qingLongServerSetting(ctx)
	value, added := mergeYYBServerValue(currentValue, serverAddr, a.panelRepresentative(ctx, acc), a.cfg.QingLongRefMode)
	if err := a.saveYYBServerEnv(ctx, env, envName, value, remarks); err != nil {
		return "", false, err
	}
	_ = a.db.CompleteAccountReferences(ctx, acc.ID, pending)
	return value, added, nil
}

func (a *App) qingLongServerSetting(ctx context.Context) (string, bool) {
	if saved, err := a.db.GetSetting(ctx, qingLongServerAddrSetting); err == nil && strings.TrimSpace(saved) != "" {
		return strings.TrimSpace(saved), true
	}
	return strings.TrimSpace(a.cfg.QingLongServer), false
}

func (a *App) defaultServerEnvName() string {
	fallback := strings.TrimSpace(a.cfg.QingLongServerEnv)
	if fallback == "" || !validPanelEnvVarName.MatchString(fallback) {
		return defaultPanelServerEnvName
	}
	return fallback
}

func (a *App) qingLongServerEnvName(ctx context.Context) (string, bool) {
	if saved, err := a.db.GetSetting(ctx, qingLongServerEnvNameSetting); err == nil {
		trimmed := strings.TrimSpace(saved)
		if trimmed != "" && validPanelEnvVarName.MatchString(trimmed) {
			return trimmed, true
		}
	}
	return a.defaultServerEnvName(), false
}

// reconcileYYBServerEntries strips fleet sub-member references and duplicate
// entries that resolve to the same local account, while keeping non-managed or
// external lines intact.
func reconcileYYBServerEntries(existing string, accounts []*store.WechatAccount, refMode string, dedupeCrossForm bool) string {
	normalized := strings.ReplaceAll(existing, "\r\n", "\n")
	normalized = strings.TrimRight(normalized, "\n")
	if normalized == "" || len(accounts) == 0 {
		return normalized
	}
	byID := make(map[int64]*store.WechatAccount, len(accounts))
	byRef := make(map[string]*store.WechatAccount, len(accounts)*2)
	for _, acc := range accounts {
		if acc == nil {
			continue
		}
		byID[acc.ID] = acc
		byRef[strconv.FormatInt(acc.ID, 10)] = acc
		if openID := strings.TrimSpace(acc.OpenID); openID != "" {
			byRef[openID] = acc
		}
	}
	canonicalRef := func(acc *store.WechatAccount) string {
		if strings.EqualFold(strings.TrimSpace(refMode), "openid") && strings.TrimSpace(acc.OpenID) != "" {
			return strings.TrimSpace(acc.OpenID)
		}
		return strconv.FormatInt(acc.ID, 10)
	}
	seenAccount := make(map[int64]bool, len(accounts))
	seenExactLine := make(map[string]bool)
	lines := strings.Split(normalized, "\n")
	// Preserve the first position where each account appears so scripts that
	// address accounts by 1-based line index do not silently shift to a
	// different account when duplicate openid/id forms are collapsed.
	firstLine := make(map[int64]int, len(accounts))
	for idx, line := range lines {
		trimmed := strings.TrimSpace(line)
		sep := strings.LastIndex(trimmed, "@")
		if sep < 0 {
			continue
		}
		ref := strings.TrimSpace(trimmed[sep+1:])
		acc := byRef[ref]
		if acc == nil {
			continue
		}
		if p := fleetPrimaryOf(acc); p > 0 && p != acc.ID {
			continue
		}
		if _, exists := firstLine[acc.ID]; !exists {
			firstLine[acc.ID] = idx
		}
	}

	kept := make([]string, 0, len(lines))
	for idx, line := range lines {
		trimmed := strings.TrimSpace(line)
		sep := strings.LastIndex(trimmed, "@")
		if sep < 0 {
			kept = append(kept, line)
			continue
		}
		ref := strings.TrimSpace(trimmed[sep+1:])
		acc := byRef[ref]
		if acc == nil {
			kept = append(kept, line)
			continue
		}
		if primaryID := fleetPrimaryOf(acc); primaryID > 0 && primaryID != acc.ID {
			primary := byID[primaryID]
			if primary == nil {
				kept = append(kept, line)
				continue
			}
			if _, hasDirect := firstLine[primaryID]; hasDirect || seenAccount[primaryID] {
				continue
			}
			seenAccount[primaryID] = true
			rewritten := trimmed[:sep+1] + canonicalRef(primary)
			seenExactLine[rewritten] = true
			kept = append(kept, rewritten)
			continue
		}
		if dedupeCrossForm {
			if winIdx, ok := firstLine[acc.ID]; ok && winIdx != idx {
				continue
			}
			if seenAccount[acc.ID] {
				continue
			}
			if canon := canonicalRef(acc); ref != canon {
				line = trimmed[:sep+1] + canon
				trimmed = line
			}
		} else if seenExactLine[trimmed] {
			continue
		}
		seenAccount[acc.ID] = true
		seenExactLine[trimmed] = true
		kept = append(kept, line)
	}
	return strings.Join(kept, "\n")
}

func (a *App) migratePendingAccountReferences(ctx context.Context, value string, acc *store.WechatAccount) (string, string, error) {
	refs, snapshot, err := a.db.PendingAccountReferences(ctx, acc.ID)
	if err != nil {
		return "", "", err
	}
	for _, ref := range refs {
		targetRef := acc.OpenID
		if _, err := strconv.ParseInt(ref, 10, 64); err == nil {
			targetRef = strconv.FormatInt(acc.ID, 10)
		}
		if ref != targetRef && containsYYBServerReference(value, ref) {
			owner, lookupErr := a.db.ResolveAccount(ctx, ref)
			if lookupErr != nil && !errors.Is(lookupErr, sql.ErrNoRows) {
				return "", "", lookupErr
			}
			if lookupErr == nil && owner.ID != acc.ID {
				return "", "", errors.New("待迁移旧引用已被其他账号复用，请先在面板手动核对")
			}
		}
		value = migrateAccountOpenIDReference(value, ref, targetRef)
	}
	return value, snapshot, nil
}

func containsYYBServerReference(value, ref string) bool {
	for _, line := range strings.Split(value, "\n") {
		index := strings.LastIndex(line, "@")
		if index >= 0 && strings.TrimSpace(line[index+1:]) == ref {
			return true
		}
	}
	return false
}

// A channel switch changes the external identity, not the local account ID.
// Migrate managed OpenID references before merging so the old row cannot linger.
// Use a detached bounded context: closing the scan page must not cancel this step.
func (a *App) syncUpdatedAccountReferences(after *store.WechatAccount) string {
	if !a.qinglong.configured() {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), max(10*time.Second, a.cfg.RequestTimeout*2))
	defer cancel()
	if _, _, err := a.syncAccountToQingLong(ctx, after); err != nil {
		// Panel errors can contain the variable value; do not log that content.
		log.Printf("[auto-sync] account %d reference migration failed", after.ID)
		return "账号已更新，面板引用同步失败，请在控制台重新同步；手动维护的旧 OpenID 引用也需更新"
	}
	return ""
}

func migrateAccountOpenIDReference(value, oldID, newID string) string {
	if oldID == "" || oldID == newID {
		return value
	}
	lines := strings.Split(strings.ReplaceAll(value, "\r\n", "\n"), "\n")
	seen := map[string]bool{}
	out := make([]string, 0, len(lines))
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		separator := strings.LastIndex(trimmed, "@")
		if separator >= 0 {
			ref := strings.TrimSpace(trimmed[separator+1:])
			if ref == oldID {
				line = trimmed[:separator+1] + newID
				ref = newID
			}
			if ref == newID {
				key := strings.TrimSpace(line)
				if seen[key] {
					continue
				}
				seen[key] = true
			}
		}
		out = append(out, line)
	}
	return strings.Join(out, "\n")
}

// Prefer the active variable over disabled historical copies. Ambiguous
// active copies require explicit reconciliation rather than losing entries
// or silently re-enabling accounts from a stale variable.
func selectYYBServerEnv(envs []qingLongEnv, envName string) (*qingLongEnv, error) {
	if strings.TrimSpace(envName) == "" {
		envName = defaultPanelServerEnvName
	}
	var active, disabled *qingLongEnv
	disabledCount := 0
	for i := range envs {
		env := &envs[i]
		if env.Name != envName {
			continue
		}
		if env.enabled() {
			if active != nil {
				return nil, errors.New("面板存在多条已启用的 " + envName + "，请先在面板合并账号并仅保留一条启用记录，再同步；已有变量未被修改")
			}
			active = env
		} else {
			disabled = env
			disabledCount++
		}
	}
	if active != nil {
		return active, nil
	}
	if disabledCount > 1 {
		return nil, errors.New("面板存在多条已禁用的 " + envName + "，请先选择并启用需要继续使用的一条，再同步；已有变量未被修改")
	}
	return disabled, nil
}

func (a *App) saveYYBServerEnv(ctx context.Context, env *qingLongEnv, envName, value, remarks string) error {
	if strings.TrimSpace(envName) == "" {
		envName = defaultPanelServerEnvName
	}
	if env != nil {
		return a.qinglong.updateEnv(ctx, env.ID, envName, value, remarks)
	}
	return a.qinglong.upsertEnv(ctx, envName, value, remarks)
}

func managedYYBServerRemarks(existing string, accounts []*store.WechatAccount) string {
	existing = strings.TrimSpace(existing)
	if existing != "" && existing != "YYB Go 账号列表" && !strings.HasPrefix(existing, "YYB Go 账号：") {
		return existing
	}
	names := make([]string, 0, len(accounts))
	for _, acc := range accounts {
		name := firstAccountLabel(acc.Nickname, acc.Remark, acc.Alias)
		if name == "" {
			name = "ID " + strconv.FormatInt(acc.ID, 10)
		}
		names = append(names, name)
	}
	if len(names) == 0 {
		return "YYB Go 账号列表"
	}
	return "YYB Go 账号：" + strings.Join(names, "、")
}

func firstAccountLabel(values ...*string) string {
	for _, value := range values {
		if value != nil && strings.TrimSpace(*value) != "" {
			return strings.TrimSpace(*value)
		}
	}
	return ""
}

func mergeYYBServerValue(existing, server string, acc *store.WechatAccount, modes ...string) (string, bool) {
	existing = strings.ReplaceAll(existing, "\r\n", "\n")
	existing = strings.TrimRight(existing, "\n")
	id := strconv.FormatInt(acc.ID, 10)
	for _, line := range strings.Split(existing, "\n") {
		separator := strings.LastIndex(strings.TrimSpace(line), "@")
		if separator < 0 {
			continue
		}
		ref := strings.TrimSpace(strings.TrimSpace(line)[separator+1:])
		if ref == id || (acc.OpenID != "" && ref == acc.OpenID) {
			return existing, false
		}
	}
	refMode := ""
	if len(modes) > 0 {
		refMode = modes[0]
	}
	ref := id
	if strings.EqualFold(strings.TrimSpace(refMode), "openid") && strings.TrimSpace(acc.OpenID) != "" {
		ref = strings.TrimSpace(acc.OpenID)
	}
	entry := strings.TrimSpace(server) + "@" + ref
	if existing == "" {
		return entry, true
	}
	return existing + "\n" + entry, true
}

func removeAccountFromYYBServer(existing string, acc *store.WechatAccount) (string, int) {
	normalized := strings.ReplaceAll(existing, "\r\n", "\n")
	normalized = strings.TrimRight(normalized, "\n")
	if normalized == "" {
		return "", 0
	}

	id := strconv.FormatInt(acc.ID, 10)
	kept := make([]string, 0, strings.Count(normalized, "\n")+1)
	removed := 0
	for _, line := range strings.Split(normalized, "\n") {
		trimmed := strings.TrimSpace(line)
		separator := strings.LastIndex(trimmed, "@")
		if separator >= 0 {
			ref := strings.TrimSpace(trimmed[separator+1:])
			if ref == id || (acc.OpenID != "" && ref == acc.OpenID) {
				removed++
				continue
			}
		}
		kept = append(kept, line)
	}
	return strings.Join(kept, "\n"), removed
}

func (a *App) cleanupAccountFromQingLong(ctx context.Context, acc *store.WechatAccount) (qingLongAccountCleanup, error) {
	result := qingLongAccountCleanup{Status: "skipped"}
	if !a.qinglong.configured() {
		return result, nil
	}

	jobs, err := a.db.ListAccountScriptJobs(ctx, acc.ID)
	if err != nil {
		return result, err
	}
	envName, _ := a.qingLongServerEnvName(ctx)
	envs, err := a.qinglong.listEnvs(ctx, envName)
	if err != nil {
		return result, err
	}

	changes := make([]qingLongEnvChange, 0)
	deletions := make([]qingLongEnv, 0)
	for _, env := range envs {
		if env.Name != envName {
			continue
		}
		value, removed := removeAccountFromYYBServer(env.Value, acc)
		if removed == 0 {
			continue
		}
		if strings.TrimSpace(value) == "" {
			deletions = append(deletions, env)
		} else {
			changes = append(changes, qingLongEnvChange{env: env, newValue: value})
		}
		result.EnvEntriesRemoved += removed
	}

	updated := make([]qingLongEnv, 0, len(changes))
	rollbackEnvs := func() {
		for i := len(updated) - 1; i >= 0; i-- {
			_ = a.qinglong.updateEnvEntry(ctx, updated[i], updated[i].Value)
		}
	}
	for _, change := range changes {
		if err := a.qinglong.updateEnvEntry(ctx, change.env, change.newValue); err != nil {
			rollbackEnvs()
			return result, err
		}
		updated = append(updated, change.env)
	}
	deleted := make([]qingLongEnv, 0, len(deletions))
	rollbackAllEnvs := func() {
		rollbackEnvs()
		for _, env := range deleted {
			_ = a.qinglong.upsertEnv(ctx, env.Name, env.Value, env.Remarks)
		}
	}
	for _, env := range deletions {
		if err := a.qinglong.deleteEnvEntries(ctx, []int64{env.ID}); err != nil {
			rollbackAllEnvs()
			return result, err
		}
		deleted = append(deleted, env)
	}

	cronIDs := make([]int64, 0, len(jobs))
	seenCronIDs := make(map[int64]struct{}, len(jobs))
	for _, job := range jobs {
		if job.QLCronID <= 0 {
			continue
		}
		if _, exists := seenCronIDs[job.QLCronID]; exists {
			continue
		}
		seenCronIDs[job.QLCronID] = struct{}{}
		cronIDs = append(cronIDs, job.QLCronID)
	}
	if err := a.qinglong.deleteCrons(ctx, cronIDs); err != nil {
		rollbackAllEnvs()
		return result, err
	}

	result.Status = "completed"
	result.TasksDeleted = len(cronIDs)
	return result, nil
}
