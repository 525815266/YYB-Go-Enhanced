package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestH5CodeRequiresRedirectURI(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "h5-noredirect", time.Now().Add(time.Hour))
	out := httptest.NewRecorder()
	request := withWXOperation(httptest.NewRequest(http.MethodPost, "/wx/h5code",
		strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wx1234567890abcdef"}`, acc.ID))), eventlog.OpH5Code)
	app.callWXApp(out, request, false, app.invokeH5Code)
	if out.Code != http.StatusBadGateway {
		t.Fatalf("HTTP status = %d, want 502: %s", out.Code, out.Body.String())
	}
	if !strings.Contains(out.Body.String(), "redirect_uri") {
		t.Fatalf("response should name the missing parameter: %s", out.Body.String())
	}
}

func TestH5CodeBusinessRejectionLeavesAccountAlone(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "h5-rejected", time.Now().Add(time.Hour))
	refreshes := 0
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		refreshes++
		return protocol.LoginBufferCredentials{}, errors.New("must not refresh for an H5 rejection")
	}
	call := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, &protocol.ErrH5OAuthBusinessRejected{Ret: 10004}
	}
	out := httptest.NewRecorder()
	request := withWXOperation(httptest.NewRequest(http.MethodPost, "/wx/h5code",
		strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wx1234567890abcdef","payload":{"redirect_uri":"https://example.com/login"}}`, acc.ID))), eventlog.OpH5Code)
	app.callWXApp(out, request, false, call)
	if out.Code != http.StatusBadGateway {
		t.Fatalf("HTTP status = %d: %s", out.Code, out.Body.String())
	}
	stored, err := app.db.GetAccount(context.Background(), acc.ID)
	if err != nil || accountStatus(stored) != "alive" {
		t.Fatalf("business rejection changed account status: %v %s", err, accountStatus(stored))
	}
	if refreshes != 0 {
		t.Fatalf("business rejection triggered %d credential refreshes", refreshes)
	}
}

func TestH5CodeRouteRefusesWebsiteAppID(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "h5-website-appid", time.Now().Add(time.Hour))
	out := httptest.NewRecorder()
	request := withWXOperation(httptest.NewRequest(http.MethodPost, "/wx/h5code",
		strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"%s","payload":{"redirect_uri":"https://example.com/login"}}`, acc.ID, websiteAppID))), eventlog.OpH5Code)
	app.callWXApp(out, request, false, app.invokeH5Code)
	if out.Code != http.StatusForbidden {
		t.Fatalf("HTTP status = %d, want 403: %s", out.Code, out.Body.String())
	}
	if !strings.Contains(out.Body.String(), `"appid_denied":true`) {
		t.Fatalf("response missing appid_denied: %s", out.Body.String())
	}
}

func TestH5CodeExplicitDenyStillApplies(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "h5-explicit-deny", time.Now().Add(time.Hour))
	if err := app.appIDs.AddDeny("wx1234567890abcdef", "operator decision"); err != nil {
		t.Fatal(err)
	}
	out := httptest.NewRecorder()
	request := withWXOperation(httptest.NewRequest(http.MethodPost, "/wx/h5code", strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wx1234567890abcdef","payload":{"redirect_uri":"https://example.com/login"}}`, acc.ID))), eventlog.OpH5Code)
	app.callWXApp(out, request, false, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		t.Fatal("explicitly denied H5 request reached the protocol")
		return nil, nil
	})
	if out.Code != http.StatusForbidden {
		t.Fatalf("explicit H5 denial = %d, want 403", out.Code)
	}
}

func TestH5AttemptDoesNotFailOverAcrossFleet(t *testing.T) {
	for _, cause := range []error{&protocol.ShortlinkRejectedError{Code: -14}, &protocol.ShortlinkResponseError{Stage: "session_payload"}} {
		app, primary, _ := fleetTestSetup(t)
		calls := 0
		attempt := &protocol.H5AuthorizationAttemptError{Cause: cause}
		ctx := withWXOperation(httptest.NewRequest(http.MethodPost, "/wx/h5code", nil), eventlog.OpH5Code).Context()
		_, err := app.invokeWithFleet(ctx, primary, "wx1234567890abcdef", nil, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
			calls++
			return nil, attempt
		})
		if !errors.Is(err, attempt) || calls != 1 {
			t.Fatalf("H5 confirmation replayed across channels: calls=%d err=%v", calls, err)
		}
	}
}

