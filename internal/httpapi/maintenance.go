package httpapi

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"yyb_go/internal/version"
)

const (
	maintenanceVersionURL    = "https://raw.githubusercontent.com/525815266/YYB-Go-Enhanced/main/VERSION"
	maintenanceVersionAPIURL = "https://api.github.com/repos/525815266/YYB-Go-Enhanced/contents/VERSION?ref=main"
	maintenanceReleaseBase   = "https://github.com/525815266/YYB-Go-Enhanced/releases"
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
	releaseURL  string
	customURL   string
	configErr   error
}

func newUpdateChecker(proxyAddress, versionAddress string) *updateChecker {
	// Clone instead of modifying the transport used by account and panel requests.
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyFromEnvironment
	c := &updateChecker{
		client: &http.Client{Transport: transport, Timeout: 10 * time.Second},
		url:    maintenanceVersionURL, fallbackURL: maintenanceVersionAPIURL,
		releaseURL: maintenanceReleaseBase + "/latest",
	}
	if raw := strings.TrimSpace(proxyAddress); raw != "" {
		proxy, err := url.Parse(raw)
		if err != nil || proxy.Hostname() == "" || (proxy.Scheme != "http" && proxy.Scheme != "https" && proxy.Scheme != "socks5" && proxy.Scheme != "socks5h") ||
			(proxy.Path != "" && proxy.Path != "/") || proxy.RawQuery != "" || proxy.Fragment != "" {
			c.configErr = fmt.Errorf("YYB_UPDATE_PROXY 格式错误，请填写 http(s)://或 socks5(h)://主机:端口，不含路径、查询参数或片段")
			return c
		}
		// Explicit update-only proxy takes precedence over global proxy / NO_PROXY.
		transport.Proxy = http.ProxyURL(proxy)
	}
	if raw := strings.TrimSpace(versionAddress); raw != "" {
		source, err := url.Parse(raw)
		if err != nil || source.Hostname() == "" || (source.Scheme != "http" && source.Scheme != "https") || source.User != nil || source.Fragment != "" {
			c.configErr = fmt.Errorf("YYB_UPDATE_VERSION_URL 格式错误，请填写返回版本号的 HTTP(S) 完整地址，不含账号密码或片段")
			return c
		}
		c.customURL = source.String()
	}
	return c
}

func versionRequestError(err error) error {
	// Do not expose mirror query tokens or proxy credentials in API error messages.
	var requestError *url.Error
	if errors.As(err, &requestError) {
		return requestError.Err
	}
	return err
}

type maintenanceRuntime struct {
	Kind              string `json:"kind"`
	OS                string `json:"os"`
	Arch              string `json:"arch"`
	Label             string `json:"label"`
	InstallMode       string `json:"install_mode"`
	ManagedUpdate     bool   `json:"managed_update"`
	DownloadAvailable bool   `json:"download_available"`
	DownloadName      string `json:"download_name,omitempty"`
	DownloadURL       string `json:"download_url,omitempty"`
	ReleaseURL        string `json:"release_url"`
	Instructions      string `json:"instructions"`
}

func releaseURL(version string) string {
	if maintenanceSemver.MatchString(version) {
		return maintenanceReleaseBase + "/tag/v" + version
	}
	return maintenanceReleaseBase + "/latest"
}

func releaseAssetURL(version, name string) string {
	if name == "" {
		return ""
	}
	if maintenanceSemver.MatchString(version) {
		return maintenanceReleaseBase + "/download/v" + version + "/" + name
	}
	return maintenanceReleaseBase + "/latest/download/" + name
}

func runtimeDownload(goos, goarch, version string) (name, label string) {
	suffix := ""
	switch goos {
	case "windows":
		suffix = ".exe"
	case "linux", "darwin":
	default:
		return "", ""
	}
	archLabel := map[string]string{"amd64": "x64", "arm64": "ARM64", "arm": "ARMv7"}[goarch]
	if archLabel == "" || (goarch == "arm" && goos != "linux") {
		return "", ""
	}
	tagArch := goarch
	if goarch == "arm" {
		tagArch = "armv7"
	}
	osLabel := map[string]string{"windows": "Windows", "linux": "Linux", "darwin": "macOS"}[goos]
	return "yyb-go-" + goos + "-" + tagArch + suffix, osLabel + " " + archLabel
}

