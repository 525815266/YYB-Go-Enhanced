package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeQingLong struct {
	mu              sync.Mutex
	crons           []qingLongCron
	envs            []qingLongEnv
	nextCron        int64
	nextEnv         int64
	runIDs          []int64
	deletedIDs      []int64
	commands        []string
	taskBefores     []string
	logs            []qingLongLogEntry
	failDeleteCrons bool
	failLogDetail   bool
	cronLogRequests int
	logRequests     int
}

func intPointer(value int) *int { return &value }

func newFakeQingLong(t *testing.T) (*fakeQingLong, *httptest.Server) {
	t.Helper()
	fake := &fakeQingLong{
		nextCron: 100,
		nextEnv:  50,
		envs:     []qingLongEnv{},
		crons: []qingLongCron{
			{ID: 1, Name: "美的会员", Command: "task SuperNaiBA_YYB-GO-Script/MDHY.js", Schedule: "11 8 * * *", Status: 1, IsDisabled: intPointer(1)},
			{ID: 2, Name: "EOOS", Command: "task SuperNaiBA_YYB-GO-Script/eoos/eoos_checkin.py", Schedule: "30 8 * * *", Status: 1, IsDisabled: intPointer(1)},
			{ID: 3, Name: "DT生活", Command: "task 525815266_YYB-Go-Enhanced/scripts/DTSH.py", Schedule: "48 15 * * *", Status: 1, IsDisabled: intPointer(1)},
		},
	}
	server := httptest.NewServer(http.HandlerFunc(fake.serveHTTP))
	t.Cleanup(server.Close)
	return fake, server
}

