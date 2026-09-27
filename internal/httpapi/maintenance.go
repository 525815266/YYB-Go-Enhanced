package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"yyb_go/internal/version"
)

const maintenanceVersionURL = "https://raw.githubusercontent.com/525815266/YYB-Go-Enhanced/main/VERSION"

var maintenanceSemver = regexp.MustCompile(`^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$`)

type updateChecker struct {
	mu      sync.Mutex
	checked time.Time
	latest  string
	err     error
	client  *http.Client
	url     string
}

func newerMaintenanceVersion(current, latest string) bool {
	if !maintenanceSemver.MatchString(current) || !maintenanceSemver.MatchString(latest) {
		return false
	}
	oldParts, newParts := strings.Split(current, "."), strings.Split(latest, ".")
	for i := range oldParts {
		old, _ := strconv.Atoi(oldParts[i])
		next, _ := strconv.Atoi(newParts[i])
		if old != next {
			return next > old
		}
	}
	return false
}

func (c *updateChecker) check(ctx context.Context) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.checked.IsZero() && time.Since(c.checked) < 5*time.Minute {
		return c.latest, c.err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url, nil)
	if err != nil {
		return "", err
	}
	resp, err := c.client.Do(req)
	c.checked = time.Now()
	if err == nil {
		defer resp.Body.Close()
		var body []byte
		body, err = io.ReadAll(io.LimitReader(resp.Body, 129))
		if err == nil && resp.StatusCode != http.StatusOK {
			err = fmt.Errorf("版本源 HTTP %d", resp.StatusCode)
		}
		latest := strings.TrimSpace(string(body))
		if err == nil && !maintenanceSemver.MatchString(latest) {
			err = fmt.Errorf("版本源格式不正确")
		}
		if err == nil {
			c.latest = latest
		}
	}
	c.err = err
	return c.latest, c.err
}

func (a *App) handleMaintenancePage(w http.ResponseWriter, r *http.Request) {
	if !requireAdmin(w, r) {
		return
	}
	serveFileOrText(w, r, filepath.Join(a.resources.Templates, "maintenance.html"), "Maintenance page missing")
}

func (a *App) maintenanceRequest(ctx context.Context, method, path string, body any) (map[string]any, int, error) {
	if a.cfg.MaintenanceSocket == "" {
		return nil, 0, fmt.Errorf("尚未配置维护执行器")
	}
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", a.cfg.MaintenanceSocket)
	}}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, 0, err
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://maintenance"+path, bytes.NewReader(raw))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("无法连接维护执行器，请检查宿主机服务和 socket 权限")
	}
	defer resp.Body.Close()
	var result map[string]any
	err = json.NewDecoder(io.LimitReader(resp.Body, 16384)).Decode(&result)
	if err != nil {
		return nil, 0, fmt.Errorf("维护执行器返回格式错误")
	}
	return result, resp.StatusCode, nil
}

func (a *App) handleMaintenance(w http.ResponseWriter, r *http.Request) {
	// Unlike legacy local mode, system operations NEVER allow anonymous access.
	if !requireAdmin(w, r) {
		return
	}
	current, _, _ := version.Info()
	if r.Method == http.MethodGet {
		result := map[string]any{"version": current, "available": false, "message": "未配置执行器。Docker 请按维护文档启用；Magisk 请在模块管理器更新或重启设备。"}
		if a.cfg.MaintenanceSocket != "" {
			state, status, err := a.maintenanceRequest(r.Context(), http.MethodGet, "/status", nil)
			if err != nil {
				result["message"] = err.Error()
			} else if status != 200 {
				result["message"] = "维护执行器暂不可用"
			} else {
				result["available"] = true
				result["agent"] = state
				result["message"] = "Docker 维护执行器已连接"
			}
		}
		if r.URL.Query().Get("check") == "1" {
			latest, err := a.updates.check(r.Context())
			result["latest_version"] = latest
			result["has_update"] = err == nil && newerMaintenanceVersion(current, latest)
			if err != nil {
				result["check_error"] = "检查版本失败，请稍后重试：" + err.Error()
			}
		}
		writeJSON(w, 200, result)
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, 405, "method not allowed")
		return
	}
	// Custom header + JSON forces browser preflight; no cross-origin CORS is enabled.
	if r.Header.Get("X-YYB-Maintenance") != "1" || !strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") || r.Header.Get("Sec-Fetch-Site") == "cross-site" {
		writeError(w, 403, "请从系统维护页面操作")
		return
	}
	var body struct {
		Action    string `json:"action"`
		Confirm   bool   `json:"confirm"`
		RequestID string `json:"request_id"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&body) != nil || !body.Confirm || (body.Action != "update" && body.Action != "restart") || !regexp.MustCompile(`^[a-zA-Z0-9-]{16,64}$`).MatchString(body.RequestID) {
		writeError(w, 400, "请确认更新或重启操作")
		return
	}
	if a.cfg.MaintenanceSocket == "" {
		writeError(w, 409, "尚未配置维护执行器，不会停止当前服务")
		return
	}
	payload := map[string]any{"action": body.Action, "request_id": body.RequestID}
	if body.Action == "update" {
		latest, err := a.updates.check(r.Context())
		if err != nil {
			writeError(w, 502, "无法确认目标版本，本次未执行更新")
			return
		}
		payload["expected_version"] = latest
		if !newerMaintenanceVersion(current, latest) {
			writeError(w, 409, "没有更新版本，不会重建或降级当前服务")
			return
		}
	}
	state, status, err := a.maintenanceRequest(r.Context(), http.MethodPost, "/jobs", payload)
	if err != nil {
		writeError(w, 502, err.Error())
		return
	}
	if status != http.StatusAccepted {
		writeError(w, status, fmt.Sprint(state["message"]))
		return
	}
	writeJSON(w, status, state)
}