func describeMaintenanceRuntime(goos, goarch, version string, docker, managed bool) maintenanceRuntime {
	info := maintenanceRuntime{
		Kind: goos, OS: goos, Arch: goarch, InstallMode: "standalone",
		ReleaseURL: releaseURL(version),
	}
	if docker {
		info.Kind = "docker"
		info.Label = "Docker"
		info.InstallMode = "container"
		info.ManagedUpdate = managed
		if managed {
			info.Instructions = "Docker 维护执行器已连接，可在面板更新或重启服务。"
		} else {
			info.Instructions = "Docker 可检查版本；仅在维护执行器连接成功后提供面板更新与重启。"
		}
		return info
	}
	if goos == "android" {
		info.Kind = "magisk"
		info.Label = "Magisk ARM64"
		info.InstallMode = "magisk"
		info.DownloadAvailable = goarch == "arm64" && maintenanceSemver.MatchString(version)
		if info.DownloadAvailable {
			info.DownloadName = "yyb-go-magisk-arm64-" + version + ".zip"
			info.DownloadURL = releaseAssetURL(version, info.DownloadName)
		}
		info.Instructions = "下载模块 ZIP 后在 Magisk 管理器中安装更新，并按提示重启设备。"
		return info
	}
	info.DownloadName, info.Label = runtimeDownload(goos, goarch, version)
	info.DownloadAvailable = info.DownloadName != ""
	info.DownloadURL = releaseAssetURL(version, info.DownloadName)
	if info.DownloadAvailable {
		if goos == "windows" {
			info.Instructions = "下载新版程序，退出当前 YYB Go 后替换 EXE，再重新启动。账号和配置不会写入 EXE。"
		} else {
			info.Instructions = "下载新版程序，停止当前服务后替换可执行文件并重新启动。"
		}
	} else {
		info.Label = goos + "/" + goarch
		info.Instructions = "当前架构暂无预编译文件，请打开 Release 查看可用产物或从源码构建。"
	}
	return info
}

