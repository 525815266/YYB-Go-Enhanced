package httpapi

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"yyb_go/internal/store"
)

var validScriptKey = regexp.MustCompile(`^[\p{L}\p{N}_+./-]+\.(?:js|py)$`)

type scriptSource struct {
	Key      string
	Name     string
	Schedule string
	TaskRoot string
	Cron     qingLongCron
}

type accountJobPublic struct {
	ScriptKey        string `json:"script_key"`
	Name             string `json:"name"`
	Schedule         string `json:"schedule"`
	Provisioned      bool   `json:"provisioned"`
	Enabled          bool   `json:"enabled"`
	Running          bool   `json:"running"`
	QLCronID         int64  `json:"ql_cron_id,omitempty"`
	LastExecutionAt  int64  `json:"last_execution_at"`
	LastRunningTime  int64  `json:"last_running_time"`
	GlobalTaskActive bool   `json:"global_task_active"`
}

type jobActionIn struct {
	Ref       string `json:"ref"`
	ScriptKey string `json:"script_key"`
	Enabled   bool   `json:"enabled"`
}

type runLogIn struct {
	Ref    string `json:"ref"`
	LogKey string `json:"log_key"`
}

type pushSettingIn struct {
	Ref     string  `json:"ref"`
	Channel string  `json:"channel"`
	Token   string  `json:"token"`
	Topic   *string `json:"topic"`
}

type pushSettingPublic struct {
	Channel         string `json:"channel"`
	TokenConfigured bool   `json:"token_configured"`
	TopicConfigured bool   `json:"topic_configured"`
}

type accountRunPublic struct {
	AccountID  int64  `json:"account_id"`
	ScriptKey  string `json:"script_key"`
	Name       string `json:"name"`
	QLCronID   int64  `json:"ql_cron_id"`
	LogKey     string `json:"log_key"`
	StartedAt  int64  `json:"started_at"`
	Size       int64  `json:"size"`
	Running    bool   `json:"running"`
	TaskStatus string `json:"status"`
}

func (a *App) handleRuns(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	serveFileOrText(w, r, filepath.Join(a.resources.Templates, "runs.html"), fallbackRunsHTML)
}

func (a *App) handleQingLongStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if !a.qinglong.configured() {
		writeJSON(w, http.StatusOK, map[string]any{"configured": false, "connected": false})
		return
	}
	if err := a.qinglong.status(r.Context()); err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"configured": true, "connected": false, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"configured": true, "connected": true})
}

func (a *App) handleQingLongJobs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	acc, ok := a.resolveAccountFromQuery(w, r)
	if !ok {
		return
	}
	sources, cronsByID, err := a.scriptCatalog(r.Context())
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	storedJobs, err := a.db.ListAccountScriptJobs(r.Context(), acc.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	jobsByKey := make(map[string]store.AccountScriptJob, len(storedJobs))
	for _, job := range storedJobs {
		jobsByKey[job.ScriptKey] = job
	}
	out := make([]accountJobPublic, 0, len(sources))
	for _, source := range sources {
		item := accountJobPublic{
			ScriptKey:        source.Key,
			Name:             source.Name,
			Schedule:         source.Schedule,
			GlobalTaskActive: source.Cron.enabled(),
		}
		if job, exists := jobsByKey[source.Key]; exists {
			if cron, found := cronsByID[job.QLCronID]; found {
				item.Provisioned = true
				item.Enabled = cron.enabled()
				item.Running = cron.running()
				item.QLCronID = cron.ID
				item.LastExecutionAt = cron.getLastExecutionAt()
				item.LastRunningTime = cron.getLastRunningTime()
			}
		}
		out = append(out, item)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"account": acc.Public(),
		"jobs":    out,
		"count":   len(out),
	})
}

func (a *App) handleQingLongJobEnable(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body jobActionIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	if !body.Enabled {
		job, err := a.db.GetAccountScriptJob(r.Context(), acc.ID, body.ScriptKey)
		if errors.Is(err, sql.ErrNoRows) {
			writeJSON(w, http.StatusOK, map[string]any{"enabled": false, "provisioned": false})
			return
		}
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		if err := a.qinglong.setCronsEnabled(r.Context(), []int64{job.QLCronID}, false); err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"enabled": false, "provisioned": true, "ql_cron_id": job.QLCronID})
		return
	}
	job, _, err := a.ensureAccountJob(r.Context(), acc, body.ScriptKey)
	if err != nil {
		writeRunError(w, err)
		return
	}
	if err := a.qinglong.setCronsEnabled(r.Context(), []int64{job.QLCronID}, true); err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"enabled": true, "provisioned": true, "ql_cron_id": job.QLCronID})
}

