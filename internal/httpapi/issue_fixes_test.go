package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestReconcileYYBServerStripsSubMembersAndDuplicates(t *testing.T) {
	p1 := int64(1)
	p2 := int64(2)
	accounts := []*store.WechatAccount{
		{ID: 1, OpenID: "owNAX6hDDYXdTestOpenID00001", FleetPrimaryID: &p1},
		{ID: 2, OpenID: "openid-2", FleetPrimaryID: &p2},
		{ID: 3, OpenID: "openid-3"},
		{ID: 14, OpenID: "openid-14", FleetPrimaryID: &p1},
		{ID: 15, OpenID: "openid-15", FleetPrimaryID: &p1},
		{ID: 20, OpenID: "openid-20", FleetPrimaryID: &p2},
	}
	raw := strings.Join([]string{
		"yyb-go:8000@owNAX6hDDYXdTestOpenID00001",
		"yyb-go:8000@3",
		"yyb-go:8000@1",
		"yyb-go:8000@14",
		"yyb-go:8000@15",
		"yyb-go:8000@2",
		"yyb-go:8000@20",
		"external-host:8000@999",
	}, "\n")
	got := reconcileYYBServerEntries(raw, accounts, "", true)
	want := strings.Join([]string{
		"yyb-go:8000@1",
		"yyb-go:8000@3",
		"yyb-go:8000@2",
		"external-host:8000@999",
	}, "\n")
	if got != want {
		t.Fatalf("reconcileYYBServerEntries(dedupe=true):\ngot:\n%s\nwant:\n%s", got, want)
	}
	// Preserve old index-1 vs index-2 migration test when dedupeCrossForm=false.
	gotNoDedupe := reconcileYYBServerEntries("s@owNAX6hDDYXdTestOpenID00001\ns@3\ns@1", accounts, "", false)
	wantNoDedupe := "s@owNAX6hDDYXdTestOpenID00001\ns@3\ns@1"
	if gotNoDedupe != wantNoDedupe {
		t.Fatalf("reconcileYYBServerEntries(dedupe=false):\ngot:\n%s\nwant:\n%s", gotNoDedupe, wantNoDedupe)
	}
}

func TestCircuitBreakerOpensSessionOnRepeatedRiskRejects(t *testing.T) {
	cb := newCircuitBreaker(defaultCircuitConfig())
	now := time.Unix(1_700_000_000, 0)
	cb.now = func() time.Time { return now }

	if scope := cb.record(1, "wx-a", "get_code", circuitRiskReject); scope != "" {
		t.Fatalf("first -12030/-13000 reject opened early: %s", scope)
	}
	if scope := cb.record(1, "wx-b", "get_code", circuitRiskReject); scope != "session" {
		t.Fatalf("second -12030/-13000 reject did not open session breaker: %q", scope)
	}
	if err := cb.check(1, "wx-c", "get_code"); err == nil {
		t.Fatal("session breaker did not hold off subsequent target")
	}
	// Single success clears session breaker immediately.
	cb.record(1, "wx-c", "get_code", circuitOK)
	if err := cb.check(1, "wx-c", "get_code"); err != nil {
		t.Fatalf("success did not clear session breaker: %v", err)
	}
}

func TestFleetDemotesFailedAliveMemberBehindHealthyPeer(t *testing.T) {
	app, a, b := fleetTestSetup(t)
	calls := []int64{}
	call := func(_ context.Context, acc *store.WechatAccount, _ string, _ map[string]any, _ string, _ bool) (map[string]any, error) {
		calls = append(calls, acc.ID)
		if acc.ID == a.ID {
			return nil, &protocol.ManualAuthRejectedError{Code: -10}
		}
		return map[string]any{"code": "ok"}, nil
	}
	// disable fresh ticket / refresh recovery by marking login budget 0
	app.pool.SetLoginBudget(0, time.Minute)

	// 1st call: tries a, fails with ManualAuth -10, fails over to b and demotes a.
	out, err := app.invokeWithFleet(context.Background(), a, "wx1234567890abcdef", nil, call)
	if err != nil || out["code"] != "ok" {
		t.Fatalf("first fleet call did not failover on ManualAuth -10: err=%v out=%v", err, out)
	}
	// Next 3 calls: b must be chosen directly without hitting broken member a.
	calls = nil
	for range 3 {
		out, err = app.invokeWithFleet(context.Background(), a, "wx1234567890abcdef", nil, call)
		if err != nil || out["code"] != "ok" {
			t.Fatalf("subsequent fleet call failed: err=%v out=%v", err, out)
		}
	}
	for _, id := range calls {
		if id != b.ID {
			t.Fatalf("demoted member %d was still preferred over healthy member %d: %v", a.ID, b.ID, calls)
		}
	}
}

