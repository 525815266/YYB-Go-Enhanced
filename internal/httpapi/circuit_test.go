package httpapi

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func testBreaker(t *testing.T) (*circuitBreaker, *time.Time) {
	t.Helper()
	now := time.Unix(1_700_000_000, 0)
	b := newCircuitBreaker(defaultCircuitConfig())
	b.now = func() time.Time { return now }
	return b, &now
}

func TestTargetBreakerHoldsOffThenRecovers(t *testing.T) {
	b, now := testBreaker(t)
	// A single rejection is not enough (default threshold is 2).
	if b.record(1, "wxA", "", circuitAppReject) != "" || b.check(1, "wxA", "") != nil {
		t.Fatal("target opened too early")
	}
	if scope := b.record(1, "wxA", "", circuitAppReject); scope != "target" {
		t.Fatalf("second -14 did not open the target: %q", scope)
	}
	var open *circuitOpenError
	if err := b.check(1, "wxA", ""); !errors.As(err, &open) || open.Scope != "target" {
		t.Fatalf("held-off target not reported: %v", err)
	}
	// A different target on the same account is unaffected.
	if err := b.check(1, "wxB", ""); err != nil {
		t.Fatalf("unrelated target held off: %v", err)
	}
	// After the cooldown the target is allowed again.
	*now = now.Add(defaultCircuitConfig().TargetCooldown + time.Second)
	if err := b.check(1, "wxA", ""); err != nil {
		t.Fatalf("target not released after cooldown: %v", err)
	}
	// A success clears accumulated state.
	b.record(1, "wxA", "", circuitAppReject)
	b.record(1, "wxA", "", circuitOK)
	if b.record(1, "wxA", "", circuitAppReject) != "" {
		t.Fatal("success did not reset the target failure count")
	}
}

func TestSessionBreakerOpensOnDistinctRejectedTargets(t *testing.T) {
	// Repeated -14 on one AppID must NOT open the session (that is the target
	// layer); only distinct AppIDs count toward a wholesale lockout.
	single, _ := testBreaker(t)
	for range 6 {
		single.record(2, "wxSame", "", circuitAppReject)
	}
	if err := single.check(2, "wxOther", ""); err != nil {
		t.Fatalf("session opened from a single AppID: %v", err)
	}

	// Three distinct rejected AppIDs open the session on the third.
	b, _ := testBreaker(t)
	if b.record(7, "wx1", "", circuitAppReject) == "session" || b.record(7, "wx2", "", circuitAppReject) == "session" {
		t.Fatal("session opened before the distinct-target threshold")
	}
	if scope := b.record(7, "wx3", "", circuitAppReject); scope != "session" {
		t.Fatalf("third distinct -14 did not open the session: %q", scope)
	}
	var open *circuitOpenError
	if err := b.check(7, "wxBrandNew", ""); !errors.As(err, &open) || open.Scope != "session" {
		t.Fatalf("session hold-off does not cover every target: %v", err)
	}

	// Empty codes never open the session.
	empty, _ := testBreaker(t)
	for _, id := range []string{"a", "b", "c", "d"} {
		empty.record(9, id, "", circuitEmptyCode)
	}
	if err := empty.check(9, "e", ""); err != nil {
		t.Fatalf("empty codes opened the session: %v", err)
	}
}

func TestSessionWindowExpiresDistinctTargets(t *testing.T) {
	b, now := testBreaker(t)
	b.record(3, "wx1", "", circuitAppReject)
	*now = now.Add(defaultCircuitConfig().SessionWindow + time.Second)
	b.record(3, "wx2", "", circuitAppReject)
	b.record(3, "wx3", "", circuitAppReject)
	// wx1 has aged out of the window, so only two distinct targets remain.
	if err := b.check(3, "wx9", ""); err != nil {
		t.Fatalf("stale target still counted toward the session: %v", err)
	}
}