// A code request that reached the upstream and came back without a code
// denies that AppID once the outcome repeats on a second account: one
// bursted account failing everything must not deny AppIDs other accounts
// still succeed on, but a dangerous AppID fails everywhere.
func TestDynamicDenyLearnsCodelessAppID(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc1 := insertKeepAliveTestAccount(t, app, "dyn-deny-learn-1", time.Now().Add(time.Hour))
	acc2 := insertKeepAliveTestAccount(t, app, "dyn-deny-learn-2", time.Now().Add(time.Hour))
	call := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, protocol.ErrLoginCodeUnavailable
	}
	requestCode := func(acc *store.WechatAccount) *httptest.ResponseRecorder {
		out := httptest.NewRecorder()
		request := withWXOperation(httptest.NewRequest(http.MethodPost, "/wxapp/getCode",
			strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wxdanger000000001"}`, acc.ID))), eventlog.OpGetCode)
		app.callWXApp(out, request, false, call)
		return out
	}
	if out := requestCode(acc1); out.Code != http.StatusBadGateway {
		t.Fatalf("first account, first failure = %d, want 502: %s", out.Code, out.Body.String())
	}
	if _, denied := app.dynamicAppIDs.check("wxdanger000000001"); denied {
		t.Fatal("one bursted account denied the AppID on its own")
	}
	// A success on another account clears the strike, as it would when the
	// first account was in burst refusal.
	app.dynamicAppIDs.recordSuccess("wxdanger000000001")
	if out := requestCode(acc1); out.Code != http.StatusBadGateway {
		t.Fatalf("after success, failure again = %d, want 502: %s", out.Code, out.Body.String())
	}
	// The failure that reaches the threshold still answers with its own error;
	// only the next request is refused locally.
	if out := requestCode(acc2); out.Code != http.StatusBadGateway {
		t.Fatalf("second account failure = %d, want 502: %s", out.Code, out.Body.String())
	}
	if out := requestCode(acc1); out.Code != http.StatusForbidden {
		t.Fatalf("post-denial request = %d, want 403: %s", out.Code, out.Body.String())
	}
	stored, err := app.db.GetAccount(context.Background(), acc1.ID)
	if err != nil || accountStatus(stored) != "alive" {
		t.Fatalf("learning changed account status: %v %s", err, accountStatus(stored))
	}
	// A failed mini-program login does not establish that an AppID cannot
	// authorize a web callback. Keep the mini-program denial, but let H5 use
	// its own command and business validation.
	out := httptest.NewRecorder()
	request := withWXOperation(httptest.NewRequest(http.MethodPost, "/wx/h5code",
		strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wxdanger000000001","payload":{"redirect_uri":"https://example.com/login"}}`, acc1.ID))), eventlog.OpH5Code)
	h5Calls := 0
	app.callWXApp(out, request, false, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		h5Calls++
		return map[string]any{"code": "synthetic-h5-code"}, nil
	})
	if out.Code != http.StatusOK || h5Calls != 1 {
		t.Fatalf("H5 route for learned AppID = %d, calls=%d, want 200/1: %s", out.Code, h5Calls, out.Body.String())
	}
	if _, denied := app.dynamicAppIDs.check("wxdanger000000001"); !denied {
		t.Fatal("H5 success removed the mini-program denial")
	}
}

// Four total failures deny the AppID even when they all come from one
// account, so a single-account deployment is still protected. The HTTP-level
// circuit breaker would defer such a run first, so this tests the learner
// directly.
func TestDynamicDenyTotalStrikesCap(t *testing.T) {
	learned := newDynamicAppIDDeny(nil, 0, nil, false)
	for i := 1; i <= 3; i++ {
		if learned.recordStrike("wxsingle0000000001", 1) {
			t.Fatalf("failure %d denied the AppID before the cap", i)
		}
		if _, denied := learned.check("wxsingle0000000001"); denied {
			t.Fatalf("failure %d denied the AppID before the cap", i)
		}
	}
	if !learned.recordStrike("wxsingle0000000001", 1) {
		t.Fatal("fourth failure did not deny the AppID")
	}
	if _, denied := learned.check("wxsingle0000000001"); !denied {
		t.Fatal("fourth failure did not deny the AppID")
	}
	// One success in between resets the count, the bursted-account case.
	learned = newDynamicAppIDDeny(nil, 0, nil, false)
	learned.recordStrike("wxsingle0000000002", 1)
	learned.recordStrike("wxsingle0000000002", 1)
	learned.recordSuccess("wxsingle0000000002")
	learned.recordStrike("wxsingle0000000002", 1)
	if _, denied := learned.check("wxsingle0000000002"); denied {
		t.Fatal("success in between did not clear the strikes")
	}
}

// An AppID in YYB_CODE_ALLOW_APPIDS is removed from the learned deny list at
// startup, so the allow list wins over anything persisted before.
func TestDynamicDenyAllowListOverridesLearned(t *testing.T) {
	db, err := store.Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	learned := newDynamicAppIDDeny(db, 0, nil, false)
	if !learned.recordStrike("wxlearnedeny00001", 1) {
		if !learned.recordStrike("wxlearnedeny00001", 2) {
			t.Fatal("two accounts failing did not deny the AppID")
		}
	}
	if _, denied := learned.check("wxlearnedeny00001"); !denied {
		t.Fatal("setup: AppID not learned")
	}
	// A fresh instance on the same database with the allow list forgets it.
	reopened := newDynamicAppIDDeny(db, 0, []string{"wxlearnedeny00001"}, false)
	if _, denied := reopened.check("wxlearnedeny00001"); denied {
		t.Fatal("allow list did not override the learned denial")
	}
	// And the removal survives another open without the allow list.
	again := newDynamicAppIDDeny(db, 0, nil, false)
	if _, denied := again.check("wxlearnedeny00001"); denied {
		t.Fatal("cleared denial came back after reopen")
	}
}

func TestAppIDDenyEndpointListsAndClears(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	app.dynamicAppIDs.recordStrike("wxlearndeny0000001", 1)
	app.dynamicAppIDs.recordStrike("wxlearndeny0000001", 2)
	out := httptest.NewRecorder()
	app.handleAppIDDeny(out, httptest.NewRequest(http.MethodGet, "/api/appid-deny", nil))
	if out.Code != http.StatusOK {
		t.Fatalf("GET = %d: %s", out.Code, out.Body.String())
	}
	var listed struct {
		Data struct {
			Denied map[string]string `json:"learned"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &listed); err != nil {
		t.Fatal(err)
	}
	if _, ok := listed.Data.Denied["wxlearndeny0000001"]; !ok {
		t.Fatalf("learned AppID missing from list: %s", out.Body.String())
	}
	out = httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/appid-deny", strings.NewReader("app_id=wxlearndeny0000001"))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	app.handleAppIDDeny(out, request)
	if out.Code != http.StatusOK {
		t.Fatalf("POST = %d: %s", out.Code, out.Body.String())
	}
	if _, denied := app.dynamicAppIDs.check("wxlearndeny0000001"); denied {
		t.Fatal("cleared AppID still denied")
	}
}
