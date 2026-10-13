package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeDaidaiPanel struct {
	mu            sync.Mutex
	appKey        string
	appSecret     string
	token         string
	envs          []qingLongEnv
	tasks         []qingLongCron
	deletedIDs    []int64
	runIDs        []int64
	envUpserts    int
	envUpdateIDs  []int64
	failEnvUpdate bool
}

func newFakeDaidaiPanel(t *testing.T) (*fakeDaidaiPanel, *httptest.Server) {
	fake := &fakeDaidaiPanel{
		appKey:    "test-app-key",
		appSecret: "test-app-secret",
		token:     "test-daidai-bearer-token",
		envs:      []qingLongEnv{},
		tasks:     []qingLongCron{},
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/open-api/token" {
			var body struct {
				AppKey    string `json:"app_key"`
				AppSecret string `json:"app_secret"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body.AppKey != fake.appKey || body.AppSecret != fake.appSecret {
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"error":"凭证无效"}`))
				return
			}
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{
				"code": 200,
				"data": {
					"access_token": "test-daidai-bearer-token",
					"token_type": "Bearer",
					"expires_in": 86400
				}
			}`))
			return
		}

		// Auth check for other endpoints
		auth := r.Header.Get("Authorization")
		if auth != "Bearer "+fake.token {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":"未授权"}`))
			return
		}

		fake.mu.Lock()
		defer fake.mu.Unlock()

		switch {
		case r.URL.Path == "/api/v1/envs" && r.Method == http.MethodGet:
			w.WriteHeader(http.StatusOK)
			out, _ := json.Marshal(map[string]any{
				"data":      fake.envs,
				"total":     len(fake.envs),
				"page":      1,
				"page_size": 100,
			})
			_, _ = w.Write(out)

		case r.URL.Path == "/api/v1/envs/by-name" && r.Method == http.MethodPut:
			fake.envUpserts++
			var body struct {
				Name    string `json:"name"`
				Value   string `json:"value"`
				Remarks string `json:"remarks"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			found := false
			for i, e := range fake.envs {
				if e.Name == body.Name && (body.Remarks == "" || e.Remarks == body.Remarks) {
					fake.envs[i].Value = body.Value
					if body.Remarks != "" {
						fake.envs[i].Remarks = body.Remarks
					}
					found = true
					break
				}
			}
			if !found {
				fake.envs = append(fake.envs, qingLongEnv{
					ID:      int64(len(fake.envs) + 1),
					Name:    body.Name,
					Value:   body.Value,
					Remarks: body.Remarks,
					Status:  0,
				})
			}
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"message":"更新成功"}`))

		case strings.HasPrefix(r.URL.Path, "/api/v1/envs/") && r.Method == http.MethodPut:
			id, err := strconv.ParseInt(strings.TrimPrefix(r.URL.Path, "/api/v1/envs/"), 10, 64)
			if err != nil {
				http.NotFound(w, r)
				return
			}
			fake.envUpdateIDs = append(fake.envUpdateIDs, id)
			if fake.failEnvUpdate {
				w.WriteHeader(http.StatusBadGateway)
				return
			}
			var body struct{ Name, Value, Remarks string }
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			for i := range fake.envs {
				if fake.envs[i].ID == id {
					fake.envs[i].Name, fake.envs[i].Value, fake.envs[i].Remarks = body.Name, body.Value, body.Remarks
					_, _ = w.Write([]byte(`{"message":"更新成功"}`))
					return
				}
			}
			w.WriteHeader(http.StatusNotFound)

		case r.URL.Path == "/api/v1/tasks" && r.Method == http.MethodGet:
			w.WriteHeader(http.StatusOK)
			out, _ := json.Marshal(map[string]any{
				"data":      fake.tasks,
				"total":     len(fake.tasks),
				"page":      1,
				"page_size": 100,
			})
			_, _ = w.Write(out)

		case r.URL.Path == "/api/v1/tasks" && r.Method == http.MethodPost:
			var body struct {
				Name           string `json:"name"`
				Command        string `json:"command"`
				CronExpression string `json:"cron_expression"`
				TaskBefore     string `json:"task_before"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			task := qingLongCron{
				ID:             int64(len(fake.tasks) + 100),
				Name:           body.Name,
				Command:        body.Command,
				CronExpression: body.CronExpression,
				Schedule:       body.CronExpression,
				TaskBefore:     body.TaskBefore,
				Status:         1,
			}
			fake.tasks = append(fake.tasks, task)
			w.WriteHeader(http.StatusOK)
			out, _ := json.Marshal(map[string]any{
				"message": "创建成功",
				"data":    task,
			})
			_, _ = w.Write(out)

		default:
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"message":"ok"}`))
		}
	}))

	return fake, server
}

func TestDaidaiPanelClient(t *testing.T) {
	fake, server := newFakeDaidaiPanel(t)
	defer server.Close()

	client := newQingLongClient(PanelTypeDaidai, server.URL, fake.appKey, fake.appSecret, 5*time.Second)

	ctx := context.Background()

	// Test status / authenticate
	if err := client.status(ctx); err != nil {
		t.Fatalf("daidai client status() error = %v", err)
	}

	// Test upsertEnv
	if err := client.upsertEnv(ctx, "YYB_SERVER", "yyb-go:8000@1", "YYB Go 账号列表"); err != nil {
		t.Fatalf("daidai client upsertEnv() error = %v", err)
	}

	envs, err := client.listEnvs(ctx, "YYB_SERVER")
	if err != nil {
		t.Fatalf("daidai client listEnvs() error = %v", err)
	}
	if len(envs) == 0 || envs[0].Name != "YYB_SERVER" {
		t.Fatalf("unexpected envs list: %+v", envs)
	}

	// Test createCron
	cron, err := client.createCron(ctx, "[YYB:1] test task", "task scripts/MDHY.js", "0 8 * * *", "export YYB_SERVER='yyb-go:8000@1'", "log_name")
	if err != nil {
		t.Fatalf("daidai client createCron() error = %v", err)
	}
	if cron.ID == 0 {
		t.Fatalf("expected created task ID > 0, got 0")
	}

	tasks, err := client.listCrons(ctx, "MDHY.js")
	if err != nil {
		t.Fatalf("daidai client listCrons() error = %v", err)
	}
	if len(tasks) == 0 {
		t.Fatalf("expected tasks list not empty")
	}
}