func runningInContainer() bool {
	if runtime.GOOS != "linux" {
		return false
	}
	if _, err := os.Stat("/.dockerenv"); err == nil {
		return true
	}
	raw, err := os.ReadFile("/proc/1/cgroup")
	if err != nil {
		return false
	}
	text := string(raw)
	return strings.Contains(text, "docker") || strings.Contains(text, "containerd") || strings.Contains(text, "kubepods")
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
		return "", versionRequestError(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", versionSourceHTTPError(resp)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4097))
	if err != nil {
		return "", err
	}
	if len(body) > 4096 {
		return "", fmt.Errorf("版本源响应超过 4 KiB")
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

func versionSourceHTTPError(resp *http.Response) error {
	if resp.StatusCode == http.StatusTooManyRequests || (resp.StatusCode == http.StatusForbidden && resp.Header.Get("X-RateLimit-Remaining") == "0") {
		return fmt.Errorf("HTTP %d（GitHub 请求限流，请稍后重试或更换服务出口）", resp.StatusCode)
	}
	if resp.StatusCode == http.StatusForbidden {
		return fmt.Errorf("HTTP 403（访问被拒绝，请检查服务出口或代理；仅凭 403 无法确认是限流）")
	}
	return fmt.Errorf("HTTP %d", resp.StatusCode)
}

// The public Release redirect does not use the API's unauthenticated rate limit.
// Inspect only the official tag destination; never follow a redirect or fetch assets.
func (c *updateChecker) fetchRelease(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, c.releaseURL, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", "YYB-Go-Enhanced-update-checker")
	client := *c.client
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	resp, err := client.Do(req)
	if err != nil {
		return "", versionRequestError(err)
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusMovedPermanently, http.StatusFound, http.StatusSeeOther, http.StatusTemporaryRedirect, http.StatusPermanentRedirect:
	default:
		if resp.StatusCode != http.StatusOK {
			return "", versionSourceHTTPError(resp)
		}
		return "", fmt.Errorf("未返回正式发布版本的跳转")
	}
	location, err := resp.Location()
	if err == nil {
		latest := strings.TrimPrefix(location.String(), maintenanceReleaseBase+"/tag/v")
		if maintenanceSemver.MatchString(latest) {
			return latest, nil
		}
	}
	return "", fmt.Errorf("Release 跳转不是本仓库的正式版本")
}

func (c *updateChecker) check(ctx context.Context) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return c.latest, err
	}
	if c.configErr != nil {
		return "", c.configErr
	}
	cacheTTL := 5 * time.Minute
	if c.err != nil {
		cacheTTL = 30 * time.Second
	}
	if !c.checked.IsZero() && time.Since(c.checked) < cacheTTL {
		return c.latest, c.err
	}
	var failures []string
	if c.customURL != "" {
		latest, err := c.fetch(ctx, c.customURL)
		if err == nil {
			c.checked, c.latest, c.err = time.Now(), latest, nil
			return c.latest, nil
		}
		if ctx.Err() != nil {
			return c.latest, ctx.Err()
		}
		failures = append(failures, "自定义版本源："+err.Error())
	}
	sources := []string{c.url}
	if c.fallbackURL != "" && c.fallbackURL != c.url {
		sources = append(sources, c.fallbackURL)
	}
	type result struct {
		source  string
		version string
		err     error
	}
	requestCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	results := make(chan result, len(sources))
	for _, source := range sources {
		go func(source string) {
			latest, err := c.fetch(requestCtx, source)
			results <- result{source: source, version: latest, err: err}
		}(source)
	}
	for range sources {
		var outcome result
		select {
		case <-ctx.Done():
			return c.latest, ctx.Err()
		case outcome = <-results:
		}
		if outcome.err == nil {
			cancel()
			c.checked = time.Now()
			c.latest = outcome.version
			c.err = nil
			return c.latest, nil
		}
		label := "GitHub Raw"
		if outcome.source == c.fallbackURL {
			label = "GitHub Contents API"
		}
		failures = append(failures, label+"："+outcome.err.Error())
	}
	if c.releaseURL != "" && ctx.Err() == nil {
		latest, err := c.fetchRelease(requestCtx)
		if err == nil {
			c.checked, c.latest, c.err = time.Now(), latest, nil
			return c.latest, nil
		}
		failures = append(failures, "GitHub Release："+err.Error())
	}
	// Closing the browser must not cache a cancellation as a network outage.
	if err := ctx.Err(); err != nil {
		return c.latest, err
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
		dockerRuntime := a.cfg.MaintenanceSocket != "" || runningInContainer()
		platform := describeMaintenanceRuntime(runtime.GOOS, runtime.GOARCH, current, dockerRuntime, false)
		result := map[string]any{
			"version": current, "available": false, "managed_update": false,
			"download_available": platform.DownloadAvailable, "runtime": platform,
			"message": platform.Instructions,
		}
		if a.cfg.MaintenanceSocket != "" {
			state, status, err := a.maintenanceRequest(r.Context(), http.MethodGet, "/status", nil)
			if err != nil {
				result["message"] = err.Error()
			} else if status != 200 {
				result["message"] = "维护执行器暂不可用"
			} else {
				result["available"] = true
				result["managed_update"] = true
				platform.ManagedUpdate = true
				platform.Instructions = "Docker 维护执行器已连接，可在面板更新或重启服务。"
				result["runtime"] = platform
				result["agent"] = state
				result["message"] = "Docker 维护执行器已连接"
			}
		}
		if r.URL.Query().Get("check") == "1" {
			latest, err := a.updates.check(r.Context())
			result["latest_version"] = latest
			hasUpdate := err == nil && newerMaintenanceVersion(current, latest)
			result["has_update"] = hasUpdate
			if err == nil {
				assetVersion := current
				if hasUpdate {
					assetVersion = latest
				}
				platform = describeMaintenanceRuntime(runtime.GOOS, runtime.GOARCH, assetVersion, dockerRuntime, result["managed_update"] == true)
				result["runtime"] = platform
				result["download_available"] = platform.DownloadAvailable
			}
			if err != nil {
				result["check_error"] = "检查版本失败，未执行更新。请检查 YYB 服务所在容器/设备的 GitHub 网络出口，30 秒后重试：" + err.Error()
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
