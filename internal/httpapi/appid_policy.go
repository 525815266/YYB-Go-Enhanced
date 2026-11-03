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
// status, clears credentials or touches the database.
type appIDPolicy struct {
	denied map[string]string // lower-case AppID to the reason shown to the caller
}

// newAppIDPolicy starts from the built-in list, adds the denials the operator
// configured and then removes everything the operator allows, so an allowance
// always wins.
func newAppIDPolicy(deny, allow []string) *appIDPolicy {
	p := &appIDPolicy{denied: map[string]string{}}
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
	for _, id := range allow {
		delete(p.denied, normalizeAppID(id))
	}
	return p
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

// dynamicAppIDDeny learns website-application AppIDs at run time. The first
// wx.login code request that reaches the upstream and comes back without a
// code (after the session-rebuild retry) denies that AppID for every account:
// docs/website-appid-getcode-20261103.md shows one such request can cost a
// healthy account its authorization, and a genuine mini program that never
// yields a code has no working caller to protect either. An allow entry in
// YYB_CODE_ALLOW_APPIDS still wins over anything learned here.
type dynamicAppIDDeny struct {
	mu        sync.Mutex
	denied    map[string]string
	strikes   map[string]int
	threshold int
	db        *store.DB
}

func newDynamicAppIDDeny(db *store.DB, threshold int, disabled bool) *dynamicAppIDDeny {
	if disabled {
		return nil
	}
	if threshold <= 0 {
		threshold = 1
	}
	d := &dynamicAppIDDeny{denied: map[string]string{}, strikes: map[string]int{}, threshold: threshold, db: db}
	if db != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if raw, err := db.GetSetting(ctx, dynamicAppIDDenySettingKey); err == nil && raw != "" {
			_ = json.Unmarshal([]byte(raw), &d.denied)
		}
	}
	return d
}

// check reports the refusal reason for a learned AppID.
func (d *dynamicAppIDDeny) check(appID string) (string, bool) {
	if d == nil {
		return "", false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	why, denied := d.denied[normalizeAppID(appID)]
	return why, denied
}

// recordStrike counts a code request that reached the upstream and returned no
// code. It reports whether this strike newly denied the AppID.
func (d *dynamicAppIDDeny) recordStrike(appID string) bool {
	if d == nil {
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
	d.strikes[key]++
	if d.strikes[key] < d.threshold {
		return false
	}
	d.denied[key] = fmt.Sprintf("自动学习：%s 取码 %d 次均未返回 code（网站应用/公众号特征），为保护账号授权已自动禁用（%s）", key, d.strikes[key], time.Now().Format("2006-01-02 15:04"))
	d.strikes[key] = 0
	return d.persistLocked()
}

// recordSuccess forgets strikes for an AppID that did return a code, so a
// transient upstream hiccup cannot deny a healthy mini program later.
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
		d.strikes = map[string]int{}
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
	switch r.Method {
	case http.MethodGet, http.MethodPost:
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if r.Method == http.MethodPost {
		if err := r.ParseForm(); err != nil {
			writeError(w, http.StatusBadRequest, "invalid form: "+err.Error())
			return
		}
		list, err := a.dynamicAppIDs.clear(strings.TrimSpace(r.FormValue("app_id")))
		if err != nil {
			writeError(w, http.StatusInternalServerError, "persist deny list: "+err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "denied": list})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"denied": a.dynamicAppIDs.list()})
}
