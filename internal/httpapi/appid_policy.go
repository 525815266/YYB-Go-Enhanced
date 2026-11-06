package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

// builtinDeniedAppIDs are AppIDs of website applications (the open platform
// kind behind qrconnect logins), not mini programs. A wx.login code request for
// one of them reaches the upstream and comes back without a code, and the
// account OAuth layer is refused within seconds:
//   - wx5cd60c5d4817a188 (Tencent PC Manager): on 2026-11-03 two healthy
//     accounts lost their authorization right after one such request, while the
//     four accounts the circuit breaker kept off the upstream stayed alive; one
//     request on a fresh authorization reproduced it, one mini program request
//     on another did not. See docs/website-appid-getcode-20261103.md.
//   - wx2f5d8f9715c59d10 (JD passport): the same kind of AppID. It never
//     returned a code and has not been tried on its own.
//
// YYB_CODE_ALLOW_APPIDS lifts a refusal for operators who know better.
var builtinDeniedAppIDs = map[string]string{
	"wx5cd60c5d4817a188": "腾讯电脑管家网站应用",
	"wx2f5d8f9715c59d10": "京东网页登录护照",
}

// appIDPolicy decides which AppIDs a code request may carry before anything is
// sent upstream. It is traffic policy only: a refusal never changes account
// status, clears credentials or touches the database. The deny/allow lists the
// operator manages in the panel are persisted in the settings store and
// override the env-configured lists (an allowance always wins).
type appIDPolicy struct {
	mu     sync.Mutex
	denied map[string]string // lower-case AppID to the reason shown to the caller
	db     *store.DB
}

const (
	appIDPolicyDenySettingKey  = "code_appid_policy_deny"
	appIDPolicyAllowSettingKey = "code_appid_policy_allow"
)

// newAppIDPolicy starts from the built-in list, adds the denials the operator
// configured (env + panel-persisted) and then removes everything the operator
// allows, so an allowance always wins.
func newAppIDPolicy(deny, allow []string, db *store.DB) *appIDPolicy {
	p := &appIDPolicy{denied: map[string]string{}, db: db}
	for id, label := range builtinDeniedAppIDs {
		p.denied[id] = fmt.Sprintf("是网站应用而不是小程序（%s），用它取码会让账号授权失效；确需使用请在服务端设置 YYB_CODE_ALLOW_APPIDS", label)
	}
	for _, id := range deny {
		if id = normalizeAppID(id); id != "" {
			if _, builtin := p.denied[id]; !builtin {
				p.denied[id] = "已在服务端 YYB_CODE_DENY_APPIDS 中禁用"
			}
		}
	}
	for _, id := range p.loadPersisted(appIDPolicyDenySettingKey) {
		if id = normalizeAppID(id); id != "" {
			if _, builtin := p.denied[id]; !builtin {
				p.denied[id] = "已在面板取码黑名单中禁用"
			}
		}
	}
	for _, id := range allow {
		delete(p.denied, normalizeAppID(id))
	}
	for _, id := range p.loadPersisted(appIDPolicyAllowSettingKey) {
		delete(p.denied, normalizeAppID(id))
	}
	return p
}

func (p *appIDPolicy) loadPersisted(key string) []string {
	if p.db == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	raw, err := p.db.GetSetting(ctx, key)
	if err != nil || raw == "" {
		return nil
	}
	var list []string
	if json.Unmarshal([]byte(raw), &list) != nil {
		return nil
	}
	return list
}