func (f *fakeQingLong) serveHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	write := func(data any) { _ = json.NewEncoder(w).Encode(map[string]any{"code": 200, "data": data}) }
	if r.URL.Path == "/open/auth/token" {
		write(map[string]any{"token": "fake-token", "expiration": 3600})
		return
	}
	if r.Header.Get("Authorization") != "Bearer fake-token" {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/open/crons":
		write(f.crons)
	case r.Method == http.MethodPost && r.URL.Path == "/open/crons":
		var in qingLongCron
		_ = json.NewDecoder(r.Body).Decode(&in)
		in.ID = f.nextCron
		f.nextCron++
		in.Status = 1
		in.IsDisabled = intPointer(0)
		if in.LogName == "" {
			in.LogName = fmt.Sprintf("managed-%d", in.ID)
		}
		f.crons = append(f.crons, in)
		f.commands = append(f.commands, in.Command)
		f.taskBefores = append(f.taskBefores, in.TaskBefore)
		write(in)
	case r.Method == http.MethodPut && r.URL.Path == "/open/crons":
		var in qingLongCron
		_ = json.NewDecoder(r.Body).Decode(&in)
		for i := range f.crons {
			if f.crons[i].ID == in.ID {
				f.crons[i].Name, f.crons[i].Command, f.crons[i].Schedule, f.crons[i].TaskBefore, f.crons[i].LogName = in.Name, in.Command, in.Schedule, in.TaskBefore, in.LogName
				f.commands = append(f.commands, in.Command)
				f.taskBefores = append(f.taskBefores, in.TaskBefore)
			}
		}
		write(nil)
	case r.Method == http.MethodPut && (r.URL.Path == "/open/crons/enable" || r.URL.Path == "/open/crons/disable"):
		var ids []int64
		_ = json.NewDecoder(r.Body).Decode(&ids)
		disabled := 1
		if strings.HasSuffix(r.URL.Path, "/enable") {
			disabled = 0
		}
		for i := range f.crons {
			for _, id := range ids {
				if f.crons[i].ID == id {
					f.crons[i].IsDisabled = intPointer(disabled)
				}
			}
		}
		write(nil)
	case r.Method == http.MethodPut && r.URL.Path == "/open/crons/run":
		var ids []int64
		_ = json.NewDecoder(r.Body).Decode(&ids)
		f.runIDs = append(f.runIDs, ids...)
		for i := range f.crons {
			for _, id := range ids {
				if f.crons[i].ID != id {
					continue
				}
				filename := "2026-07-31-14-30-00-000.log"
				key := f.crons[i].LogName + "/" + filename
				f.crons[i].LogPath = key
				f.crons[i].LastExecutionTime = 1785480000
				f.logs = append(f.logs, qingLongLogEntry{Title: f.crons[i].LogName, Key: f.crons[i].LogName, Type: "directory", Children: []qingLongLogEntry{{Title: filename, Key: key, Parent: f.crons[i].LogName, Type: "file", Size: 88, CreateTime: 1785480000000}}})
			}
		}
		write(nil)
	case r.Method == http.MethodDelete && r.URL.Path == "/open/crons":
		if f.failDeleteCrons {
			w.WriteHeader(http.StatusBadGateway)
			write(nil)
			return
		}
		var ids []int64
		_ = json.NewDecoder(r.Body).Decode(&ids)
		f.deletedIDs = append(f.deletedIDs, ids...)
		kept := f.crons[:0]
		for _, cron := range f.crons {
			deleted := false
			for _, id := range ids {
				if cron.ID == id {
					deleted = true
					break
				}
			}
			if !deleted {
				kept = append(kept, cron)
			}
		}
		f.crons = kept
		write(nil)
	case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/log"):
		f.cronLogRequests++
		write("fake account log")
	case r.Method == http.MethodGet && r.URL.Path == "/open/logs":
		write(f.logs)
	case r.Method == http.MethodGet && r.URL.Path == "/open/logs/detail":
		f.logRequests++
		if f.failLogDetail {
			w.WriteHeader(http.StatusBadGateway)
			_ = json.NewEncoder(w).Encode(map[string]any{"code": 502, "message": "log detail unavailable"})
			return
		}
		write("fake account history log")
	case r.Method == http.MethodGet && r.URL.Path == "/open/envs":
		write(f.envs)
	case r.Method == http.MethodPost && r.URL.Path == "/open/envs":
		var in []qingLongEnv
		_ = json.NewDecoder(r.Body).Decode(&in)
		for i := range in {
			in[i].ID = f.nextEnv
			f.nextEnv++
			f.envs = append(f.envs, in[i])
		}
		write(in)
	case r.Method == http.MethodPut && r.URL.Path == "/open/envs":
		var in qingLongEnv
		_ = json.NewDecoder(r.Body).Decode(&in)
		for i := range f.envs {
			if f.envs[i].ID == in.ID {
				f.envs[i].Name, f.envs[i].Value, f.envs[i].Remarks = in.Name, in.Value, in.Remarks
			}
		}
		write(nil)
	case r.Method == http.MethodDelete && r.URL.Path == "/open/envs":
		var ids []int64
		_ = json.NewDecoder(r.Body).Decode(&ids)
		kept := f.envs[:0]
		for _, env := range f.envs {
			deleted := false
			for _, id := range ids {
				if env.ID == id {
					deleted = true
					break
				}
			}
			if !deleted {
				kept = append(kept, env)
			}
		}
		f.envs = kept
		write(nil)
	case r.Method == http.MethodPut && (r.URL.Path == "/open/envs/enable" || r.URL.Path == "/open/envs/disable"):
		write(nil)
	default:
		w.WriteHeader(http.StatusNotFound)
		write(nil)
	}
}

func newRunsTestApp(t *testing.T, qlURL string) (*App, http.Handler, string) {
	t.Helper()
	app, err := NewApp(Config{
		ResourceRoot:     t.TempDir(),
		RequestTimeout:   time.Second,
		SessionTTL:       time.Minute,
		QRSessionTTL:     time.Minute,
		QingLongURL:      qlURL,
		QingLongClientID: "client-id",
		QingLongSecret:   "client-secret",
		QingLongServer:   "yyb-go:8000",
		QingLongRepo:     "SuperNaiBA_YYB-GO-Script,525815266_YYB-Go-Enhanced/scripts",
	})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	t.Cleanup(func() { _ = app.Close() })
	status := "alive"
	acc, err := app.db.UpsertAccount(context.Background(), "test-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("seed account: %v", err)
	}
	return app, app.Handler(), fmt.Sprintf("%d", acc.ID)
}