func (a *App) handleQingLongJobRun(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body jobActionIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	job, source, err := a.ensureAccountJob(r.Context(), acc, body.ScriptKey)
	if err != nil {
		writeRunError(w, err)
		return
	}
	if err := a.qinglong.runCrons(r.Context(), []int64{job.QLCronID}); err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]any{"started": true, "account_id": acc.ID, "script_key": source.Key, "ql_cron_id": job.QLCronID, "name": source.Name, "submitted_at": time.Now().Unix()})
}

func (a *App) handleQingLongJobLog(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	acc, ok := a.resolveAccountFromQuery(w, r)
	if !ok {
		return
	}
	scriptKey := strings.TrimSpace(r.URL.Query().Get("script_key"))
	job, err := a.db.GetAccountScriptJob(r.Context(), acc.ID, scriptKey)
	if errors.Is(err, sql.ErrNoRows) {
		writeError(w, http.StatusNotFound, "该账号尚未创建此脚本任务")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	logText, err := a.qinglong.cronLog(r.Context(), job.QLCronID)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"script_key": scriptKey, "ql_cron_id": job.QLCronID, "log": logText})
}

func (a *App) handleQingLongRuns(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	acc, ok := a.resolveAccountFromQuery(w, r)
	if !ok {
		return
	}
	runs, err := a.accountRunHistory(r.Context(), acc.ID)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"account": acc.Public(), "runs": runs, "count": len(runs)})
}

func (a *App) handleQingLongRunLog(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	ref := strings.TrimSpace(r.URL.Query().Get("ref"))
	logKey := strings.TrimSpace(r.URL.Query().Get("log_key"))
	if r.Method == http.MethodPost {
		var body runLogIn
		if err := decodeOptionalJSON(r, &body); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		ref = strings.TrimSpace(body.Ref)
		logKey = strings.TrimSpace(body.LogKey)
	}
	acc, ok := a.resolveAccountRef(w, r, ref)
	if !ok {
		return
	}
	if logKey == "" {
		writeError(w, http.StatusBadRequest, "缺少日志键")
		return
	}
	runs, err := a.accountRunHistory(r.Context(), acc.ID)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	var selected *accountRunPublic
	for i := range runs {
		if runs[i].LogKey == logKey {
			selected = &runs[i]
			break
		}
	}
	if selected == nil {
		writeError(w, http.StatusNotFound, "该日志不属于当前账号或已被清理")
		return
	}
	latest := true
	for i := range runs {
		if runs[i].QLCronID == selected.QLCronID && runs[i].StartedAt > selected.StartedAt {
			latest = false
			break
		}
	}
	separator := strings.LastIndex(logKey, "/")
	if separator <= 0 || separator == len(logKey)-1 {
		writeError(w, http.StatusBadRequest, "日志路径不合法")
		return
	}
	var logText string
	var logErr error
	if latest && a.qinglong.getPanelType() != PanelTypeArcadia {
		// QingLong's task-log endpoint is the same path used by its own UI and
		// avoids reverse proxies that block or time out /open/logs/detail.
		logText, logErr = a.qinglong.cronLog(r.Context(), selected.QLCronID)
		if logErr != nil {
			logText, logErr = a.qinglong.logDetail(r.Context(), logKey[:separator], logKey[separator+1:])
		}
	} else {
		// Historical files (and Arcadia's isolated files) are addressed by the
		// log key because there is no reliable current-task fallback.
		logText, logErr = a.qinglong.logDetail(r.Context(), logKey[:separator], logKey[separator+1:])
	}
	if logErr != nil {
		writeError(w, http.StatusBadGateway, logErr.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"account_id": acc.ID, "script_key": selected.ScriptKey, "log_key": logKey, "log": logText})
}

