package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

type daidaiDriver struct {
	baseURL     string
	appKey      string
	appSecret   string
	httpClient  *http.Client
	mu          sync.Mutex
	token       string
	tokenExpiry time.Time
}

func newDaidaiDriver(baseURL, appKey, appSecret string, timeout time.Duration) *daidaiDriver {
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	return &daidaiDriver{
		baseURL:    strings.TrimRight(strings.TrimSpace(baseURL), "/"),
		appKey:     strings.TrimSpace(appKey),
		appSecret:  strings.TrimSpace(appSecret),
		httpClient: &http.Client{Timeout: timeout},
	}
}

func (d *daidaiDriver) PanelType() string {
	return PanelTypeDaidai
}

func (d *daidaiDriver) Status(ctx context.Context) error {
	_, err := d.authenticate(ctx)
	return err
}

func (d *daidaiDriver) authenticate(ctx context.Context) (string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.baseURL == "" || d.appKey == "" || d.appSecret == "" {
		return "", fmt.Errorf("呆呆面板 OpenAPI 未配置")
	}
	if d.token != "" && time.Now().Before(d.tokenExpiry) {
		return d.token, nil
	}

	authReqBody, _ := json.Marshal(map[string]string{
		"app_key":    d.appKey,
		"app_secret": d.appSecret,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, d.baseURL+"/api/v1/open-api/token", bytes.NewReader(authReqBody))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := d.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("连接呆呆面板失败: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		var errResp struct {
			Error   string `json:"error"`
			Message string `json:"message"`
		}
		_ = json.Unmarshal(body, &errResp)
		errMsg := errResp.Error
		if errMsg == "" {
			errMsg = errResp.Message
		}
		if errMsg != "" {
			return "", fmt.Errorf("呆呆面板鉴权失败 (HTTP %d): %s", resp.StatusCode, errMsg)
		}
		return "", fmt.Errorf("呆呆面板鉴权返回 HTTP %d", resp.StatusCode)
	}

	var daidaiTokenResp struct {
		Code int `json:"code"`
		Data struct {
			AccessToken string `json:"access_token"`
			TokenType   string `json:"token_type"`
			ExpiresIn   int64  `json:"expires_in"`
		} `json:"data"`
		AccessToken string `json:"access_token"`
	}
	if err := json.Unmarshal(body, &daidaiTokenResp); err != nil {
		return "", fmt.Errorf("解析呆呆面板鉴权响应失败: %w", err)
	}
	token := daidaiTokenResp.Data.AccessToken
	if token == "" {
		token = daidaiTokenResp.AccessToken
	}
	if token == "" {
		return "", fmt.Errorf("呆呆面板鉴权未返回 access_token")
	}
	expiresIn := daidaiTokenResp.Data.ExpiresIn
	if expiresIn <= 0 {
		expiresIn = 86400
	}
	ttl := time.Duration(expiresIn) * time.Second
	d.token = token
	d.tokenExpiry = time.Now().Add(ttl - 5*time.Minute)
	return d.token, nil
}