func TestEnhancedRepoScriptsKeepTheirSourcePath(t *testing.T) {
	fake, server := newFakeQingLong(t)
	_, handler, ref := newRunsTestApp(t, server.URL)

	list := apiRequest(t, handler, http.MethodGet, "/api/qinglong/jobs?ref="+url.QueryEscape(ref), nil)
	if list.Code != http.StatusOK || !strings.Contains(list.Body.String(), "DTSH.py") {
		t.Fatalf("enhanced repo script missing: %d %s", list.Code, list.Body.String())
	}

	enable := apiRequest(t, handler, http.MethodPut, "/api/qinglong/jobs/enable", map[string]any{
		"ref": ref, "script_key": "DTSH.py", "enabled": true,
	})
	if enable.Code != http.StatusOK {
		t.Fatalf("enable response = %d %s", enable.Code, enable.Body.String())
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if got := fake.commands[len(fake.commands)-1]; got != "task 525815266_YYB-Go-Enhanced/scripts/DTSH.py" {
		t.Fatalf("managed command = %q", got)
	}
}

func TestQingLongRepoRoots(t *testing.T) {
	repos, err := qingLongRepoRoots(" SuperNaiBA_YYB-GO-Script,525815266_YYB-Go-Enhanced/scripts;SuperNaiBA_YYB-GO-Script ")
	if err != nil {
		t.Fatalf("qingLongRepoRoots() error = %v", err)
	}
	if got := strings.Join(repos, ","); got != "SuperNaiBA_YYB-GO-Script,525815266_YYB-Go-Enhanced/scripts" {
		t.Fatalf("repos = %q", got)
	}
	if _, err := qingLongRepoRoots("../scripts"); err == nil {
		t.Fatal("invalid repository path was accepted")
	}
}

func TestParseScriptKeyFromCronNormalizesWindowsSeparators(t *testing.T) {
	repos := []string{"525815266_YYB-Go-Enhanced/scripts"}
	cron := qingLongCron{
		Name:    "京东签到",
		Command: `task \525815266_YYB-Go-Enhanced\scripts\weile_coin.py`,
	}
	key, repo, ok := parseScriptKeyFromCron(cron, repos)
	if !ok {
		t.Fatal("Windows-style task command was not recognized")
	}
	if key != "weile_coin.py" || repo != repos[0] {
		t.Fatalf("parsed task = %q in repo %q, want weile_coin.py in %q", key, repo, repos[0])
	}
}

func apiRequest(t *testing.T, handler http.Handler, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var raw []byte
	if body != nil {
		raw, _ = json.Marshal(body)
	}
	req := httptest.NewRequest(method, path, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)
	return recorder
}

func TestAccountJobsAreIsolatedDisabledByDefaultAndRunExplicitly(t *testing.T) {
	fake, server := newFakeQingLong(t)
	_, handler, ref := newRunsTestApp(t, server.URL)

	list := apiRequest(t, handler, http.MethodGet, "/api/qinglong/jobs?ref="+url.QueryEscape(ref), nil)
	if list.Code != http.StatusOK || strings.Contains(list.Body.String(), "eoos_checkin") {
		t.Fatalf("initial jobs response = %d %s", list.Code, list.Body.String())
	}
	if !strings.Contains(list.Body.String(), "MDHY.js") {
		t.Fatalf("compatible script missing: %s", list.Body.String())
	}

	enable := apiRequest(t, handler, http.MethodPut, "/api/qinglong/jobs/enable", map[string]any{
		"ref": ref, "script_key": "MDHY.js", "enabled": true,
	})
	if enable.Code != http.StatusOK {
		t.Fatalf("enable response = %d %s", enable.Code, enable.Body.String())
	}
	fake.mu.Lock()
	if len(fake.runIDs) != 0 {
		t.Fatalf("enabling a task unexpectedly ran it: %v", fake.runIDs)
	}
	if len(fake.commands) == 0 {
		t.Fatal("managed task command was not created")
	}
	command := fake.commands[len(fake.commands)-1]
	fake.mu.Unlock()
	if command != "task SuperNaiBA_YYB-GO-Script/MDHY.js" {
		t.Fatalf("managed command = %q", command)
	}
	taskBefore := fake.taskBefores[len(fake.taskBefores)-1]
	if !strings.Contains(taskBefore, "export YYB_SERVER='yyb-go:8000@"+ref+"'") {
		t.Fatalf("managed task_before = %q", taskBefore)
	}

	run := apiRequest(t, handler, http.MethodPost, "/api/qinglong/jobs/run", map[string]any{
		"ref": ref, "script_key": "MDHY.js",
	})
	if run.Code != http.StatusAccepted {
		t.Fatalf("run response = %d %s", run.Code, run.Body.String())
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.runIDs) != 1 {
		t.Fatalf("explicit run IDs = %v", fake.runIDs)
	}
}

func TestAccountJobReclaimsQingLongCronAfterLocalMappingLoss(t *testing.T) {
	fake, server := newFakeQingLong(t)
	fake.mu.Lock()
	fake.crons = append(fake.crons, qingLongCron{
		ID: 77, Name: "[YYB:1] 美的会员",
		Command:  "task SuperNaiBA_YYB-GO-Script/MDHY.js",
		Schedule: "11 8 * * *", LogName: "old-yyb-log", Status: 1, IsDisabled: intPointer(1),
	})
	fake.mu.Unlock()
	app, handler, ref := newRunsTestApp(t, server.URL)
	run := apiRequest(t, handler, http.MethodPost, "/api/qinglong/jobs/run", map[string]any{
		"ref": ref, "script_key": "MDHY.js",
	})
	if run.Code != http.StatusAccepted {
		t.Fatalf("run response = %d %s", run.Code, run.Body.String())
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.crons) != 4 {
		t.Fatalf("recovery created a duplicate cron: %d crons", len(fake.crons))
	}
	if len(fake.runIDs) != 1 || fake.runIDs[0] != 77 {
		t.Fatalf("run IDs = %v, want existing cron 77", fake.runIDs)
	}
	job, err := app.db.GetAccountScriptJob(context.Background(), 1, "MDHY.js")
	if err != nil {
		t.Fatalf("restored account job: %v", err)
	}
	if job.QLCronID != 77 {
		t.Fatalf("restored cron id = %d, want 77", job.QLCronID)
	}
}

func TestAccountJobUsesCurrentQingLongStateFields(t *testing.T) {
	fake, server := newFakeQingLong(t)
	_, handler, ref := newRunsTestApp(t, server.URL)
	_ = apiRequest(t, handler, http.MethodPut, "/api/qinglong/jobs/enable", map[string]any{
		"ref": ref, "script_key": "MDHY.js", "enabled": true,
	})

	fake.mu.Lock()
	fake.crons[0].IsDisabled = intPointer(0)
	for i := range fake.crons {
		if strings.HasPrefix(fake.crons[i].Name, "[YYB:") {
			fake.crons[i].Status = 1
			fake.crons[i].PID = 12345
			fake.crons[i].IsDisabled = intPointer(0)
		}
	}
	fake.mu.Unlock()

	idle := apiRequest(t, handler, http.MethodGet, "/api/qinglong/jobs?ref="+url.QueryEscape(ref), nil)
	if idle.Code != http.StatusOK || !strings.Contains(idle.Body.String(), `"enabled":true`) || !strings.Contains(idle.Body.String(), `"running":false`) {
		t.Fatalf("idle current QingLong job response = %d %s", idle.Code, idle.Body.String())
	}
	if !strings.Contains(idle.Body.String(), `"name":"美的会员"`) || strings.Contains(idle.Body.String(), `"name":"[YYB:`) {
		t.Fatalf("managed account task replaced the source script: %s", idle.Body.String())
	}
	if !strings.Contains(idle.Body.String(), `"global_task_active":true`) {
		t.Fatalf("current QingLong enabled source was not detected: %s", idle.Body.String())
	}

	fake.mu.Lock()
	for i := range fake.crons {
		if strings.HasPrefix(fake.crons[i].Name, "[YYB:") {
			fake.crons[i].Status = 0.5
		}
	}
	fake.mu.Unlock()

	queued := apiRequest(t, handler, http.MethodGet, "/api/qinglong/jobs?ref="+url.QueryEscape(ref), nil)
	if queued.Code != http.StatusOK || !strings.Contains(queued.Body.String(), `"running":true`) {
		t.Fatalf("queued current QingLong job response = %d %s", queued.Code, queued.Body.String())
	}
}

func TestAccountRunHistoryAndLogAreScopedToAccount(t *testing.T) {
	_, server := newFakeQingLong(t)
	app, handler, ref := newRunsTestApp(t, server.URL)
	run := apiRequest(t, handler, http.MethodPost, "/api/qinglong/jobs/run", map[string]any{
		"ref": ref, "script_key": "MDHY.js",
	})
	if run.Code != http.StatusAccepted || !strings.Contains(run.Body.String(), `"account_id":1`) {
		t.Fatalf("run response = %d %s", run.Code, run.Body.String())
	}

	firstLogKey := managedLogName(1, "MDHY.js") + "/2026-07-31-14-30-00-000.log"
	history := apiRequest(t, handler, http.MethodGet, "/api/qinglong/runs?ref="+url.QueryEscape(ref), nil)
	if history.Code != http.StatusOK || !strings.Contains(history.Body.String(), `"script_key":"MDHY.js"`) || !strings.Contains(history.Body.String(), `"log_key":"`+firstLogKey+`"`) {
		t.Fatalf("account history response = %d %s", history.Code, history.Body.String())
	}

	logKey := url.QueryEscape(firstLogKey)
	log := apiRequest(t, handler, http.MethodGet, "/api/qinglong/runs/log?ref="+url.QueryEscape(ref)+"&log_key="+logKey, nil)
	if log.Code != http.StatusOK || !strings.Contains(log.Body.String(), "fake account log") {
		t.Fatalf("account log response = %d %s", log.Code, log.Body.String())
	}

	status := "alive"
	second, err := app.db.UpsertAccount(context.Background(), "second-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("seed second account: %v", err)
	}
	secondRef := fmt.Sprintf("%d", second.ID)
	secondRun := apiRequest(t, handler, http.MethodPost, "/api/qinglong/jobs/run", map[string]any{"ref": secondRef, "script_key": "MDHY.js"})
	if secondRun.Code != http.StatusAccepted {
		t.Fatalf("second account run response = %d %s", secondRun.Code, secondRun.Body.String())
	}
	secondLogKey := managedLogName(second.ID, "MDHY.js") + "/2026-07-31-14-30-00-000.log"
	secondHistory := apiRequest(t, handler, http.MethodGet, "/api/qinglong/runs?ref="+url.QueryEscape(secondRef), nil)
	if secondHistory.Code != http.StatusOK || !strings.Contains(secondHistory.Body.String(), secondLogKey) || strings.Contains(secondHistory.Body.String(), firstLogKey) {
		t.Fatalf("second account history was not isolated = %d %s", secondHistory.Code, secondHistory.Body.String())
	}

	foreign := apiRequest(t, handler, http.MethodGet, "/api/qinglong/runs/log?ref="+url.QueryEscape(ref)+"&log_key="+url.QueryEscape(secondLogKey), nil)
	if foreign.Code != http.StatusNotFound {
		t.Fatalf("foreign account log response = %d %s", foreign.Code, foreign.Body.String())
	}
}

func TestLatestAccountRunUsesOnlyCronLog(t *testing.T) {
	fake, server := newFakeQingLong(t)
	_, handler, ref := newRunsTestApp(t, server.URL)
	run := apiRequest(t, handler, http.MethodPost, "/api/qinglong/jobs/run", map[string]any{
		"ref": ref, "script_key": "MDHY.js",
	})
	if run.Code != http.StatusAccepted {
		t.Fatalf("run response = %d %s", run.Code, run.Body.String())
	}

	logKey := managedLogName(1, "MDHY.js") + "/2026-07-31-14-30-00-000.log"
	log := apiRequest(t, handler, http.MethodGet, "/api/qinglong/runs/log?ref="+url.QueryEscape(ref)+"&log_key="+url.QueryEscape(logKey), nil)
	if log.Code != http.StatusOK || !strings.Contains(log.Body.String(), "fake account log") {
		t.Fatalf("latest account log response = %d %s", log.Code, log.Body.String())
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.cronLogRequests != 1 || fake.logRequests != 0 {
		t.Fatalf("latest log requests: cron=%d detail=%d", fake.cronLogRequests, fake.logRequests)
	}
}

func TestLatestAccountRunLogAcceptsPostJSON(t *testing.T) {
	fake, server := newFakeQingLong(t)
	_, handler, ref := newRunsTestApp(t, server.URL)
	run := apiRequest(t, handler, http.MethodPost, "/api/qinglong/jobs/run", map[string]any{
		"ref": ref, "script_key": "MDHY.js",
	})
	if run.Code != http.StatusAccepted {
		t.Fatalf("run response = %d %s", run.Code, run.Body.String())
	}
	logKey := managedLogName(1, "MDHY.js") + "/2026-07-31-14-30-00-000.log"
	log := apiRequest(t, handler, http.MethodPost, "/api/qinglong/runs/log", map[string]any{
		"ref": ref, "log_key": logKey,
	})
	if log.Code != http.StatusOK || !strings.Contains(log.Body.String(), "fake account log") {
		t.Fatalf("POST account log response = %d %s", log.Code, log.Body.String())
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.cronLogRequests != 1 || fake.logRequests != 0 {
		t.Fatalf("POST latest log requests: cron=%d detail=%d", fake.cronLogRequests, fake.logRequests)
	}
}

func TestHistoricalAccountRunUsesFileDetail(t *testing.T) {
	fake, server := newFakeQingLong(t)
	_, handler, ref := newRunsTestApp(t, server.URL)
	run := apiRequest(t, handler, http.MethodPost, "/api/qinglong/jobs/run", map[string]any{
		"ref": ref, "script_key": "MDHY.js",
	})
	if run.Code != http.StatusAccepted {
		t.Fatalf("run response = %d %s", run.Code, run.Body.String())
	}
	root := managedLogName(1, "MDHY.js")
	newerName := "2026-07-31-14-31-00-000.log"
	fake.mu.Lock()
	fake.logs[0].Children = append(fake.logs[0].Children, qingLongLogEntry{
		Title: newerName, Key: root + "/" + newerName, Parent: root, Type: "file", CreateTime: 1785480060000,
	})
	fake.mu.Unlock()
	olderKey := root + "/2026-07-31-14-30-00-000.log"
	log := apiRequest(t, handler, http.MethodGet, "/api/qinglong/runs/log?ref="+url.QueryEscape(ref)+"&log_key="+url.QueryEscape(olderKey), nil)
	if log.Code != http.StatusOK || !strings.Contains(log.Body.String(), "fake account history log") {
		t.Fatalf("historical account log response = %d %s", log.Code, log.Body.String())
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.cronLogRequests != 0 || fake.logRequests != 1 {
		t.Fatalf("historical log requests: cron=%d detail=%d", fake.cronLogRequests, fake.logRequests)
	}
}

func TestPushSecretStaysInQingLongEnvironment(t *testing.T) {
	fake, server := newFakeQingLong(t)
	_, handler, ref := newRunsTestApp(t, server.URL)
	_ = apiRequest(t, handler, http.MethodPut, "/api/qinglong/jobs/enable", map[string]any{
		"ref": ref, "script_key": "MDHY.js", "enabled": true,
	})
	secret := "SCT_FAKE_SECRET_VALUE"
	save := apiRequest(t, handler, http.MethodPut, "/api/qinglong/push", map[string]any{
		"ref": ref, "channel": "serverchan", "token": secret,
	})
	if save.Code != http.StatusOK {
		t.Fatalf("save push response = %d %s", save.Code, save.Body.String())
	}
	if strings.Contains(save.Body.String(), secret) || strings.Contains(save.Body.String(), "token_env_name") {
		t.Fatalf("push response leaked secret metadata: %s", save.Body.String())
	}
	if !strings.Contains(save.Body.String(), `"token_configured":true`) {
		t.Fatalf("push response does not report configured state: %s", save.Body.String())
	}

	fake.mu.Lock()
	defer fake.mu.Unlock()
	foundSecret := false
	for _, env := range fake.envs {
		if env.Value == secret {
			foundSecret = true
		}
	}
	if !foundSecret {
		t.Fatal("secret was not stored in QingLong environment")
	}
	for _, command := range fake.commands {
		if strings.Contains(command, secret) {
			t.Fatalf("task command leaked secret: %q", command)
		}
	}
	for _, taskBefore := range fake.taskBefores {
		if strings.Contains(taskBefore, secret) {
			t.Fatalf("task_before leaked secret: %q", taskBefore)
		}
	}
	if !strings.Contains(fake.taskBefores[len(fake.taskBefores)-1], "${YYB_RUN_ACCOUNT_"+ref+"_SERVERCHAN_KEY:-}") {
		t.Fatalf("task_before does not reference account environment: %q", fake.taskBefores[len(fake.taskBefores)-1])
	}
}