func (a *App) accountRunHistory(ctx context.Context, accountID int64) ([]accountRunPublic, error) {
	// History only needs the managed cron records. Avoid rebuilding the entire
	// script catalog on every refresh; that can require several panel requests
	// and is especially fragile behind a reverse proxy.
	crons, err := a.qinglong.listCrons(ctx, "")
	if err != nil {
		return nil, err
	}
	cronsByID := make(map[int64]qingLongCron, len(crons))
	for _, cron := range crons {
		cronsByID[cron.ID] = cron
	}
	jobs, err := a.db.ListAccountScriptJobs(ctx, accountID)
	if err != nil {
		return nil, err
	}
	logs, err := a.qinglong.listLogs(ctx)
	if err != nil {
		return nil, err
	}
	sourceByKey := make(map[string]string)
	if repos, repoErr := qingLongRepoRoots(a.cfg.QingLongRepo); repoErr == nil {
		for _, cron := range crons {
			if key, _, ok := parseScriptKeyFromCron(cron, repos); ok {
				sourceByKey[key] = cron.Name
			}
		}
	}
	logRoots := make(map[string]qingLongLogEntry, len(logs))
	for _, entry := range logs {
		logRoots[entry.Key] = entry
		if entry.Title != "" {
			logRoots[entry.Title] = entry
		}
	}
	out := make([]accountRunPublic, 0)
	for _, job := range jobs {
		cron, exists := cronsByID[job.QLCronID]
		if !exists {
			continue
		}
		rootKey := strings.Trim(cron.LogName, "/")
		if rootKey == "" {
			if separator := strings.Index(cron.LogPath, "/"); separator > 0 {
				rootKey = cron.LogPath[:separator]
			}
		}
		root, exists := logRoots[rootKey]
		children := append([]qingLongLogEntry(nil), root.Children...)
		if !exists && strings.TrimSpace(cron.LogPath) != "" {
			// Some QingLong versions omit the managed directory from /open/logs
			// while still returning the latest file path on the cron record.
			path := strings.Trim(cron.LogPath, "/")
			if separator := strings.LastIndex(path, "/"); separator > 0 && strings.HasSuffix(strings.ToLower(path[separator+1:]), ".log") {
				children = []qingLongLogEntry{{Title: path[separator+1:], Key: path, Type: "file", Size: 0, CreateTime: cron.getLastExecutionAt() * 1000}}
			}
		}
		if len(children) == 0 {
			continue
		}
		sort.Slice(children, func(i, j int) bool { return children[i].CreateTime > children[j].CreateTime })
		name := job.ScriptKey
		if sourceName, found := sourceByKey[job.ScriptKey]; found && !strings.HasPrefix(sourceName, "[YYB:") {
			name = sourceName
		}
		for index, entry := range children {
			if entry.Type != "file" || !strings.HasSuffix(strings.ToLower(entry.Title), ".log") {
				continue
			}
			logKey := entry.Key
			if logKey == "" {
				logKey = strings.TrimRight(root.Key, "/") + "/" + entry.Title
			}
			running := cron.running() && index == 0
			status := "已完成"
			if running {
				status = "运行中"
			}
			out = append(out, accountRunPublic{
				AccountID: accountID, ScriptKey: job.ScriptKey, Name: name, QLCronID: cron.ID,
				LogKey: logKey, StartedAt: entry.CreateTime / 1000, Size: entry.Size, Running: running, TaskStatus: status,
			})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].StartedAt > out[j].StartedAt })
	if len(out) > 100 {
		out = out[:100]
	}
	return out, nil
}

