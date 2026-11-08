package httpapi

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"yyb_go/internal/auth"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestCircuitPolicyControlsActualRequestsAndClearsCooldown(t *testing.T) {
	a := newKeepAliveTestApp(t)
	defer a.Close()
	acc := insertKeepAliveTestAccount(t, a, "circuit-policy", time.Now().Add(time.Hour))
	other := insertKeepAliveTestAccount(t, a, "circuit-other", time.Now().Add(time.Hour))
	ctx := context.Background()
	upstream := 0
	reject := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		upstream++
		return nil, &protocol.ShortlinkRejectedError{Code: -14}
	}
	call := func(account *store.WechatAccount) error {
		_, err := a.invokeWXApp(ctx, account, "wxTest", nil, reject)
		return err
	}
	for range 2 {
		_ = call(acc)
	}
	var open *circuitOpenError
	if err := call(acc); !errors.As(err, &open) || upstream != 2 {
		t.Fatalf("expected cooldown, calls=%d err=%v", upstream, err)
	}
	put := func(path string, enabled bool) {
		t.Helper()
		w := apiRequest(t, a.Handler(), "PUT", path, map[string]any{"enabled": enabled})
		if w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	accountURL := fmt.Sprintf("/accounts/circuit-settings?ref=%d", acc.ID)
	put(accountURL, false)
	for range 5 {
		if err := call(acc); !protocol.IsApplicationRequestRejection(err) {
			t.Fatal(err)
		}
	}
	for range 2 {
		_ = call(other)
	}
	if err := call(other); !errors.As(err, &open) {
		t.Fatal("account exception disabled other account", err)
	}
	put("/api/circuit-settings", false)
	put(accountURL, true)
	for range 5 {
		if err := call(acc); !protocol.IsApplicationRequestRejection(err) {
			t.Fatal("account bypassed master off", err)
		}
	}
	put("/api/circuit-settings", true)
	if err := call(acc); !protocol.IsApplicationRequestRejection(err) {
		t.Fatal("old cooldown survived toggle", err)
	}
	_ = call(acc)
	if err := call(acc); !errors.As(err, &open) {
		t.Fatal("reenabled breaker did not hold requests", err)
	}
	fresh, err := a.db.GetAccount(ctx, acc.ID)
	if err != nil || !fresh.SameAuthorization(acc) || accountStatus(fresh) != "alive" {
		t.Fatal("switch changed authorization", err)
	}
}

func TestCircuitSettingsPermissionsAndValidation(t *testing.T) {
	a, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite", DisableCircuitBreaker: true})
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	ctx := context.Background()
	acc, err := a.db.UpsertAccount(ctx, "owned", "buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	initial, err := a.db.CircuitSettings(ctx, acc.ID, !a.cfg.DisableCircuitBreaker)
	if err != nil || initial.GlobalEnabled || initial.Effective || !initial.AccountEnabled {
		t.Fatal("env default ignored", initial, err)
	}
	owner, err := a.auth.CreateUser(ctx, "owner", "Owner", "synthetic-password", "user")
	if err != nil {
		t.Fatal(err)
	}
	if err = a.auth.ClaimAccount(ctx, acc.ID, owner.ID); err != nil {
		t.Fatal(err)
	}
	other := &auth.User{ID: owner.ID + 1, Role: "user"}
	admin := &auth.User{ID: owner.ID + 2, Role: "admin"}
	for _, tc := range []struct {
		user       *auth.User
		path, body string
		want       int
	}{
		{owner, "/api/circuit-settings", `{"enabled":false}`, 403},
		{other, "/accounts/circuit-settings?ref=1", `{"enabled":false}`, 404},
		{owner, "/accounts/circuit-settings?ref=1", `{"enabled":false}`, 200},
		{admin, "/api/circuit-settings", `{"enabled":true}`, 200},
		{admin, "/api/circuit-settings", `{}`, 400},
		{admin, "/api/circuit-settings", `{"enabled":"false"}`, 400},
		{admin, "/api/circuit-settings", `{"enabled":null}`, 400},
	} {
		r := httptest.NewRequest("PUT", tc.path, strings.NewReader(tc.body))
		r = r.WithContext(context.WithValue(ctx, authUserKey, tc.user))
		w := httptest.NewRecorder()
		a.handleCircuitSettings(w, r)
		if w.Code != tc.want {
			t.Fatalf("%s: %d want %d", tc.path, w.Code, tc.want)
		}
	}
	for _, path := range []string{"/api/circuit-settings", "/accounts/circuit-settings?ref=1"} {
		w := apiRequest(t, a.Handler(), http.MethodPut, path, map[string]any{"enabled": false})
		if w.Code != 401 {
			t.Fatal("anonymous policy mutation", path, w.Code)
		}
	}
	s, err := a.db.CircuitSettings(ctx, acc.ID, !a.cfg.DisableCircuitBreaker)
	if err != nil || !s.GlobalEnabled || s.AccountEnabled || s.Effective {
		t.Fatal("master/account precedence", s, err)
	}
}

func TestCircuitPolicyConcurrentReadsAndChanges(t *testing.T) {
	a := newKeepAliveTestApp(t)
	defer a.Close()
	acc := insertKeepAliveTestAccount(t, a, "concurrent-circuit", time.Now().Add(time.Hour))
	var wg sync.WaitGroup
	for worker := range 4 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range 15 {
				if worker == 0 {
					w := apiRequest(t, a.Handler(), "PUT", "/api/circuit-settings", map[string]any{"enabled": i%2 == 0})
					if w.Code != 200 {
						t.Error(w.Code)
					}
				} else {
					a.recordCircuitOutcome(context.Background(), acc, "wxConcurrent", "", &protocol.ShortlinkRejectedError{Code: -14})
					_ = a.circuit.check(acc.ID, "wxConcurrent", "")
				}
			}
		}()
	}
	wg.Wait()
}

func TestDisabledCircuitStillHonorsPacing(t *testing.T) {
	a := newKeepAliveTestApp(t)
	defer a.Close()
	acc := insertKeepAliveTestAccount(t, a, "circuit-pacing", time.Now().Add(time.Hour))
	if err := a.db.SetSetting(context.Background(), store.CircuitEnabledKey, "false"); err != nil {
		t.Fatal(err)
	}
	a.pacer = newAccountPacer("", pacingSettings{MinGap: time.Minute, MaxWait: 0})
	calls := 0
	success := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		calls++
		return map[string]any{"code": "synthetic"}, nil
	}
	if _, err := a.invokeWXApp(context.Background(), acc, "wxPaced", nil, success); err != nil {
		t.Fatal(err)
	}
	_, err := a.invokeWXApp(context.Background(), acc, "wxPaced", nil, success)
	var open *circuitOpenError
	if !errors.As(err, &open) || open.Scope != "pacing" || calls != 1 {
		t.Fatal("master off bypassed independent pacing", calls, err)
	}
}