func TestPacerCancelledWaitReleasesReservedSlotAndReportsTimeoutStage(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	app.pacer = newAccountPacer("", pacingSettings{MinGap: 5 * time.Second, MaxWait: 20 * time.Second})
	now := time.Now()
	app.pacer.now = func() time.Time { return now }

	acc := insertKeepAliveTestAccount(t, app, "pace-cancel", now.Add(time.Hour))
	// Take the immediate slot at T=0 -> next slot is T+5s.
	if err := app.paceWXApp(context.Background(), acc); err != nil {
		t.Fatal(err)
	}
	// Second caller times out after 20ms -> cancels its T+5s slot.
	shortCtx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	err := app.paceWXApp(shortCtx, acc)
	if !errors.Is(err, context.DeadlineExceeded) || timeoutFailureStage(err) != "pacing_queue" {
		t.Fatalf("expected pacing_queue timeout stage, got err=%v stage=%q", err, timeoutFailureStage(err))
	}
	// Third caller should get the released T+5s slot (wait=5s), NOT T+10s!
	wait, _, err := app.pacer.reserve(acc.ID)
	if err != nil || wait != 5*time.Second {
		t.Fatalf("cancelled caller leaked pacing slot: wait=%v err=%v, want 5s", wait, err)
	}

	w := httptest.NewRecorder()
	writeProtocolCallError(w, &protocol.TimeoutStageError{Stage: "pacing_queue", Err: context.DeadlineExceeded})
	var resp struct {
		Code int            `json:"code"`
		Data map[string]any `json:"data"`
	}
	if json.Unmarshal(w.Body.Bytes(), &resp) != nil || w.Code != http.StatusBadGateway || resp.Data["timeout_stage"] != "pacing_queue" {
		t.Fatalf("unexpected timeout diagnostic payload: %d %s", w.Code, w.Body.String())
	}
}

func TestFleetExponentialDemotionAndReset(t *testing.T) {
	app := &App{}
	now := time.Unix(1_700_000_000, 0)
	members := []*store.WechatAccount{{ID: 14}}

	p1 := app.markFleetMemberFailedAt(14, now)
	if p1.strikes != 1 || p1.until.Sub(now) != 15*time.Minute {
		t.Fatalf("strike 1 mismatch: %+v", p1)
	}
	if !app.activeFleetDemotionsAt(members, now.Add(14*time.Minute))[14] {
		t.Fatal("expected member 14 to stay demoted before 15m")
	}
	// Expiry lifts current active penalty, but must retain strike history.
	if app.activeFleetDemotionsAt(members, now.Add(16*time.Minute))[14] {
		t.Fatal("expected member 14 penalty to lift after 15m")
	}

	now2 := now.Add(16 * time.Minute)
	p2 := app.markFleetMemberFailedAt(14, now2)
	if p2.strikes != 2 || p2.until.Sub(now2) != 30*time.Minute {
		t.Fatalf("strike 2 mismatch (should escalate after expiry): %+v", p2)
	}

	now3 := now2.Add(31 * time.Minute)
	p3 := app.markFleetMemberFailedAt(14, now3)
	if p3.strikes != 3 || p3.until.Sub(now3) != 60*time.Minute {
		t.Fatalf("strike 3 mismatch: %+v", p3)
	}

	// Subsequent consecutive failures plateau cleanly at 6h without overflow.
	cur := now3
	var last fleetPenalty
	for i := 0; i < 8; i++ {
		cur = cur.Add(6*time.Hour + time.Minute)
		last = app.markFleetMemberFailedAt(14, cur)
	}
	if last.until.Sub(cur) != 6*time.Hour {
		t.Fatalf("expected 6h max cooldown cap, got %v", last.until.Sub(cur))
	}

	// Success clears penalty and resets cooldown back to 15m on future failure.
	app.clearFleetMemberPenalty(14)
	pReset := app.markFleetMemberFailedAt(14, cur.Add(time.Minute))
	if pReset.strikes != 1 || pReset.until.Sub(cur.Add(time.Minute)) != 15*time.Minute {
		t.Fatalf("clearFleetMemberPenalty did not reset strike count: %+v", pReset)
	}

	// Idle gap > 24h since last failure also resets strikes to 15m.
	pStaleReset := app.markFleetMemberFailedAt(14, cur.Add(25*time.Hour))
	if pStaleReset.strikes != 1 || pStaleReset.until.Sub(cur.Add(25*time.Hour)) != 15*time.Minute {
		t.Fatalf(">24h quiet window did not reset strike count: %+v", pStaleReset)
	}
}