func (a *App) handleQingLongPush(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		acc, ok := a.resolveAccountFromQuery(w, r)
		if !ok {
			return
		}
		setting, err := a.db.AccountPushSettingOrDefault(r.Context(), acc.ID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		public, err := a.pushSettingPublic(r.Context(), setting)
		if err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, public)
	case http.MethodPut:
		var body pushSettingIn
		if err := decodeOptionalJSON(r, &body); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		acc, ok := a.resolveAccountRef(w, r, body.Ref)
		if !ok {
			return
		}
		setting, err := a.savePushSetting(r.Context(), acc, body)
		if err != nil {
			if errors.Is(err, errPushTokenRequired) {
				writeError(w, http.StatusBadRequest, err.Error())
			} else {
				writeError(w, http.StatusBadGateway, err.Error())
			}
			return
		}
		public, err := a.pushSettingPublic(r.Context(), setting)
		if err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, public)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (a *App) scriptCatalog(ctx context.Context) ([]scriptSource, map[int64]qingLongCron, error) {
	if !a.qinglong.configured() {
		return nil, nil, fmt.Errorf("面板 OpenAPI 未配置")
	}
	repos, err := qingLongRepoRoots(a.cfg.QingLongRepo)
	if err != nil {
		return nil, nil, err
	}
	byKey := make(map[string]scriptSource)
	byID := make(map[int64]qingLongCron)

	var crons []qingLongCron
	for _, repo := range repos {
		list, err := a.qinglong.listCrons(ctx, repo)
		if err == nil && len(list) > 0 {
			crons = append(crons, list...)
		}
	}
	if len(crons) == 0 {
		list, err := a.qinglong.listCrons(ctx, "")
		if err != nil {
			return nil, nil, err
		}
		crons = list
	}

	for _, cron := range crons {
		byID[cron.ID] = cron
		key, repo, ok := parseScriptKeyFromCron(cron, repos)
		if !ok {
			continue
		}
		if _, exists := byKey[key]; !exists {
			byKey[key] = scriptSource{Key: key, Name: cron.Name, Schedule: cron.getSchedule(), TaskRoot: repo, Cron: cron}
		}
	}
	out := make([]scriptSource, 0, len(byKey))
	for _, source := range byKey {
		out = append(out, source)
	}
	sort.Slice(out, func(i, j int) bool {
		return strings.ToLower(out[i].Name) < strings.ToLower(out[j].Name)
	})
	return out, byID, nil
}

func parseScriptKeyFromCron(cron qingLongCron, repos []string) (string, string, bool) {
	if strings.HasPrefix(cron.Name, "[YYB:") {
		return "", "", false
	}
	cmd := strings.TrimSpace(cron.Command)
	for _, p := range []string{"arcadia run ", "task ", "node ", "python3 ", "python "} {
		if strings.HasPrefix(cmd, p) {
			cmd = strings.TrimSpace(strings.TrimPrefix(cmd, p))
		}
	}
	// Daidai and Windows-based QingLong clients may return task commands with
	// backslashes. Normalize before matching configured repository roots so the
	// same task is visible regardless of the panel's path separator.
	cmd = strings.ReplaceAll(cmd, "\\", "/")
	for _, repo := range repos {
		cleanRepo := strings.Trim(strings.TrimSpace(repo), "/")
		prefix := cleanRepo + "/"
		if strings.HasPrefix(cmd, prefix) {
			key := strings.TrimSpace(strings.TrimPrefix(cmd, prefix))
			if validScriptKey.MatchString(key) && !isIgnoredScriptKey(key) {
				return key, cleanRepo, true
			}
		}
		if idx := strings.Index(cmd, "/"+cleanRepo+"/"); idx != -1 {
			key := strings.TrimSpace(cmd[idx+len("/"+cleanRepo+"/"):])
			if validScriptKey.MatchString(key) && !isIgnoredScriptKey(key) {
				return key, cleanRepo, true
			}
		}
	}
	return "", "", false
}

func isIgnoredScriptKey(key string) bool {
	key = strings.TrimSpace(key)
	return key == "SendNotify.py" || strings.Contains(key, "eoos_checkin.py")
}

