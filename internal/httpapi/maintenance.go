package httpapi

import (
	"bytes"
	"context"
	"encoding/base64"
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

const (
	maintenanceVersionURL    = "https://raw.githubusercontent.com/525815266/YYB-Go-Enhanced/main/VERSION"
	maintenanceVersionAPIURL = "https://api.github.com/repos/525815266/YYB-Go-Enhanced/contents/VERSION?ref=main"
)

var maintenanceSemver = regexp.MustCompile(`^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$`)

type updateChecker struct {
	mu          sync.Mutex
	checked     time.Time
	latest      string
	err         error
	client      *http.Client
	url         string
	fallbackURL string
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

func (c *updateChecker) fetch(ctx context.Context, source string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, source, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "YYB-Go-Enhanced-update-checker")
	resp, err := c.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4097))
	if err != nil {
		return "", err
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	latest := strings.TrimSpace(string(body))
	if maintenanceSemver.MatchString(latest) {
		return latest, nil
	}
	var githubFile struct {
		Content  string `json:"content"`
		Encoding string `json:"encoding"`
	}
	if json.Unmarshal(body, &githubFile) == nil && githubFile.Encoding == "base64" {
		decoded, decodeErr := base64.StdEncoding.DecodeString(strings.ReplaceAll(githubFile.Content, "\n", ""))
		if decodeErr == nil {
			latest = strings.TrimSpace(string(decoded))
			if maintenanceSemver.MatchString(latest) {
				return latest, nil
			}
		}
	}
	return "", fmt.Errorf("版本源格式不正确")
}

func (c *updateChecker) check(ctx context.Context) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.checked.IsZero() && time.Since(c.checked) < 5*time.Minute {
		return c.latest, c.err
	}
	sources := []string{c.url}
	if c.fallbackURL != "" && c.fallbackURL != c.url {
		sources = append(sources, c.fallbackURL)
	}
	type result struct {
		version string
		err     error
	}
	requestCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	results := make(chan result, len(sources))
	for _, source := range sources {
		go func(source string) {
			latest, err := c.fetch(requestCtx, source)
			results <- result{version: latest, err: err}
		}(source)
	}
	var failures []string
	for range sources {
		outcome := <-results
		if outcome.err == nil {
			cancel()
			c.checked = time.Now()
			c.latest = outcome.version
			c.err = nil
			return c.latest, nil
		}
		failures = append(failures, outcome.err.Error())
	}
	c.checked = time.Now()
	c.err = fmt.Errorf("所有版本源均不可用：%s", strings.Join(failures, "；"))
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