func TestBreakerForgetAndDisabled(t *testing.T) {
	b, _ := testBreaker(t)
	b.record(4, "wxA", "", circuitAppReject)
	b.record(4, "wxA", "", circuitAppReject)
	b.forget(4)
	if err := b.check(4, "wxA", ""); err != nil {
		t.Fatalf("forget did not clear the target: %v", err)
	}
	off := newCircuitBreaker(circuitConfigFor(true))
	for range 10 {
		off.record(5, "wxA", "", circuitAppReject)
		off.record(5, "wxB", "", circuitAppReject)
		off.record(5, "wxC", "", circuitAppReject)
	}
	if err := off.check(5, "wxA", ""); err != nil {
		t.Fatalf("disabled breaker held off a request: %v", err)
	}
}

// A held-off request must be a local 429 that never reaches the pool and never
// changes account status or credentials.
func TestInvokeWXAppShortCircuitsWithoutUpstream(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-circuit", time.Now().Add(time.Hour))
	var calls atomic.Int32
	reject := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		calls.Add(1)
		return nil, &protocol.ShortlinkRejectedError{Code: -14}
	}
	ctx := context.Background()
	// Two -14 open the target; the third call must be short-circuited locally.
	for range 2 {
		if _, err := app.invokeWXApp(ctx, acc, "wxTarget", nil, reject); !protocol.IsApplicationRequestRejection(err) {
			t.Fatalf("expected -14 passthrough, got %v", err)
		}
	}
	before := calls.Load()
	_, err := app.invokeWXApp(ctx, acc, "wxTarget", nil, reject)
	var open *circuitOpenError
	if !errors.As(err, &open) || open.Scope != "target" {
		t.Fatalf("third call was not short-circuited: %v", err)
	}
	if calls.Load() != before {
		t.Fatal("short-circuited request still reached the pool")
	}
	stored, getErr := app.db.GetAccount(ctx, acc.ID)
	if getErr != nil || accountStatus(stored) != "alive" || stored.Credentials == nil {
		t.Fatal("circuit breaker changed account status or credentials")
	}

	rec := httptest.NewRecorder()
	if !writeIfCircuitOpen(rec, err) || rec.Code != http.StatusTooManyRequests || rec.Header().Get("Retry-After") == "" {
		t.Fatalf("circuit error did not map to 429 with Retry-After: %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"circuit_open":"target"`) {
		t.Fatalf("429 body missing circuit marker: %s", rec.Body.String())
	}

	// A fresh scan clears the breaker.
	if _, err := app.storeFromScan(ctx, acc.LoginBuffer, protocol.CredentialsFromMap(acc.Credentials), nil); err != nil {
		t.Fatal(err)
	}
	if err := app.circuit.check(acc.ID, "wxTarget", ""); err != nil {
		t.Fatalf("re-scan did not clear the breaker: %v", err)
	}
}

// The production signature: one account whose whole session is refused on many
// AppIDs must stop reaching the upstream instead of being hammered every run.
func TestWholesaleRejectionStopsUpstreamHammering(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-wholesale", time.Now().Add(time.Hour))
	var calls atomic.Int32
	reject := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		calls.Add(1)
		return nil, &protocol.ShortlinkRejectedError{Code: -14}
	}
	ctx := context.Background()
	targets := []string{"wx1", "wx2", "wx3", "wx4", "wx5", "wx6"}
	for round := range 20 {
		for _, target := range targets {
			_, _ = app.invokeWXApp(ctx, acc, target, nil, reject)
		}
		_ = round
	}
	// Without the breaker this would be 120 upstream calls. The session opens
	// after a few distinct -14 and holds the rest off locally.
	if n := calls.Load(); n >= 40 {
		t.Fatalf("breaker did not curb upstream calls: %d of 120 reached upstream", n)
	}
}

// A phone or operateWxData request the host refuses for its own reasons must
// not hold off wx.login for the same AppID.
func TestRejectedPhoneRequestsDoNotHoldOffCodeRequests(t *testing.T) {
	b, _ := testBreaker(t)
	b.record(1, "wxJD", eventlog.OpGetPhoneNumber, circuitAppReject)
	if scope := b.record(1, "wxJD", eventlog.OpGetPhoneNumber, circuitAppReject); scope != "target" {
		t.Fatalf("second refused phone request did not open its own target: %q", scope)
	}
	if err := b.check(1, "wxJD", eventlog.OpGetPhoneNumber); err == nil {
		t.Fatal("phone target not held off")
	}
	for _, op := range []string{eventlog.OpGetCode, eventlog.OpOperateWXData, ""} {
		if err := b.check(1, "wxJD", op); err != nil {
			t.Fatalf("%q requests were held off by a phone rejection: %v", op, err)
		}
	}
}