func (a *App) ensureAccountJob(ctx context.Context, acc *store.WechatAccount, scriptKey string) (*store.AccountScriptJob, scriptSource, error) {
	scriptKey = strings.TrimSpace(scriptKey)
	sources, cronsByID, err := a.scriptCatalog(ctx)
	if err != nil {
		return nil, scriptSource{}, err
	}
	var source scriptSource
	found := false
	for _, candidate := range sources {
		if candidate.Key == scriptKey {
			source, found = candidate, true
			break
		}
	}
	if !found {
		return nil, scriptSource{}, fmt.Errorf("不支持的脚本: %s", scriptKey)
	}
	setting, err := a.db.AccountPushSettingOrDefault(ctx, acc.ID)
	if err != nil {
		return nil, scriptSource{}, err
	}
	command, taskBefore, err := a.accountTaskSpec(acc.ID, source.TaskRoot, scriptKey, setting)
	if err != nil {
		return nil, scriptSource{}, err
	}
	name := managedTaskName(acc, source.Name)
	logName := managedLogName(acc.ID, scriptKey)
	job, err := a.db.GetAccountScriptJob(ctx, acc.ID, scriptKey)
	if err == nil {
		if _, exists := cronsByID[job.QLCronID]; exists {
			if err := a.qinglong.updateCron(ctx, job.QLCronID, name, command, source.Schedule, taskBefore, logName); err != nil {
				return nil, scriptSource{}, err
			}
			return job, source, nil
		}
		_ = a.db.DeleteAccountScriptJob(ctx, acc.ID, scriptKey)
	} else if !errors.Is(err, sql.ErrNoRows) {
		return nil, scriptSource{}, err
	}

	// A YYB reinstall can remove the local account_script_jobs row while the
	// managed cron remains in QingLong. Reuse that cron instead of trying to
	// create a duplicate (QingLong rejects duplicate command/schedule pairs).
	if crons, scanErr := a.qinglong.listCrons(ctx, ""); scanErr == nil {
		if existing, ok := findManagedAccountCron(crons, acc.ID, scriptKey, command); ok {
			if err := a.qinglong.updateCron(ctx, existing.ID, name, command, source.Schedule, taskBefore, logName); err != nil {
				return nil, scriptSource{}, err
			}
			job, err := a.db.UpsertAccountScriptJob(ctx, acc.ID, scriptKey, existing.ID, source.Schedule)
			return job, source, err
		}
	}
	cron, err := a.qinglong.createCron(ctx, name, command, source.Schedule, taskBefore, logName)
	if err != nil {
		return nil, scriptSource{}, err
	}
	if err := a.qinglong.setCronsEnabled(ctx, []int64{cron.ID}, false); err != nil {
		return nil, scriptSource{}, err
	}
	job, err = a.db.UpsertAccountScriptJob(ctx, acc.ID, scriptKey, cron.ID, source.Schedule)
	return job, source, err
}

// findManagedAccountCron locates a cron created for this account and script.
// The account marker is required so a normal, shared QingLong task can never
// be adopted merely because it happens to use the same script.
func findManagedAccountCron(crons []qingLongCron, accountID int64, scriptKey, expectedCommand string) (qingLongCron, bool) {
	prefix := fmt.Sprintf("[YYB:%d]", accountID)
	expected := normalizeCronCommand(expectedCommand)
	for _, cron := range crons {
		if !strings.HasPrefix(strings.TrimSpace(cron.Name), prefix) {
			continue
		}
		actual := normalizeCronCommand(cron.Command)
		if actual == expected || strings.HasSuffix(actual, "/"+strings.TrimSpace(scriptKey)) {
			return cron, true
		}
	}
	return qingLongCron{}, false
}

func normalizeCronCommand(command string) string {
	command = strings.TrimSpace(command)
	for _, prefix := range []string{"arcadia run ", "task ", "node ", "python3 ", "python "} {
		if strings.HasPrefix(command, prefix) {
			command = strings.TrimSpace(strings.TrimPrefix(command, prefix))
			break
		}
	}
	return command
}

func managedTaskName(acc *store.WechatAccount, sourceName string) string {
	prefix := fmt.Sprintf("[YYB:%d]", acc.ID)
	if acc.Remark != nil && strings.TrimSpace(*acc.Remark) != "" {
		return fmt.Sprintf("%s %s · %s", prefix, strings.TrimSpace(*acc.Remark), sourceName)
	}
	return prefix + " " + sourceName
}

func managedLogName(accountID int64, scriptKey string) string {
	sum := sha256.Sum256([]byte(scriptKey))
	return fmt.Sprintf("yyb_account_%d_%x", accountID, sum[:6])
}