func TestCodeBurstLimiterAndHTTP429(t *testing.T) {
	lim := newCodeBurstLimiter(3, time.Minute)
	t0 := time.Unix(1_700_000_000, 0)
	for i := 0; i < 3; i++ {
		ok, retry := lim.allow(1, "wx1111111111111111", t0.Add(time.Duration(i)*5*time.Second))
		if !ok || retry != 0 {
			t.Fatalf("call %d unexpectedly rejected: ok=%v retry=%v", i+1, ok, retry)
		}
	}
	ok, retry := lim.allow(1, "wx1111111111111111", t0.Add(20*time.Second))
	if ok || retry != 40*time.Second {
		t.Fatalf("4th call expected reject with 40s retryAfter, got ok=%v retry=%v", ok, retry)
	}
	// Separate (account, AppID) pairs stay independent.
	if okOther, _ := lim.allow(1, "wx2222222222222222", t0.Add(20*time.Second)); !okOther {
		t.Fatal("different AppID should not share burst counter")
	}
	if okOtherAcc, _ := lim.allow(2, "wx1111111111111111", t0.Add(20*time.Second)); !okOtherAcc {
		t.Fatal("different account should not share burst counter")
	}
	// Oldest hit at t0 expires at t0+60s.
	if okAfter, _ := lim.allow(1, "wx1111111111111111", t0.Add(time.Minute)); !okAfter {
		t.Fatal("call after rolling window expiry should succeed")
	}
	// Limit=0 disables limiting.
	lim.configure(0, time.Minute)
	for i := 0; i < 10; i++ {
		if okZero, _ := lim.allow(1, "wx1111111111111111", t0.Add(time.Minute)); !okZero {
			t.Fatalf("limit=0 rejected call %d", i+1)
		}
	}

	// HTTP-level check via /wxapp/getCode + /integration/actions/get-code + /api/runtime-settings.
	app := newKeepAliveTestApp(t)
	defer app.Close()
	app.auth = nil
	app.cfg.IntegrationToken = "test-integ-token"
	acc := insertKeepAliveTestAccount(t, app, "burst-openid", time.Now().Add(time.Hour))
	putRec := httptest.NewRecorder()
	app.handleRuntimeSettings(putRec, httptest.NewRequest(http.MethodPut, "/api/runtime-settings",
		strings.NewReader(`{"code_burst_limit":2,"code_burst_window_ms":30000}`)))
	if putRec.Code != http.StatusOK {
		t.Fatalf("PUT runtime-settings failed: %d %s", putRec.Code, putRec.Body.String())
	}
	// Exhaust quota: use allowCodeBurst directly for first 2 hits so no real WeChat call happens.
	if !app.allowCodeBurst(httptest.NewRecorder(), acc, "wx1111111111111111") ||
		!app.allowCodeBurst(httptest.NewRecorder(), acc, "wx1111111111111111") {
		t.Fatal("first 2 hits within limit=2 should pass")
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/wxapp/getCode",
		strings.NewReader(`{"ref":"burst-openid","app_id":"wx1111111111111111"}`))
	app.handleGetCode(rec, req)
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("expected 429 on 3rd getCode, got %d %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Retry-After") == "" {
		t.Fatal("missing Retry-After header on 429")
	}
	var env struct {
		Code int            `json:"code"`
		Msg  string         `json:"msg"`
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode 429 body failed: %v", err)
	}
	if env.Code != http.StatusTooManyRequests || env.Data["code_burst_limited"] != true ||
		env.Data["limit"] != float64(2) || env.Data["window_seconds"] != float64(30) ||
		env.Data["request_rejected"] != false || env.Data["retry_after"].(float64) < 1 {
		t.Fatalf("unexpected 429 envelope: %+v", env)
	}

	// Integration endpoint also enforces code burst on the same account+AppID.
	intRec := httptest.NewRecorder()
	intReq := httptest.NewRequest(http.MethodPost, "/integration/actions/get-code",
		strings.NewReader(`{"ref":"burst-openid","app_id":"wx1111111111111111"}`))
	intReq.Header.Set("Authorization", "Bearer test-integ-token")
	app.handleIntegrationGetCode(intRec, intReq)
	if intRec.Code != http.StatusTooManyRequests {
		t.Fatalf("expected integration get-code to also enforce 429, got %d %s", intRec.Code, intRec.Body.String())
	}

	// Flush event logger and ensure protocol.burst_limited was emitted only once in this window.
	app.events.Close()
	logs, err := app.db.ListSystemLogs(context.Background(), store.SystemLogFilter{Category: "protocol", Limit: 20})
	if err != nil {
		t.Fatal(err)
	}
	burstEvents := 0
	for _, entry := range logs {
		if entry.Event == "protocol.burst_limited" {
			burstEvents++
		}
	}
	if burstEvents != 1 {
		t.Fatalf("expected 1 burst_limited event per window, got %d", burstEvents)
	}
}

