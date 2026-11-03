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

// A code request that reached the upstream and came back without a code denies
// that AppID for every account: the next request is refused locally with 403
// and never reaches the upstream again.
func TestDynamicDenyLearnsCodelessAppID(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "dyn-deny-learn", time.Now().Add(time.Hour))
	call := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, protocol.ErrLoginCodeUnavailable
	}
	requestCode := func() *httptest.ResponseRecorder {
		out := httptest.NewRecorder()
		request := withWXOperation(httptest.NewRequest(http.MethodPost, "/wxapp/getCode",
			strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wxdanger000000001"}`, acc.ID))), eventlog.OpGetCode)
		app.callWXApp(out, request, false, call)
		return out
	}
	if out := requestCode(); out.Code != http.StatusBadGateway {
		t.Fatalf("first code-less request = %d, want 502: %s", out.Code, out.Body.String())
	}
	if _, denied := app.dynamicAppIDs.check("wxdanger000000001"); !denied {
		t.Fatal("code-less request did not deny the AppID")
	}
	if out := requestCode(); out.Code != http.StatusForbidden {
		t.Fatalf("second request = %d, want 403: %s", out.Code, out.Body.String())
	}
	stored, err := app.db.GetAccount(context.Background(), acc.ID)
	if err != nil || accountStatus(stored) != "alive" {
		t.Fatalf("learning changed account status: %v %s", err, accountStatus(stored))
	}
	// A learned AppID is refused on the H5 route as well.
	out := httptest.NewRecorder()
	request := withWXOperation(httptest.NewRequest(http.MethodPost, "/wx/h5code",
		strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wxdanger000000001","payload":{"redirect_uri":"https://example.com/login"}}`, acc.ID))), eventlog.OpH5Code)
	app.callWXApp(out, request, false, app.invokeH5Code)
	if out.Code != http.StatusForbidden {
		t.Fatalf("H5 route for learned AppID = %d, want 403: %s", out.Code, out.Body.String())
	}
}

// One code-less outcome can be a transient hiccup: a success clears the strike
// so a healthy AppID only gets denied after threshold consecutive failures.
func TestDynamicDenyThresholdAndStrikeClearing(t *testing.T) {
	app, err := NewApp(Config{
		ResourceRoot: t.TempDir(), RequestTimeout: time.Second, AvatarTimeout: time.Second,
		SessionTTL: time.Minute, QRSessionTTL: time.Minute,
		CodeDynamicDenyThreshold: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "dyn-deny-threshold", time.Now().Add(time.Hour))
	calls := 0
	call := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		calls++
		if calls == 2 || calls == 3 {
			return map[string]any{"code": "issued"}, nil
		}
		return nil, protocol.ErrLoginCodeUnavailable
	}
	requestCode := func() *httptest.ResponseRecorder {
		out := httptest.NewRecorder()
		request := withWXOperation(httptest.NewRequest(http.MethodPost, "/wxapp/getCode",
			strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wxthreshold0000001"}`, acc.ID))), eventlog.OpGetCode)
		app.callWXApp(out, request, false, call)
		return out
	}
	if out := requestCode(); out.Code != http.StatusBadGateway {
		t.Fatalf("call 1 (strike 1) = %d, want 502: %s", out.Code, out.Body.String())
	}
	if _, denied := app.dynamicAppIDs.check("wxthreshold0000001"); denied {
		t.Fatal("single strike denied the AppID despite threshold 2")
	}
	for i := 0; i < 2; i++ {
		if out := requestCode(); out.Code != http.StatusOK {
			t.Fatalf("call %d (success) = %d: %s", calls, out.Code, out.Body.String())
		}
	}
	if out := requestCode(); out.Code != http.StatusBadGateway {
		t.Fatalf("call 4 (strike 1 again) = %d: %s", out.Code, out.Body.String())
	}
	// The strike that reaches the threshold still answers with its own error;
	// only the next request is refused locally.
	if out := requestCode(); out.Code != http.StatusBadGateway {
		t.Fatalf("call 5 (strike 2, denies) = %d: %s", out.Code, out.Body.String())
	}
	if out := requestCode(); out.Code != http.StatusForbidden {
		t.Fatalf("call 6 (denied) = %d, want 403: %s", out.Code, out.Body.String())
	}
}

func TestAppIDDenyEndpointListsAndClears(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	app.dynamicAppIDs.recordStrike("wxlearndeny0000001")
	out := httptest.NewRecorder()
	app.handleAppIDDeny(out, httptest.NewRequest(http.MethodGet, "/api/appid-deny", nil))
	if out.Code != http.StatusOK {
		t.Fatalf("GET = %d: %s", out.Code, out.Body.String())
	}
	var listed struct {
		Data struct {
			Denied map[string]string `json:"denied"`
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