func (a *App) accountTaskSpec(accountID int64, taskRoot, scriptKey string, setting *store.AccountPushSetting) (string, string, error) {
	if !validScriptKey.MatchString(scriptKey) {
		return "", "", fmt.Errorf("脚本路径不合法")
	}
	if !regexp.MustCompile(`^[A-Za-z0-9_.:-]+$`).MatchString(a.cfg.QingLongServer) {
		return "", "", fmt.Errorf("YYB_QINGLONG_SERVER 格式不合法")
	}
	if !validQingLongTaskRoot(taskRoot) {
		return "", "", fmt.Errorf("YYB_QINGLONG_REPO 格式不合法")
	}
	pushKey, pushPlusToken, pushPlusTopic, qywxKey := "''", "''", "''", "''"
	switch setting.Channel {
	case "serverchan":
		pushKey = envReference(setting.TokenEnvName)
	case "pushplus":
		pushPlusToken = envReference(setting.TokenEnvName)
		if setting.TopicEnvName != "" {
			pushPlusTopic = envReference(setting.TopicEnvName)
		}
	case "qywx":
		qywxKey = envReference(setting.TokenEnvName)
	}
	command := fmt.Sprintf("task %s/%s", taskRoot, scriptKey)
	taskBefore := fmt.Sprintf(
		"export YYB_SERVER='%s@%d'; export PUSH_KEY=%s; export PUSH_PLUS_TOKEN=%s; export PUSH_PLUS_USER=%s; export QYWX_KEY=%s",
		a.cfg.QingLongServer, accountID, pushKey, pushPlusToken, pushPlusTopic, qywxKey,
	)
	return command, taskBefore, nil
}

func qingLongRepoRoots(raw string) ([]string, error) {
	parts := strings.FieldsFunc(raw, func(r rune) bool {
		return r == ',' || r == ';' || r == '\n' || r == '\r'
	})
	seen := make(map[string]struct{}, len(parts))
	repos := make([]string, 0, len(parts))
	for _, part := range parts {
		repo := strings.Trim(strings.TrimSpace(part), "/")
		if repo == "" {
			continue
		}
		if !validQingLongTaskRoot(repo) {
			return nil, fmt.Errorf("YYB_QINGLONG_REPO 格式不合法: %s", repo)
		}
		if _, exists := seen[repo]; exists {
			continue
		}
		seen[repo] = struct{}{}
		repos = append(repos, repo)
	}
	if len(repos) == 0 {
		return nil, fmt.Errorf("YYB_QINGLONG_REPO 未配置")
	}
	return repos, nil
}

func validQingLongTaskRoot(root string) bool {
	if !regexp.MustCompile(`^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$`).MatchString(root) {
		return false
	}
	for _, segment := range strings.Split(root, "/") {
		if segment == "." || segment == ".." {
			return false
		}
	}
	return true
}

func envReference(name string) string {
	if !regexp.MustCompile(`^[A-Z0-9_]+$`).MatchString(name) {
		return "''"
	}
	return `"${` + name + `:-}"`
}

var errPushTokenRequired = errors.New("首次配置该推送渠道时必须填写 Token 或 Key")

func pushEnvNames(accountID int64) map[string][2]string {
	prefix := "YYB_RUN_ACCOUNT_" + strconv.FormatInt(accountID, 10) + "_"
	return map[string][2]string{
		"serverchan": {prefix + "SERVERCHAN_KEY", ""},
		"pushplus":   {prefix + "PUSHPLUS_TOKEN", prefix + "PUSHPLUS_TOPIC"},
		"qywx":       {prefix + "QYWX_KEY", ""},
	}
}