// Only wx.login rejections show that the whole session is refused; operate and
// phone rejections feed their own target only.
func TestOnlyCodeRejectionsFeedTheSessionLayer(t *testing.T) {
	b, _ := testBreaker(t)
	for _, id := range []string{"wx1", "wx2", "wx3", "wx4", "wx5"} {
		for _, op := range []string{eventlog.OpOperateWXData, eventlog.OpGetPhoneNumber} {
			if scope := b.record(8, id, op, circuitAppReject); scope == "session" {
				t.Fatalf("%s rejections opened the session", op)
			}
		}
	}
	if err := b.check(8, "wx9", eventlog.OpGetCode); err != nil {
		t.Fatalf("operate and phone rejections held off code requests: %v", err)
	}

	codes, _ := testBreaker(t)
	codes.record(8, "wx1", eventlog.OpGetCode, circuitAppReject)
	codes.record(8, "wx2", eventlog.OpGetCode, circuitAppReject)
	if scope := codes.record(8, "wx3", eventlog.OpGetCode, circuitAppReject); scope != "session" {
		t.Fatalf("three refused code requests did not open the session: %q", scope)
	}
}

func TestTargetRejectionsExpireAfterTheWindow(t *testing.T) {
	b, now := testBreaker(t)
	b.record(1, "wxA", "", circuitAppReject)
	*now = now.Add(defaultCircuitConfig().TargetWindow + time.Second)
	if scope := b.record(1, "wxA", "", circuitAppReject); scope != "" {
		t.Fatalf("a rejection from long ago still counted as consecutive: %q", scope)
	}
	if scope := b.record(1, "wxA", "", circuitAppReject); scope != "target" {
		t.Fatalf("two recent rejections did not open the target: %q", scope)
	}
}

func TestBreakerTargetMapIsBounded(t *testing.T) {
	b, now := testBreaker(t)
	for i := 0; i < maxBreakerTargets+200; i++ {
		b.record(1, fmt.Sprintf("wx%d", i), "", circuitAppReject)
	}
	if n := len(b.targets); n > maxBreakerTargets {
		t.Fatalf("targets grew past the bound: %d", n)
	}
	// A full table makes room instead of leaving the newest target unprotected.
	if _, ok := b.targets[targetKey{1, fmt.Sprintf("wx%d", maxBreakerTargets+199), ""}]; !ok {
		t.Fatal("the newest target was not tracked once the table was full")
	}
	// Idle entries are pruned once they are an hour old, making room again.
	*now = now.Add(2 * time.Hour)
	b.record(1, "wxFresh", "", circuitAppReject)
	if _, ok := b.targets[targetKey{1, "wxFresh", ""}]; !ok {
		t.Fatal("pruning did not make room for a new target")
	}
}

// Events must say which AppID and request class were refused, without storing
// anything outside plain identifier text.
func TestProtocolEventsRecordAppIDAndOperation(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-event-target", time.Now().Add(time.Hour))
	reject := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, &protocol.ShortlinkRejectedError{Code: -14}
	}
	ctx := withWXOperationContext(context.Background(), eventlog.OpOperateWXData)
	payload := map[string]any{"api_name": "getUserInfo", "data": map[string]any{"token": "must-not-be-logged"}}
	if _, err := app.invokeWXApp(ctx, acc, "wx91d27dbf599dff74", payload, reject); !protocol.IsApplicationRequestRejection(err) {
		t.Fatalf("expected -14 passthrough, got %v", err)
	}
	app.events.Close() // flush the asynchronous queue
	entries, err := app.db.ListSystemLogs(context.Background(), store.SystemLogFilter{Limit: 50})
	if err != nil {
		t.Fatal(err)
	}
	var found bool
	for _, entry := range entries {
		if entry.Event != "protocol.failed" {
			continue
		}
		found = true
		details := string(entry.Details)
		for _, want := range []string{`"app_id":"wx91d27dbf599dff74"`, `"operation":"operate_wx_data"`, `"api":"getUserInfo"`} {
			if !strings.Contains(details, want) {
				t.Fatalf("event details missing %s: %s", want, details)
			}
		}
		if strings.Contains(details, "must-not-be-logged") {
			t.Fatalf("event details leaked a payload value: %s", details)
		}
	}
	if !found {
		t.Fatal("no protocol.failed event was recorded")
	}
}

