package httpapi

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
)

// Runtime settings: the operator-facing surface for values that used to be
// env-only. Pacing knobs ride the existing tuning.json hot-reload path (the
// pacer re-reads it within a second and pushes the login budget and
// fresh-session window into the pool); the dynamic AppID deny list carries
// its own setters. Secrets, auth databases and bootstrap values stay
// env-only on purpose.

type runtimeSettingsIn struct {
	MinGapMS             *int64 `json:"code_min_gap_ms"`
	MaxWaitMS            *int64 `json:"code_max_wait_ms"`
	LoginBudget          *int64 `json:"login_budget"`
	LoginWindowMS        *int64 `json:"login_window_ms"`
	FreshWindowMS        *int64 `json:"fresh_session_window_ms"`
	DynamicDenyEnabled   *bool  `json:"dynamic_deny_enabled"`
	DynamicDenyThreshold *int64 `json:"dynamic_deny_threshold"`
	// Reset removes the tuning file so every pacing knob returns to the env
	// defaults. Dynamic-deny fields still apply when present.
	Reset bool `json:"reset"`
}

func (a *App) handleRuntimeSettings(w http.ResponseWriter, r *http.Request) {
	// Local mode (no auth store) is administered on the machine; otherwise
	// only admins may change traffic shaping.
	if a.auth != nil {
		if !requireAdmin(w, r) {
			return
		}
	}
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, a.runtimeSettingsOut())
	case http.MethodPut:
		var body runtimeSettingsIn
		if err := decodeOptionalJSON(r, &body); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if err := a.applyRuntimeSettings(r, body); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		a.events.Emit("management.completed", eventlog.Fields{Route: "/api/runtime-settings", Method: http.MethodPut})
		writeJSON(w, http.StatusOK, a.runtimeSettingsOut())
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (a *App) runtimeSettingsOut() map[string]any {
	s := a.pacer.current()
	cfg := a.cfg
	healthy := func(ms int64) bool { return ms > 0 }
	return map[string]any{
		"effective": map[string]any{
			"code_min_gap_ms":         s.MinGap.Milliseconds(),
			"code_max_wait_ms":        s.MaxWait.Milliseconds(),
			"login_budget":            s.LoginBudget,
			"login_window_ms":         s.LoginWindow.Milliseconds(),
			"fresh_session_window_ms": s.FreshWindow.Milliseconds(),
		},
		"defaults": map[string]any{
			"code_min_gap_ms":         cfg.CodeMinGap.Milliseconds(),
			"code_max_wait_ms":        cfg.CodeMaxWait.Milliseconds(),
			"login_budget":            cfg.LoginBudget,
			"login_window_ms":         cfg.LoginWindow.Milliseconds(),
			"fresh_session_window_ms": protocol.EffectiveFreshWindow(cfg.FreshSessionWindow).Milliseconds(),
		},
		"tuning_file": map[string]any{
			"path":    a.pacer.filePath(),
			"present": a.tuningFilePresent(),
		},
		"dynamic_deny": map[string]any{
			"enabled":   a.dynamicAppIDs.enabledNow(),
			"threshold": a.dynamicAppIDs.currentThreshold(),
			"learned":   len(a.dynamicAppIDs.list()),
		},
		"notes": []string{
			"取码间隔/排队等待/登录预算/新鲜会话窗口写入数据库旁的 tuning.json，1 秒内生效并保留到重启之后",
			"动态黑名单关闭后不再学习也不再拦截，已学习的名单保留可查看/清除",
		},
		"healthy": healthy(s.MinGap.Milliseconds()) && s.LoginBudget >= 0,
	}
}

func (a *App) tuningFilePresent() bool {
	info, err := os.Stat(a.pacer.filePath())
	return err == nil && info.Size() >= 0
}

func (a *App) applyRuntimeSettings(r *http.Request, body runtimeSettingsIn) error {
	if body.Reset {
		if err := os.Remove(a.pacer.filePath()); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("重置 tuning 文件失败: %w", err)
		}
	}
	pacingChanged := body.MinGapMS != nil || body.MaxWaitMS != nil || body.LoginBudget != nil ||
		body.LoginWindowMS != nil || body.FreshWindowMS != nil
	if pacingChanged {
		if err := a.writeTuningFile(body); err != nil {
			return err
		}
	}
	if body.DynamicDenyEnabled != nil {
		a.dynamicAppIDs.setEnabled(*body.DynamicDenyEnabled)
	}
	if body.DynamicDenyThreshold != nil {
		threshold := *body.DynamicDenyThreshold
		if threshold < 1 || threshold > 50 {
			return fmt.Errorf("动态黑名单阈值需在 1-50 之间")
		}
		a.dynamicAppIDs.setThreshold(int(threshold))
	}
	return nil
}

// writeTuningFile merges the provided pacing knobs into tuning.json. Fields
// the caller left out keep their current file value, so one card can change
// one knob without restating the others.
func (a *App) writeTuningFile(body runtimeSettingsIn) error {
	path := a.pacer.filePath()
	current := map[string]any{}
	if raw, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(raw, &current)
	}
	overlay := map[string]any{}
	if body.MinGapMS != nil {
		overlay["code_min_gap_ms"] = *body.MinGapMS
	}
	if body.MaxWaitMS != nil {
		overlay["code_max_wait_ms"] = *body.MaxWaitMS
	}
	if body.LoginBudget != nil {
		if *body.LoginBudget < 0 || *body.LoginBudget > 1000 {
			return fmt.Errorf("登录预算需在 0-1000 之间（0 关闭重建）")
		}
		overlay["login_budget"] = *body.LoginBudget
	}
	if body.LoginWindowMS != nil {
		overlay["login_window_ms"] = *body.LoginWindowMS
	}
	if body.FreshWindowMS != nil {
		if *body.FreshWindowMS < 0 || *body.FreshWindowMS > int64((time.Hour).Milliseconds()) {
			return fmt.Errorf("新鲜会话窗口需在 0-3600000 毫秒之间（0 关闭保护）")
		}
		overlay["fresh_session_window_ms"] = *body.FreshWindowMS
	}
	for key, value := range overlay {
		current[key] = value
	}
	raw, err := json.MarshalIndent(current, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		return fmt.Errorf("写入 tuning 文件失败: %w", err)
	}
	return nil
}