func (a *App) savePushSetting(ctx context.Context, acc *store.WechatAccount, body pushSettingIn) (*store.AccountPushSetting, error) {
	channel := strings.ToLower(strings.TrimSpace(body.Channel))
	if channel == "" {
		channel = "none"
	}
	names := pushEnvNames(acc.ID)
	if channel != "none" {
		if _, ok := names[channel]; !ok {
			return nil, fmt.Errorf("不支持的推送渠道")
		}
	}
	allNames := make([]string, 0, 4)
	for _, pair := range names {
		allNames = append(allNames, pair[0])
		if pair[1] != "" {
			allNames = append(allNames, pair[1])
		}
	}
	if channel == "none" {
		if err := a.qinglong.setNamedEnvsEnabled(ctx, allNames, false); err != nil {
			return nil, err
		}
		setting, err := a.db.UpsertAccountPushSetting(ctx, acc.ID, "none", "", "")
		if err != nil {
			return nil, err
		}
		if err := a.refreshAccountJobCommands(ctx, acc, setting); err != nil {
			return nil, err
		}
		return setting, nil
	}
	selected := names[channel]
	configured, err := a.namedEnvHasValue(ctx, selected[0])
	if err != nil {
		return nil, err
	}
	token := strings.TrimSpace(body.Token)
	if token == "" && !configured {
		return nil, errPushTokenRequired
	}
	if token != "" {
		if err := a.qinglong.upsertEnv(ctx, selected[0], token, fmt.Sprintf("YYB Go 账号 %d %s 推送", acc.ID, channel)); err != nil {
			return nil, err
		}
	}
	if selected[1] != "" && body.Topic != nil {
		if err := a.qinglong.upsertEnv(ctx, selected[1], strings.TrimSpace(*body.Topic), fmt.Sprintf("YYB Go 账号 %d PushPlus 群组", acc.ID)); err != nil {
			return nil, err
		}
	}
	otherNames := make([]string, 0, len(allNames))
	for _, name := range allNames {
		if name != selected[0] && name != selected[1] {
			otherNames = append(otherNames, name)
		}
	}
	if err := a.qinglong.setNamedEnvsEnabled(ctx, otherNames, false); err != nil {
		return nil, err
	}
	selectedNames := []string{selected[0]}
	if selected[1] != "" {
		selectedNames = append(selectedNames, selected[1])
	}
	if err := a.qinglong.setNamedEnvsEnabled(ctx, selectedNames, true); err != nil {
		return nil, err
	}
	setting, err := a.db.UpsertAccountPushSetting(ctx, acc.ID, channel, selected[0], selected[1])
	if err != nil {
		return nil, err
	}
	if err := a.refreshAccountJobCommands(ctx, acc, setting); err != nil {
		return nil, err
	}
	return setting, nil
}

func (a *App) refreshAccountJobCommands(ctx context.Context, acc *store.WechatAccount, setting *store.AccountPushSetting) error {
	sources, cronsByID, err := a.scriptCatalog(ctx)
	if err != nil {
		return err
	}
	sourceByKey := make(map[string]scriptSource, len(sources))
	for _, source := range sources {
		sourceByKey[source.Key] = source
	}
	jobs, err := a.db.ListAccountScriptJobs(ctx, acc.ID)
	if err != nil {
		return err
	}
	for _, job := range jobs {
		source, sourceExists := sourceByKey[job.ScriptKey]
		if _, cronExists := cronsByID[job.QLCronID]; !sourceExists || !cronExists {
			continue
		}
		command, taskBefore, err := a.accountTaskSpec(acc.ID, source.TaskRoot, job.ScriptKey, setting)
		if err != nil {
			return err
		}
		if err := a.qinglong.updateCron(ctx, job.QLCronID, managedTaskName(acc, source.Name), command, source.Schedule, taskBefore, managedLogName(acc.ID, job.ScriptKey)); err != nil {
			return err
		}
	}
	return nil
}

func (a *App) pushSettingPublic(ctx context.Context, setting *store.AccountPushSetting) (pushSettingPublic, error) {
	out := pushSettingPublic{Channel: setting.Channel}
	if setting.Channel == "none" || setting.TokenEnvName == "" {
		return out, nil
	}
	var err error
	out.TokenConfigured, err = a.namedEnvHasValue(ctx, setting.TokenEnvName)
	if err != nil {
		return out, err
	}
	if setting.TopicEnvName != "" {
		out.TopicConfigured, err = a.namedEnvHasValue(ctx, setting.TopicEnvName)
	}
	return out, err
}

func (a *App) namedEnvHasValue(ctx context.Context, name string) (bool, error) {
	if name == "" {
		return false, nil
	}
	envs, err := a.qinglong.listEnvs(ctx, name)
	if err != nil {
		return false, err
	}
	for _, env := range envs {
		if env.Name == name && strings.TrimSpace(env.Value) != "" {
			return true, nil
		}
	}
	return false, nil
}

func writeRunError(w http.ResponseWriter, err error) {
	if strings.HasPrefix(err.Error(), "不支持的脚本") || strings.Contains(err.Error(), "格式不合法") {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeError(w, http.StatusBadGateway, err.Error())
}
