package httpapi

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func fleetTestSetup(t *testing.T) (*App, *store.WechatAccount, *store.WechatAccount) {
	t.Helper()
	app := newKeepAliveTestApp(t)
	t.Cleanup(func() { app.Close() })
	a := insertKeepAliveTestAccount(t, app, "fleet-a", time.Now().Add(time.Hour))
	b := insertKeepAliveTestAccount(t, app, "fleet-b", time.Now().Add(time.Hour))
	if err := app.db.SetAccountFleet(context.Background(), a.ID, a.ID); err != nil {
		t.Fatal(err)
	}
	if err := app.db.SetAccountFleet(context.Background(), b.ID, a.ID); err != nil {
		t.Fatal(err)
	}
	// Re-read: dispatch reads the fleet marker from the account object, and
	// production always loads accounts fresh from the store per request.
	a, err := app.db.GetAccount(context.Background(), a.ID)
	if err != nil {
		t.Fatal(err)
	}
	b, err = app.db.GetAccount(context.Background(), b.ID)
	if err != nil {
		t.Fatal(err)
	}
	// Each temporary DB reuses IDs 1/2. Its dispatch history must not come
	// from an unrelated App constructed by a previous test.
	fleetLRU.mu.Lock()
	delete(fleetLRU.last, a.ID)
	delete(fleetLRU.last, b.ID)
	fleetLRU.mu.Unlock()
	return app, a, b
}

func TestFleetDispatchFailsOverToNextMember(t *testing.T) {
	app, a, b := fleetTestSetup(t)
	var mu sync.Mutex
	served := []int64{}
	call := func(_ context.Context, acc *store.WechatAccount, _ string, _ map[string]any, _ string, _ bool) (map[string]any, error) {
		mu.Lock()
		served = append(served, acc.ID)
		mu.Unlock()
		if acc.ID == a.ID {
			return nil, &protocol.ShortlinkRejectedError{Code: -14}
		}
		return map[string]any{"code": "ok"}, nil
	}
	out, err := app.invokeWithFleet(context.Background(), a, "wx1234567890abcdef", nil, call)
	if err != nil {
		t.Fatalf("fleet dispatch failed: %v", err)
	}
	if out["code"] != "ok" || out["fleet_member"] != b.ID {
		t.Fatalf("unexpected result: %v", out)
	}
	if len(served) != 2 || served[0] != a.ID || served[1] != b.ID {
		t.Fatalf("expected failover a→b, served: %v", served)
	}
}

func TestFleetDispatchKeepsBusinessRejectionLocal(t *testing.T) {
	app, a, _ := fleetTestSetup(t)
	var mu sync.Mutex
	served := []int64{}
	refreshes := 0
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		refreshes++
		fresh := creds
		fresh.AccessToken = "access-new"
		fresh.ExpiresAt = time.Now().Add(time.Hour).Unix()
		return fresh, nil
	}
	call := func(_ context.Context, acc *store.WechatAccount, _ string, _ map[string]any, _ string, _ bool) (map[string]any, error) {
		mu.Lock()
		served = append(served, acc.ID)
		mu.Unlock()
		return nil, &protocol.ErrH5OAuthBusinessRejected{Ret: 10003}
	}
	if _, err := app.invokeWithFleet(context.Background(), a, "wx1234567890abcdef", nil, call); err == nil {
		t.Fatal("business rejection must surface")
	}
	mu.Lock()
	defer mu.Unlock()
	// The business answer is request-shaped: member 1 may retry once after its
	// own in-place refresh, but the fleet must not hand it to member 2.
	if len(served) < 1 || len(served) > 2 {
		t.Fatalf("business rejection fanned out across members: %v", served)
	}
	for _, id := range served {
		if id != a.ID {
			t.Fatalf("business rejection reached another member: %v", served)
		}
	}
	if refreshes > 1 {
		t.Fatalf("at most one in-place refresh: %d", refreshes)
	}
}

func TestFleetDispatchRotatesLRU(t *testing.T) {
	app, a, _ := fleetTestSetup(t)
	var mu sync.Mutex
	served := []int64{}
	call := func(_ context.Context, acc *store.WechatAccount, _ string, _ map[string]any, _ string, _ bool) (map[string]any, error) {
		mu.Lock()
		served = append(served, acc.ID)
		mu.Unlock()
		return map[string]any{"code": "ok"}, nil
	}
	for i := 0; i < 4; i++ {
		if _, err := app.invokeWithFleet(context.Background(), a, "wx1234567890abcdef", nil, call); err != nil {
			t.Fatalf("dispatch %d: %v", i, err)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(served) != 4 {
		t.Fatalf("unexpected call count: %v", served)
	}
	// Weak spread property: both members served, and LRU never lets one
	// member absorb three consecutive requests while its peer idles. Exact
	// alternation depends on package-level LRU state left by earlier tests.
	counts := map[int64]int{}
	for _, id := range served {
		counts[id]++
	}
	if len(counts) != 2 {
		t.Fatalf("fleet must spread across both members: %v", served)
	}
	for i := 0; i+2 < len(served); i++ {
		if served[i] == served[i+1] && served[i+1] == served[i+2] {
			t.Fatalf("one member served three in a row: %v", served)
		}
	}
}

func TestFleetAPIBindUnbindStatus(t *testing.T) {
	app, a, b := fleetTestSetup(t)
	// unbind b first: the setup bound it
	if err := app.db.SetAccountFleet(context.Background(), b.ID, 0); err != nil {
		t.Fatal(err)
	}
	// bind through the API with force (test accounts share no real unionid)
	out := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/fleet",
		strings.NewReader(`{"ref":"`+itoa64(b.ID)+`","primary_ref":"`+itoa64(a.ID)+`","force":true}`))
	app.handleFleetAPI(out, request)
	if out.Code != http.StatusOK {
		t.Fatalf("bind HTTP %d: %s", out.Code, out.Body.String())
	}
	status := httptest.NewRecorder()
	app.handleFleetAPI(status, httptest.NewRequest(http.MethodGet, "/api/fleet?ref="+itoa64(b.ID), nil))
	if status.Code != http.StatusOK || !strings.Contains(status.Body.String(), `"fleet":true`) {
		t.Fatalf("status after bind: %d %s", status.Code, status.Body.String())
	}
	// mismatched unionid without force must be refused
	app.oauthH5Invoke = nil
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		return protocol.LoginBufferCredentials{}, nil
	}
	// give the accounts distinct unionids through user_info
	if err := app.db.SetAccountProfile(context.Background(), a.ID, nil, nil, map[string]any{"unionid": "union-aaa"}); err != nil {
		t.Fatal(err)
	}
	if err := app.db.SetAccountProfile(context.Background(), b.ID, nil, nil, map[string]any{"unionid": "union-bbb"}); err != nil {
		t.Fatal(err)
	}
	rebind := httptest.NewRecorder()
	app.handleFleetAPI(rebind, httptest.NewRequest(http.MethodPost, "/api/fleet",
		strings.NewReader(`{"ref":"`+itoa64(b.ID)+`","primary_ref":"`+itoa64(a.ID)+`"}`)))
	if rebind.Code != http.StatusUnprocessableEntity || !strings.Contains(rebind.Body.String(), "unionid") {
		t.Fatalf("unionid mismatch must refuse binding: %d %s", rebind.Code, rebind.Body.String())
	}
}

func itoa64(v int64) string {
	return strconv.FormatInt(v, 10)
}