func (d *daidaiDriver) request(ctx context.Context, method, path string, body any, out any) error {
	token, err := d.authenticate(ctx)
	if err != nil {
		return err
	}
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, d.baseURL+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := d.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	rawBody, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("呆呆面板 HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(rawBody)))
	}
	if out == nil {
		return nil
	}
	var envelope struct {
		Msg  string          `json:"message"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(rawBody, &envelope); err == nil && len(envelope.Data) > 0 && string(envelope.Data) != "null" {
		return json.Unmarshal(envelope.Data, out)
	}
	return json.Unmarshal(rawBody, out)
}

func (d *daidaiDriver) ListEnvs(ctx context.Context, searchValue string) ([]qingLongEnv, error) {
	path := "/api/v1/envs?" + url.Values{"keyword": {searchValue}, "all": {"1"}}.Encode()
	var raw json.RawMessage
	if err := d.request(ctx, http.MethodGet, path, nil, &raw); err != nil {
		return nil, err
	}
	if len(raw) == 0 || string(raw) == "null" {
		return []qingLongEnv{}, nil
	}
	var list []qingLongEnv
	if err := json.Unmarshal(raw, &list); err == nil {
		return list, nil
	}
	var page struct {
		Data []qingLongEnv `json:"data"`
		List []qingLongEnv `json:"list"`
	}
	if err := json.Unmarshal(raw, &page); err == nil {
		if page.Data != nil {
			return page.Data, nil
		}
		return page.List, nil
	}
	var errList []qingLongEnv
	if err := json.Unmarshal(raw, &errList); err != nil {
		return nil, fmt.Errorf("解析呆呆面板环境变量失败: %w", err)
	}
	return []qingLongEnv{}, nil
}

func (d *daidaiDriver) UpsertEnv(ctx context.Context, name, value, remarks string) error {
	body := map[string]string{
		"name":    name,
		"value":   value,
		"remarks": remarks,
	}
	return d.request(ctx, http.MethodPut, "/api/v1/envs/by-name", body, nil)
}

func (d *daidaiDriver) UpdateEnv(ctx context.Context, id int64, name, value, remarks string) error {
	if id <= 0 {
		return fmt.Errorf("呆呆面板环境变量 ID 无效")
	}
	body := map[string]string{"name": name, "value": value, "remarks": remarks}
	return d.request(ctx, http.MethodPut, fmt.Sprintf("/api/v1/envs/%d", id), body, nil)
}

func (d *daidaiDriver) UpdateEnvEntry(ctx context.Context, env qingLongEnv, newValue string) error {
	return d.UpdateEnv(ctx, env.ID, env.Name, newValue, env.Remarks)
}

func (d *daidaiDriver) DeleteEnvs(ctx context.Context, ids []int64) error {
	for _, id := range ids {
		if err := d.request(ctx, http.MethodDelete, fmt.Sprintf("/api/v1/envs/%d", id), nil, nil); err != nil {
			return err
		}
	}
	return nil
}

func (d *daidaiDriver) SetEnvsEnabled(ctx context.Context, ids []int64, enabled bool) error {
	action := "enable"
	if !enabled {
		action = "disable"
	}
	for _, id := range ids {
		if err := d.request(ctx, http.MethodPut, fmt.Sprintf("/api/v1/envs/%d/%s", id, action), nil, nil); err != nil {
			return err
		}
	}
	return nil
}

func (d *daidaiDriver) SetNamedEnvsEnabled(ctx context.Context, names []string, enabled bool) error {
	if len(names) == 0 {
		return nil
	}
	nameMap := make(map[string]bool, len(names))
	for _, n := range names {
		nameMap[n] = true
	}
	envs, err := d.ListEnvs(ctx, "")
	if err != nil {
		return err
	}
	var ids []int64
	for _, env := range envs {
		if nameMap[env.Name] {
			ids = append(ids, env.ID)
		}
	}
	if len(ids) == 0 {
		return nil
	}
	return d.SetEnvsEnabled(ctx, ids, enabled)
}

func (d *daidaiDriver) ListCrons(ctx context.Context, search string) ([]qingLongCron, error) {
	path := "/api/v1/tasks?" + url.Values{"keyword": {search}, "all": {"1"}}.Encode()
	var raw json.RawMessage
	if err := d.request(ctx, http.MethodGet, path, nil, &raw); err != nil {
		return nil, err
	}
	if len(raw) == 0 || string(raw) == "null" {
		return []qingLongCron{}, nil
	}
	var out []qingLongCron
	if err := json.Unmarshal(raw, &out); err == nil {
		return out, nil
	}
	var page struct {
		Data []qingLongCron `json:"data"`
		List []qingLongCron `json:"list"`
	}
	if err := json.Unmarshal(raw, &page); err == nil {
		if page.Data != nil {
			return page.Data, nil
		}
		return page.List, nil
	}
	var errOut []qingLongCron
	if err := json.Unmarshal(raw, &errOut); err != nil {
		return nil, fmt.Errorf("解析呆呆面板任务列表失败: %w", err)
	}
	return []qingLongCron{}, nil
}

func (d *daidaiDriver) CreateCron(ctx context.Context, name, command, schedule, taskBefore, logName string) (*qingLongCron, error) {
	body := map[string]any{
		"name":            name,
		"command":         command,
		"cron_expression": schedule,
		"task_before":     taskBefore,
		"task_type":       "cron",
	}
	var raw json.RawMessage
	if err := d.request(ctx, http.MethodPost, "/api/v1/tasks", body, &raw); err != nil {
		return nil, err
	}
	var cron qingLongCron
	if err := json.Unmarshal(raw, &cron); err == nil && cron.ID != 0 {
		return &cron, nil
	}
	var list []qingLongCron
	if err := json.Unmarshal(raw, &list); err == nil && len(list) > 0 {
		return &list[0], nil
	}
	return nil, fmt.Errorf("呆呆面板创建任务后未返回任务 ID")
}

func (d *daidaiDriver) UpdateCron(ctx context.Context, id int64, name, command, schedule, taskBefore, logName string) error {
	body := map[string]any{
		"name":            name,
		"command":         command,
		"cron_expression": schedule,
		"task_before":     taskBefore,
	}
	return d.request(ctx, http.MethodPut, fmt.Sprintf("/api/v1/tasks/%d", id), body, nil)
}

func (d *daidaiDriver) SetCronsEnabled(ctx context.Context, ids []int64, enabled bool) error {
	action := "enable"
	if !enabled {
		action = "disable"
	}
	for _, id := range ids {
		if err := d.request(ctx, http.MethodPut, fmt.Sprintf("/api/v1/tasks/%d/%s", id, action), nil, nil); err != nil {
			return err
		}
	}
	return nil
}

func (d *daidaiDriver) RunCrons(ctx context.Context, ids []int64) error {
	for _, id := range ids {
		if err := d.request(ctx, http.MethodPut, fmt.Sprintf("/api/v1/tasks/%d/run", id), nil, nil); err != nil {
			return err
		}
	}
	return nil
}

func (d *daidaiDriver) DeleteCrons(ctx context.Context, ids []int64) error {
	for _, id := range ids {
		if err := d.request(ctx, http.MethodDelete, fmt.Sprintf("/api/v1/tasks/%d", id), nil, nil); err != nil {
			return err
		}
	}
	return nil
}

func (d *daidaiDriver) CronLog(ctx context.Context, id int64) (string, error) {
	var resp struct {
		Data struct {
			Content string `json:"content"`
			Log     string `json:"log"`
		} `json:"data"`
		Content string `json:"content"`
		Log     string `json:"log"`
	}
	if err := d.request(ctx, http.MethodGet, fmt.Sprintf("/api/v1/tasks/%d/latest-log", id), nil, &resp); err != nil {
		return "", err
	}
	content := resp.Data.Content
	if content == "" {
		content = resp.Data.Log
	}
	if content == "" {
		content = resp.Content
	}
	if content == "" {
		content = resp.Log
	}
	return content, nil
}

func (d *daidaiDriver) ListLogs(ctx context.Context) ([]qingLongLogEntry, error) {
	tasks, err := d.ListCrons(ctx, "")
	if err != nil {
		return nil, err
	}
	out := make([]qingLongLogEntry, 0, len(tasks))
	for _, task := range tasks {
		out = append(out, qingLongLogEntry{
			Title: task.Name,
			Key:   strconv.FormatInt(task.ID, 10),
			Type:  "file",
		})
	}
	return out, nil
}

func (d *daidaiDriver) LogDetail(ctx context.Context, dir, filename string) (string, error) {
	key := filename
	if key == "" {
		key = dir
	}
	id, err := strconv.ParseInt(key, 10, 64)
	if err != nil {
		return "", fmt.Errorf("无效的呆呆面板日志 ID: %s", key)
	}
	return d.CronLog(ctx, id)
}
