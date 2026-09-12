package httpapi

import (
	"context"
	"errors"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"yyb_go/internal/store"
)

const (
	qingLongTypeSetting     = "qinglong_type"
	qingLongURLSetting      = "qinglong_url"
	qingLongClientIDSetting = "qinglong_client_id"
	qingLongSecretSetting   = "qinglong_client_secret"
)

type accountRemarkIn struct {
	Ref    string `json:"ref"`
	Remark string `json:"remark"`
}

type qingLongConfigIn struct {
	Type         string `json:"type"`
	URL          string `json:"url"`
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
	Clear        bool   `json:"clear"`
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
			writeJSON(w, http.StatusOK, map[string]any{
				"type": activeType, "active_type": activeType,
				"configured": a.qinglong.configured(), "restricted": true,
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
		writeJSON(w, http.StatusOK, map[string]any{
			"type":              pType,
			"active_type":       activeType,
			"url":               baseURL,
			"client_id":         clientID,
			"secret_configured": strings.TrimSpace(secret) != "",
			"configured":        profileConfigured,
			"profiles":          profiles,
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
		if err := a.persistQingLongConfig(r.Context(), pType, baseURL, clientID, secret); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		a.qinglong.reconfigure(pType, baseURL, clientID, secret)
		writeJSON(w, http.StatusOK, map[string]any{
			"type": pType, "url": baseURL, "client_id": clientID, "secret_configured": true,
			"configured": true, "connected": true,
		})
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
	writeJSON(w, http.StatusOK, map[string]any{
		"account": acc.Public(), "name": "YYB_SERVER", "value": responseValue, "added": added,
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
		a.panelSyncMu.Lock()
		defer a.panelSyncMu.Unlock()
		if _, _, err := a.syncAccountToQingLong(ctx, account); err != nil {
			log.Printf("[auto-sync] account %d panel sync failed: %v", account.ID, err)
		}
	}(acc)
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
	accounts, err := a.db.ListAccounts(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	envs, err := a.qinglong.listEnvs(r.Context(), "YYB_SERVER")
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	currentValue, remarks := "", "YYB Go 账号列表"
	for _, env := range envs {
		if env.Name == "YYB_SERVER" {
			currentValue = env.Value
			if strings.TrimSpace(env.Remarks) != "" {
				remarks = env.Remarks
			}
			break
		}
	}
	remarks = managedYYBServerRemarks(remarks, accounts)
	value := currentValue
	added := 0
	for _, acc := range accounts {
		var changed bool
		value, changed = mergeYYBServerValue(value, a.cfg.QingLongServer, acc, a.cfg.QingLongRefMode)
		if changed {
			added++
		}
	}
	if err := a.qinglong.upsertEnv(r.Context(), "YYB_SERVER", value, remarks); err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"name": "YYB_SERVER", "value": value, "accounts": len(accounts), "added": added,
	})
}

func (a *App) syncAccountToQingLong(ctx context.Context, acc *store.WechatAccount) (string, bool, error) {
	envs, err := a.qinglong.listEnvs(ctx, "YYB_SERVER")
	if err != nil {
		return "", false, err
	}
	currentValue, remarks := "", "YYB Go 账号列表"
	for _, env := range envs {
		if env.Name == "YYB_SERVER" {
			currentValue = env.Value
			if strings.TrimSpace(env.Remarks) != "" {
				remarks = env.Remarks
			}
			break
		}
	}
	accounts, err := a.db.ListAccounts(ctx)
	if err != nil {
		return "", false, err
	}
	remarks = managedYYBServerRemarks(remarks, accounts)
	value, added := mergeYYBServerValue(currentValue, a.cfg.QingLongServer, acc, a.cfg.QingLongRefMode)
	if err := a.qinglong.upsertEnv(ctx, "YYB_SERVER", value, remarks); err != nil {
		return "", false, err
	}
	return value, added, nil
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
	if len(modes) > 0 { refMode = modes[0] }
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
	envs, err := a.qinglong.listEnvs(ctx, "YYB_SERVER")
	if err != nil {
		return result, err
	}

	changes := make([]qingLongEnvChange, 0)
	deletions := make([]qingLongEnv, 0)
	for _, env := range envs {
		if env.Name != "YYB_SERVER" {
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