func (p *appIDPolicy) persistList(key string, list []string) error {
	if p.db == nil {
		return errors.New("policy store unavailable")
	}
	raw := "[]"
	if len(list) > 0 {
		encoded, err := json.Marshal(list)
		if err != nil {
			return err
		}
		raw = string(encoded)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return p.db.SetSetting(ctx, key, raw)
}

func (p *appIDPolicy) removePersisted(key, appID string) error {
	list := p.loadPersisted(key)
	kept := make([]string, 0, len(list))
	for _, existing := range list {
		if normalizeAppID(existing) != normalizeAppID(appID) {
			kept = append(kept, existing)
		}
	}
	if len(kept) == len(list) {
		return nil
	}
	return p.persistList(key, kept)
}

// AddDeny persists an operator-managed deny entry. Builtin entries cannot be
// re-denied (already refused); an existing allow entry is lifted.
func (p *appIDPolicy) AddDeny(appID, reason string) error {
	id := normalizeAppID(appID)
	if id == "" {
		return errors.New("appid 为空")
	}
	if _, builtin := builtinDeniedAppIDs[id]; builtin {
		return errors.New("该 AppID 是内置拦截项，无需重复添加")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if reason == "" {
		reason = "管理员在面板黑名单中禁用"
	}
	p.denied[id] = reason
	list := p.loadPersisted(appIDPolicyDenySettingKey)
	for _, existing := range list {
		if normalizeAppID(existing) == id {
			return nil
		}
	}
	list = append(list, id)
	if err := p.persistList(appIDPolicyDenySettingKey, list); err != nil {
		return err
	}
	return p.removePersisted(appIDPolicyAllowSettingKey, id)
}

// RemoveDeny lifts an operator-managed deny entry (builtin and env entries
// cannot be lifted from here).
func (p *appIDPolicy) RemoveDeny(appID string) error {
	id := normalizeAppID(appID)
	if _, builtin := builtinDeniedAppIDs[id]; builtin {
		return errors.New("内置拦截项不可移除")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	reason, denied := p.denied[id]
	if !denied || strings.Contains(reason, "YYB_CODE_DENY_APPIDS") {
		return errors.New("该 AppID 不在面板黑名单中")
	}
	delete(p.denied, id)
	list := p.loadPersisted(appIDPolicyDenySettingKey)
	kept := make([]string, 0, len(list))
	for _, existing := range list {
		if normalizeAppID(existing) != id {
			kept = append(kept, existing)
		}
	}
	return p.persistList(appIDPolicyDenySettingKey, kept)
}

// AddAllow whitelists an AppID, lifting any refusal for it.
func (p *appIDPolicy) AddAllow(appID string) error {
	id := normalizeAppID(appID)
	if id == "" {
		return errors.New("appid 为空")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.denied, id)
	list := p.loadPersisted(appIDPolicyAllowSettingKey)
	for _, existing := range list {
		if normalizeAppID(existing) == id {
			return nil
		}
	}
	list = append(list, id)
	if err := p.persistList(appIDPolicyAllowSettingKey, list); err != nil {
		return err
	}
	return p.removePersisted(appIDPolicyDenySettingKey, id)
}

// RemoveAllow drops a whitelisted AppID so the builtin/env/deny lists apply again.
func (p *appIDPolicy) RemoveAllow(appID string) error {
	id := normalizeAppID(appID)
	p.mu.Lock()
	defer p.mu.Unlock()
	list := p.loadPersisted(appIDPolicyAllowSettingKey)
	kept := make([]string, 0, len(list))
	for _, existing := range list {
		if normalizeAppID(existing) != id {
			kept = append(kept, existing)
		}
	}
	if err := p.persistList(appIDPolicyAllowSettingKey, kept); err != nil {
		return err
	}
	if _, builtin := builtinDeniedAppIDs[id]; builtin {
		p.denied[id] = fmt.Sprintf("是网站应用而不是小程序（%s），用它取码会让账号授权失效", builtinDeniedAppIDs[id])
	}
	return nil
}

// persistedDenyList returns a copy of the operator-managed deny entries.
func (p *appIDPolicy) persistedDenyList() []string {
	return p.loadPersisted(appIDPolicyDenySettingKey)
}

// persistedAllowList returns a copy of the operator-managed allow entries.
func (p *appIDPolicy) persistedAllowList() []string {
	return p.loadPersisted(appIDPolicyAllowSettingKey)
}

func normalizeAppID(appID string) string {
	return strings.ToLower(strings.TrimSpace(appID))
}

// check returns a non-nil error when a code request for appID must not reach
// the upstream. A nil policy allows everything.
func (p *appIDPolicy) check(appID string) *appIDDeniedError {
	if p == nil {
		return nil
	}
	why, denied := p.denied[normalizeAppID(appID)]
	if !denied {
		return nil
	}
	return &appIDDeniedError{AppID: strings.TrimSpace(appID), Why: why}
}

// appIDDeniedError is a local decision, never an upstream rejection: it carries
// no WeChat code and must not be mapped to account expiry or fed to the breaker.
type appIDDeniedError struct {
	AppID string
	Why   string
}

func (e *appIDDeniedError) Error() string {
	return fmt.Sprintf("取码已拒绝：%s %s，请求未发往上游", e.AppID, e.Why)
}

// writeIfAppIDDenied renders a refused AppID as HTTP 403, distinct from a
// held-off 429, an upstream 502 and a re-scan 409. It reports whether it
// handled the error.
func writeIfAppIDDenied(w http.ResponseWriter, err error) bool {
	var denied *appIDDeniedError
	if !errors.As(err, &denied) {
		return false
	}
	writeRawJSON(w, http.StatusForbidden, apiEnvelope{
		Code: http.StatusForbidden,
		Msg:  "call refused: " + denied.Error(),
		Data: map[string]any{"appid_denied": true, "app_id": denied.AppID, "request_rejected": false},
	})
	return true
}

// dynamicAppIDDenySettingKey stores the learned list across restarts, so a
// script that fires a dangerous AppID once does not get a second chance after
// the service restarts.
const dynamicAppIDDenySettingKey = "code_dynamic_deny_appids"

// dynamicAppIDDenyTotalStrikes is the number of code-less outcomes for one
// AppID that denies it regardless of how many accounts produced them. It caps
// the damage a bad AppID can do on a single-account deployment, where the
// distinct-account rule below would never fire.
const dynamicAppIDDenyTotalStrikes = 4

// appidStrikes accumulates the code-less outcomes of one AppID: how many
// distinct accounts failed and how many requests failed in total.
type appidStrikes struct {
	accounts map[int64]bool
	attempts int
}

// dynamicAppIDDeny learns website-application AppIDs at run time. A wx.login
// code request that reaches the upstream and comes back without a code denies
// that AppID for every account once the outcome repeats: the default needs two
// distinct accounts to fail, or dynamicAppIDDenyTotalStrikes failures in
// total, because one account can also stop receiving codes when the upstream
// puts it into burst refusal after a long run of successes — that night every
// AppID failed on the bursted account while other accounts kept succeeding on
// the same ones (docs/h5-oauth-code.md). A success on any account clears the
// strikes for that AppID. Denials persist across restarts; an allow entry in
// YYB_CODE_ALLOW_APPIDS removes a learned AppID again at startup.
type dynamicAppIDDeny struct {
	mu        sync.Mutex
	enabled   bool
	denied    map[string]string
	strikes   map[string]*appidStrikes
	threshold int
	db        *store.DB
}

func newDynamicAppIDDeny(db *store.DB, threshold int, allow []string, disabled bool) *dynamicAppIDDeny {
	if threshold <= 0 {
		threshold = 2
	}
	// The deny struct is always constructed so the operator can toggle it at
	// run time; disabled only stops new learning and enforcement.
	d := &dynamicAppIDDeny{enabled: !disabled, denied: map[string]string{}, strikes: map[string]*appidStrikes{}, threshold: threshold, db: db}
	if db != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if raw, err := db.GetSetting(ctx, dynamicAppIDDenySettingKey); err == nil && raw != "" {
			_ = json.Unmarshal([]byte(raw), &d.denied)
		}
	}
	// The allow list always wins: a learned AppID the operator allows is
	// forgotten here, so the refusal never comes back on a restart.
	if len(allow) > 0 {
		removed := false
		for _, appID := range allow {
			key := normalizeAppID(appID)
			if key == "" {
				continue
			}
			if _, was := d.denied[key]; was {
				delete(d.denied, key)
				removed = true
			}
		}
		if removed {
			d.persistLocked()
		}
	}
	return d
}

// check reports the refusal reason for a learned AppID.
func (d *dynamicAppIDDeny) check(appID string) (string, bool) {
	if d == nil || !d.enabled {
		return "", false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	why, denied := d.denied[normalizeAppID(appID)]
	return why, denied
}

// setEnabled toggles learning and enforcement at run time. The learned list
// is kept either way so the operator can inspect or clear it.
func (d *dynamicAppIDDeny) setEnabled(enabled bool) {
	if d == nil {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	d.enabled = enabled
}

func (d *dynamicAppIDDeny) setThreshold(threshold int) {
	if d == nil {
		return
	}
	if threshold <= 0 {
		threshold = 2
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	d.threshold = threshold
}

func (d *dynamicAppIDDeny) currentThreshold() int {
	if d == nil {
		return 0
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.threshold
}

func (d *dynamicAppIDDeny) enabledNow() bool {
	if d == nil {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.enabled
}

// recordStrike counts a code request that reached the upstream and returned no
// code, per account. It reports whether this strike newly denied the AppID.
func (d *dynamicAppIDDeny) recordStrike(appID string, accountID int64) bool {
	if d == nil || !d.enabled {
		return false
	}
	key := normalizeAppID(appID)
	if key == "" {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, already := d.denied[key]; already {
		return false
	}
	set, exists := d.strikes[key]
	if !exists {
		set = &appidStrikes{accounts: map[int64]bool{}}
		d.strikes[key] = set
	}
	set.accounts[accountID] = true
	set.attempts++
	// Deny once the failure repeats on the threshold of distinct accounts, or
	// once one AppID has accumulated dynamicAppIDDenyTotalStrikes failures in
	// total (a single-account deployment never reaches two accounts).
	if len(set.accounts) < d.threshold && set.attempts < dynamicAppIDDenyTotalStrikes {
		return false
	}
	d.denied[key] = fmt.Sprintf("自动学习：%s 取码未返回 code 已在 %d 个账号上出现（累计 %d 次，网站应用/公众号特征），为保护账号授权已自动禁用（%s）", key, len(set.accounts), set.attempts, time.Now().Format("2006-01-02 15:04"))
	delete(d.strikes, key)
	return d.persistLocked()
}

// recordSuccess forgets strikes for an AppID that did return a code, so one
// bursted account cannot deny a healthy AppID the other accounts still use.
func (d *dynamicAppIDDeny) recordSuccess(appID string) {
	if d == nil {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	delete(d.strikes, normalizeAppID(appID))
}

func (d *dynamicAppIDDeny) list() map[string]string {
	if d == nil {
		return map[string]string{}
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make(map[string]string, len(d.denied))
	for key, why := range d.denied {
		out[key] = why
	}
	return out
}

// clear forgets one or every learned AppID and persists the change. A cleared
// AppID starts striking from zero again.
func (d *dynamicAppIDDeny) clear(appID string) (map[string]string, error) {
	if d == nil {
		return map[string]string{}, nil
	}
	key := normalizeAppID(appID)
	d.mu.Lock()
	if key == "" {
		d.denied = map[string]string{}
		d.strikes = map[string]*appidStrikes{}
	} else {
		delete(d.denied, key)
		delete(d.strikes, key)
	}
	out := make(map[string]string, len(d.denied))
	for learned, why := range d.denied {
		out[learned] = why
	}
	var err error
	if d.db != nil {
		var raw []byte
		if raw, err = json.Marshal(d.denied); err == nil {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			if setErr := d.db.SetSetting(ctx, dynamicAppIDDenySettingKey, string(raw)); setErr != nil {
				err = setErr
			}
		}
	}
	d.mu.Unlock()
	return out, err
}

func (d *dynamicAppIDDeny) persistLocked() bool {
	if d.db == nil {
		return true
	}
	raw, err := json.Marshal(d.denied)
	if err != nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return d.db.SetSetting(ctx, dynamicAppIDDenySettingKey, string(raw)) == nil
}

// handleAppIDDeny is the console surface of the learned deny list: GET shows
// every AppID learned at run time, POST clears one (?app_id=wx…) or all of
// them. The built-in list and the configured env lists are not editable here.
func (a *App) handleAppIDDeny(w http.ResponseWriter, r *http.Request) {
	if a.auth != nil {
		if !requireAdmin(w, r) {
			return
		}
	}
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, a.appIDPolicyView())
		return
	case http.MethodPost:
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if err := r.ParseForm(); err != nil {
		writeError(w, http.StatusBadRequest, "invalid form: "+err.Error())
		return
	}
	appID := strings.TrimSpace(r.FormValue("app_id"))
	action := strings.TrimSpace(r.FormValue("action"))
	if appID == "" {
		appID = strings.TrimSpace(r.FormValue("appid"))
	}
	var message string
	switch action {
	case "", "clear_learned":
		list, err := a.dynamicAppIDs.clear(appID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "persist deny list: "+err.Error())
			return
		}
		message = "已清除动态学习名单"
		_ = list
	case "add_deny":
		if err := a.appIDs.AddDeny(appID, strings.TrimSpace(r.FormValue("reason"))); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		message = "已加入黑名单：" + appID
	case "remove_deny":
		if err := a.appIDs.RemoveDeny(appID); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		message = "已从黑名单移除：" + appID
	case "add_allow":
		if err := a.appIDs.AddAllow(appID); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		message = "已加入白名单：" + appID
	case "remove_allow":
		if err := a.appIDs.RemoveAllow(appID); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		message = "已从白名单移除：" + appID
	default:
		writeError(w, http.StatusBadRequest, "未知 action: "+action)
		return
	}
	a.events.Emit("management.completed", eventlog.Fields{Route: "/api/appid-deny", Method: http.MethodPost, AppID: appID})
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "message": message, "policy": a.appIDPolicyView()})
}

// appIDPolicyView is the full visibility the panel needs: every AppID the
// policy refuses, with the list that refused it, plus the operator-managed
// allow entries and the learned dynamic list.
func (a *App) appIDPolicyView() map[string]any {
	builtin := map[string]string{}
	for id, label := range builtinDeniedAppIDs {
		builtin[id] = label
	}
	envDenied := map[string]string{}
	for _, id := range a.cfg.CodeDenyAppIDs {
		if id = normalizeAppID(id); id != "" {
			if _, isBuiltin := builtin[id]; !isBuiltin {
				envDenied[id] = "YYB_CODE_DENY_APPIDS"
			}
		}
	}
	persistedDeny := map[string]string{}
	for _, id := range a.appIDs.persistedDenyList() {
		if id = normalizeAppID(id); id != "" {
			if _, taken := builtin[id]; !taken {
				if _, taken = envDenied[id]; !taken {
					persistedDeny[id] = "面板黑名单"
				}
			}
		}
	}
	allow := []string{}
	for _, id := range a.cfg.CodeAllowAppIDs {
		if id = normalizeAppID(id); id != "" {
			allow = append(allow, id)
		}
	}
	for _, id := range a.appIDs.persistedAllowList() {
		if id = normalizeAppID(id); id != "" {
			allow = append(allow, id)
		}
	}
	learned := a.dynamicAppIDs.list()
	return map[string]any{
		"builtin":         builtin,
		"env_denied":      envDenied,
		"persisted_deny":  persistedDeny,
		"allow":           allow,
		"learned":         learned,
		"learned_count":   len(learned),
		"denied_total":    len(builtin) + len(envDenied) + len(persistedDeny),
	}
}