func TestWriteProtocolCallErrorPreservesTimeoutStageOnSocketTimeout(t *testing.T) {
	opTimeout := &net.OpError{Op: "read", Net: "tcp", Err: os.ErrDeadlineExceeded}
	if !protocol.IsTimeoutError(opTimeout) {
		t.Fatal("expected *net.OpError(os.ErrDeadlineExceeded) to be detected as timeout")
	}
	staged := &protocol.TimeoutStageError{Stage: "shortlink_exchange", Err: opTimeout}
	w := httptest.NewRecorder()
	writeProtocolCallError(w, staged)
	if w.Code != http.StatusBadGateway {
		t.Fatalf("unexpected HTTP code %d: %s", w.Code, w.Body.String())
	}
	var env struct {
		Code int            `json:"code"`
		Msg  string         `json:"msg"`
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatal(err)
	}
	if env.Data == nil || env.Data["timeout_stage"] != "shortlink_exchange" || env.Data["request_rejected"] != false {
		t.Fatalf("expected timeout_stage=shortlink_exchange, got %+v", env)
	}
}

func TestCustomPanelEnvNameAndServerAddrLifecycle(t *testing.T) {
	fake, server := newFakeQingLong(t)
	app, handler, ref := newRunsTestApp(t, server.URL)

	// Invalid server_env_name and invalid server_addr must return 400 before hitting panel.
	badEnv := apiRequest(t, handler, http.MethodPut, "/api/qinglong/config", map[string]any{
		"type": "qinglong", "url": server.URL, "client_id": "client-id", "client_secret": "client-secret",
		"server_env_name": "9_INVALID;rm",
	})
	if badEnv.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 on invalid server_env_name, got %d %s", badEnv.Code, badEnv.Body.String())
	}
	badAddr := apiRequest(t, handler, http.MethodPut, "/api/qinglong/config", map[string]any{
		"type": "qinglong", "url": server.URL, "client_id": "client-id", "client_secret": "client-secret",
		"server_addr": "1.2.3.4:8000@evil",
	})
	if badAddr.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 on invalid server_addr, got %d %s", badAddr.Code, badAddr.Body.String())
	}

	// Enable a managed job first using initial defaults (yyb-go:8000 + YYB_SERVER).
	enable := apiRequest(t, handler, http.MethodPut, "/api/qinglong/jobs/enable", map[string]any{
		"ref": ref, "script_key": "MDHY.js", "enabled": true,
	})
	if enable.Code != http.StatusOK {
		t.Fatalf("enable response = %d %s", enable.Code, enable.Body.String())
	}

	// Hot-configure custom external server_addr and custom panel env name.
	put := apiRequest(t, handler, http.MethodPut, "/api/qinglong/config", map[string]any{
		"type": "qinglong", "url": server.URL, "client_id": "client-id", "client_secret": "client-secret",
		"server_addr": "https://yyb.example.com:8443", "server_env_name": "YYB_SERVER_PROD",
	})
	if put.Code != http.StatusOK {
		t.Fatalf("PUT custom config failed: %d %s", put.Code, put.Body.String())
	}

	// GET config exposes active custom values.
	get := apiRequest(t, handler, http.MethodGet, "/api/qinglong/config", nil)
	if get.Code != http.StatusOK || !strings.Contains(get.Body.String(), `"server_addr":"https://yyb.example.com:8443"`) || !strings.Contains(get.Body.String(), `"server_env_name":"YYB_SERVER_PROD"`) {
		t.Fatalf("unexpected GET config: %d %s", get.Code, get.Body.String())
	}

	// Updating config automatically refreshes existing managed cron task_before (fixes runs.go bug!).
	fake.mu.Lock()
	var foundRefreshed bool
	for _, cron := range fake.crons {
		if strings.HasPrefix(cron.Name, "[YYB:") && strings.Contains(cron.TaskBefore, "export YYB_SERVER_PROD='https://yyb.example.com:8443@"+ref+"'") {
			foundRefreshed = true
		}
	}
	fake.mu.Unlock()
	if !foundRefreshed {
		t.Fatalf("managed cron task_before was not refreshed with hot server_addr/env_name: %+v", fake.taskBefores)
	}

	// Sync account writes YYB_SERVER_PROD without creating YYB_SERVER.
	syncRes := apiRequest(t, handler, http.MethodPost, "/api/qinglong/sync", map[string]any{"ref": ref})
	if syncRes.Code != http.StatusOK || !strings.Contains(syncRes.Body.String(), `"name":"YYB_SERVER_PROD"`) {
		t.Fatalf("sync custom env failed: %d %s", syncRes.Code, syncRes.Body.String())
	}
	fake.mu.Lock()
	var customEnv qingLongEnv
	for _, env := range fake.envs {
		if env.Name == "YYB_SERVER" {
			t.Fatalf("default YYB_SERVER should not be written when custom name is set: %+v", env)
		}
		if env.Name == "YYB_SERVER_PROD" {
			customEnv = env
		}
	}
	if customEnv.Value != "https://yyb.example.com:8443@"+ref {
		fake.mu.Unlock()
		t.Fatalf("unexpected YYB_SERVER_PROD value: %+v", customEnv)
	}
	fake.mu.Unlock()

	// Add second account with ID gap so compact can test remapping YYB_SERVER_PROD.
	status := "alive"
	tmpAcc, err := app.db.UpsertAccount(context.Background(), "gap-tmp", "buf", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}
	keepAcc, err := app.db.UpsertAccount(context.Background(), "gap-keep", "buf", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}
	if err := app.db.DeleteAccount(context.Background(), tmpAcc.ID); err != nil {
		t.Fatal(err)
	}
	syncAll := apiRequest(t, handler, http.MethodPost, "/api/qinglong/sync-all", nil)
	if syncAll.Code != http.StatusOK || !strings.Contains(syncAll.Body.String(), `"name":"YYB_SERVER_PROD"`) {
		t.Fatalf("sync-all failed: %d %s", syncAll.Code, syncAll.Body.String())
	}

	compact := apiRequest(t, handler, http.MethodPost, "/accounts/compact", map[string]any{"confirm": true})
	if compact.Code != http.StatusOK || !strings.Contains(compact.Body.String(), "YYB_SERVER_PROD") {
		t.Fatalf("compact failed: %d %s", compact.Code, compact.Body.String())
	}
	fake.mu.Lock()
	var compactedVal string
	for _, env := range fake.envs {
		if env.Name == "YYB_SERVER_PROD" {
			compactedVal = env.Value
		}
	}
	fake.mu.Unlock()
	if strings.Contains(compactedVal, "@"+strconv.FormatInt(keepAcc.ID, 10)) || !strings.Contains(compactedVal, "@2") {
		t.Fatalf("compact did not remap YYB_SERVER_PROD entry: %q", compactedVal)
	}

	// Deleting account cleans up custom env variable entry.
	del := apiRequest(t, handler, http.MethodDelete, "/accounts?ref=2", nil)
	if del.Code != http.StatusOK || !strings.Contains(del.Body.String(), `"env_entries_removed":1`) {
		t.Fatalf("delete cleanup on custom env failed: %d %s", del.Code, del.Body.String())
	}
	fake.mu.Lock()
	for _, env := range fake.envs {
		if env.Name == "YYB_SERVER_PROD" && strings.Contains(env.Value, "@2") {
			fake.mu.Unlock()
			t.Fatalf("deleted account still present in YYB_SERVER_PROD: %+v", env)
		}
	}
	fake.mu.Unlock()

	// Clearing server_addr and server_env_name reverts to default YYB_SERVER + yyb-go:8000.
	resetPut := apiRequest(t, handler, http.MethodPut, "/api/qinglong/config", map[string]any{
		"type": "qinglong", "url": server.URL, "client_id": "client-id", "client_secret": "client-secret",
		"server_addr": "", "server_env_name": "",
	})
	if resetPut.Code != http.StatusOK || !strings.Contains(resetPut.Body.String(), `"server_addr":"yyb-go:8000"`) || !strings.Contains(resetPut.Body.String(), `"server_env_name":"YYB_SERVER"`) {
		t.Fatalf("reset config failed: %d %s", resetPut.Code, resetPut.Body.String())
	}
}

