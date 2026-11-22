package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
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
		"yyb-go:8000@3",
		"yyb-go:8000@1",
		"yyb-go:8000@2",
		"external-host:8000@999",
	}, "\n")
	if got != want {
		t.Fatalf("reconcileYYBServerEntries:\ngot:\n%s\nwant:\n%s", got, want)
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
