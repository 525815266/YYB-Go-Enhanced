package httpapi

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

const websiteAppID = "wx5cd60c5d4817a188"

func TestAppIDPolicyDecisions(t *testing.T) {
	cases := []struct {
		name        string
		deny, allow []string
		appID       string
		denied      bool
	}{
		{"built-in PC Manager website app", nil, nil, websiteAppID, true},
		{"built-in JD passport", nil, nil, "wx2f5d8f9715c59d10", true},
		{"case and spaces do not matter", nil, nil, "  WX5CD60C5D4817A188 ", true},
		{"a mini program passes", nil, nil, "wx141bfb9b73c970a9", false},
		{"an empty AppID is left to the other checks", nil, nil, "", false},
		{"the operator can add a denial", []string{"wxabc0123456789def"}, nil, "wxabc0123456789def", true},
		{"an allowance lifts a built-in denial", nil, []string{websiteAppID}, websiteAppID, false},
		{"an allowance beats an added denial", []string{"wxabc0123456789def"}, []string{"WXABC0123456789DEF"}, "wxabc0123456789def", false},
		{"allowing one AppID keeps the other denial", nil, []string{websiteAppID}, "wx2f5d8f9715c59d10", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := newAppIDPolicy(tc.deny, tc.allow).check(tc.appID)
			if (got != nil) != tc.denied {
				t.Fatalf("check(%q) denied=%v, want %v", tc.appID, got != nil, tc.denied)
			}
		})
	}
	if (*appIDPolicy)(nil).check(websiteAppID) != nil {
		t.Fatal("a nil policy must allow everything")
	}
}

// A refused AppID must never reach the upstream or feed the breaker, and it
// is logged as its own event instead of a protocol failure.
func TestCodeRequestForWebsiteAppIDIsRefusedBeforeUpstream(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-website-appid", time.Now().Add(time.Hour))
	var calls atomic.Int32
	call := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		calls.Add(1)
		return map[string]any{"code": "synthetic-code"}, nil
	}
	code := withWXOperationContext(context.Background(), eventlog.OpGetCode)
	for range 5 { // the breaker would hold off after two real rejections
		var denied *appIDDeniedError
		if _, err := app.invokeWXApp(code, acc, websiteAppID, nil, call); !errors.As(err, &denied) {
			t.Fatalf("website AppID was not refused: %v", err)
		}
	}
	if calls.Load() != 0 {
		t.Fatalf("a refused AppID reached the upstream %d times", calls.Load())
	}
	if err := app.circuit.check(acc.ID, websiteAppID, eventlog.OpGetCode); err != nil {
		t.Fatalf("refusals fed the circuit breaker: %v", err)
	}
	if _, err := app.invokeWXApp(code, acc, "wx141bfb9b73c970a9", nil, call); err != nil || calls.Load() != 1 {
		t.Fatalf("a mini program AppID on the same account was held back: err=%v calls=%d", err, calls.Load())
	}
	phone := withWXOperationContext(context.Background(), eventlog.OpGetPhoneNumber)
	if _, err := app.invokeWXApp(phone, acc, websiteAppID, nil, call); err != nil || calls.Load() != 2 {
		t.Fatalf("the policy must only cover code requests: err=%v calls=%d", err, calls.Load())
	}
	app.events.Close() // flush the asynchronous queue
	entries, err := app.db.ListSystemLogs(context.Background(), store.SystemLogFilter{Limit: 50})
	if err != nil {
		t.Fatal(err)
	}
	var refused int
	for _, entry := range entries {
		if entry.Event == "protocol.appid_denied" {
			refused++
			details := string(entry.Details)
			for _, want := range []string{`"app_id":"` + websiteAppID + `"`, `"operation":"get_code"`} {
				if !strings.Contains(details, want) {
					t.Fatalf("event details missing %s: %s", want, details)
				}
			}
		}
		if entry.Event == "protocol.failed" {
			t.Fatalf("a refusal was logged as a protocol failure: %s", entry.Details)
		}
	}
	if refused != 5 {
		t.Fatalf("expected 5 protocol.appid_denied events, got %d", refused)
	}
}

// Every route that can ask for a code answers a refused AppID with 403 and says
// why, without touching the upstream.
func TestCodeRoutesAnswerWebsiteAppIDWith403(t *testing.T) {
	app, err := NewApp(Config{
		ResourceRoot: t.TempDir(), RequestTimeout: time.Second, AvatarTimeout: time.Second,
		SessionTTL: time.Minute, QRSessionTTL: time.Minute, IntegrationToken: "integration-test-token",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-route-website", time.Now().Add(time.Hour))
	body := `{"ref":"` + strconv.FormatInt(acc.ID, 10) + `","app_id":"` + websiteAppID + `"}`
	for _, path := range []string{"/wxapp/getCode", "/wx/code", "/integration/actions/get-code"} {
		request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Authorization", "Bearer integration-test-token")
		response := httptest.NewRecorder()
		app.Handler().ServeHTTP(response, request)
		if response.Code != http.StatusForbidden {
			t.Fatalf("%s answered %d, want 403: %s", path, response.Code, response.Body.String())
		}
		for _, want := range []string{`"appid_denied":true`, websiteAppID, `"request_rejected":false`} {
			if !strings.Contains(response.Body.String(), want) {
				t.Fatalf("%s response missing %s: %s", path, want, response.Body.String())
			}
		}
	}
}