func TestRunsRejectsFleetSubMemberExecution(t *testing.T) {
	_, server := newFakeQingLong(t)
	app, handler, primaryRef := newRunsTestApp(t, server.URL)
	ctx := context.Background()
	status := "alive"
	primaryID, _ := strconv.ParseInt(primaryRef, 10, 64)
	member, err := app.db.UpsertAccount(ctx, "fleet-sub-runs", "buf", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []int64{primaryID, member.ID} {
		if err := app.db.SetAccountFleet(ctx, id, primaryID); err != nil {
			t.Fatal(err)
		}
	}
	memberRef := strconv.FormatInt(member.ID, 10)

	en := apiRequest(t, handler, http.MethodPut, "/api/qinglong/jobs/enable", map[string]any{
		"ref": memberRef, "script_key": "MDHY.js", "enabled": true,
	})
	if en.Code != http.StatusConflict || !strings.Contains(en.Body.String(), `"member_ref_not_allowed":true`) {
		t.Fatalf("expected 409 conflict enabling sub-member job, got %d %s", en.Code, en.Body.String())
	}
	run := apiRequest(t, handler, http.MethodPost, "/api/qinglong/jobs/run", map[string]any{
		"ref": memberRef, "script_key": "MDHY.js",
	})
	if run.Code != http.StatusConflict || !strings.Contains(run.Body.String(), `"member_ref_not_allowed":true`) {
		t.Fatalf("expected 409 conflict running sub-member job, got %d %s", run.Code, run.Body.String())
	}

	// Disabling an unprovisioned/legacy sub-member job stays allowed for cleanup.
	dis := apiRequest(t, handler, http.MethodPut, "/api/qinglong/jobs/enable", map[string]any{
		"ref": memberRef, "script_key": "MDHY.js", "enabled": false,
	})
	if dis.Code != http.StatusOK {
		t.Fatalf("expected disabling sub-member job to succeed for cleanup, got %d %s", dis.Code, dis.Body.String())
	}
}