// A refused getPhoneNumber and a healthy getCode on one AppID go through the
// real invoke path independently.
func TestPhoneRejectionsDoNotBlockCodeThroughInvoke(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-phone-code", time.Now().Add(time.Hour))
	reject := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, &protocol.ShortlinkRejectedError{Code: -14}
	}
	var codeCalls atomic.Int32
	okCode := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		codeCalls.Add(1)
		return map[string]any{"code": "synthetic-code"}, nil
	}
	phone := withWXOperationContext(context.Background(), eventlog.OpGetPhoneNumber)
	code := withWXOperationContext(context.Background(), eventlog.OpGetCode)
	for range 3 {
		_, _ = app.invokeWXApp(phone, acc, "wxJD", nil, reject)
	}
	var open *circuitOpenError
	if _, err := app.invokeWXApp(phone, acc, "wxJD", nil, reject); !errors.As(err, &open) {
		t.Fatalf("repeated phone rejections were not held off: %v", err)
	}
	if _, err := app.invokeWXApp(code, acc, "wxJD", nil, okCode); err != nil || codeCalls.Load() != 1 {
		t.Fatalf("code request for the same AppID was blocked by phone rejections: err=%v calls=%d", err, codeCalls.Load())
	}
}

// A table saturated with open breakers fails open for new targets instead of
// dropping an open one or panicking; the open ones keep holding requests off.
func TestBreakerSaturatedWithOpenTargetsFailsOpen(t *testing.T) {
	b, _ := testBreaker(t)
	for i := 0; i < maxBreakerTargets; i++ {
		account := int64(i + 1)
		b.record(account, "wx", "", circuitAppReject)
		b.record(account, "wx", "", circuitAppReject) // opens this target
	}
	if n := len(b.targets); n != maxBreakerTargets {
		t.Fatalf("expected a full table, got %d", n)
	}
	if scope := b.record(999999, "wxNew", "", circuitAppReject); scope != "" {
		t.Fatalf("an untracked target cannot open a breaker: %q", scope)
	}
	if err := b.check(999999, "wxNew", ""); err != nil {
		t.Fatalf("a new target was held off although the table could not track it: %v", err)
	}
	if err := b.check(1, "wx", ""); err == nil {
		t.Fatal("an open target stopped holding requests off when the table filled up")
	}
}

// The integration get-code endpoint must report a local hold-off as 429, like
// the other entry points, so a client backs off instead of retrying a 502.
func TestIntegrationGetCodeReportsALocalHoldOffAs429(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), IntegrationToken: "secret-token", RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-integration-429", time.Now().Add(time.Hour))
	app.circuit.record(acc.ID, "wxHeldOff", eventlog.OpGetCode, circuitAppReject)
	app.circuit.record(acc.ID, "wxHeldOff", eventlog.OpGetCode, circuitAppReject)

	body := fmt.Sprintf(`{"ref":"%d","app_id":"wxHeldOff"}`, acc.ID)
	request := httptest.NewRequest(http.MethodPost, "/integration/actions/get-code", strings.NewReader(body))
	request.Header.Set("Authorization", "Bearer secret-token")
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	app.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusTooManyRequests || recorder.Header().Get("Retry-After") == "" {
		t.Fatalf("held-off integration request: status=%d retry-after=%q body=%s", recorder.Code, recorder.Header().Get("Retry-After"), recorder.Body.String())
	}
}
