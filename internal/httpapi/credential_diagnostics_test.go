package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestRecovery409IncludesSafeTypedDiagnostics(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "private-account", time.Now().Add(time.Hour))
	refreshes := 0
	app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
		return "", &protocol.LoginBufferRejectedError{Code: -101, Message: "private-login-buffer"}
	}
	app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		refreshes++
		primary := &protocol.RefreshRejectedError{Code: -109, Operation: "refresh_token", Message: "RC_PARAMS_INVALID"}
		checked := &protocol.RefreshRejectedError{
			Code: -109, Operation: "check_login",
			Message: "CheckWXToken wx refresh return error 42007 | 42007 private-account private-token",
		}
		return protocol.LoginBufferCredentials{}, fmt.Errorf("%w; check login: %w", primary, checked)
	}
	call := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		return nil, errors.Join(&protocol.ShortlinkRejectedError{Code: -14}, &protocol.ManualAuthRejectedError{Code: -10})
	}
	request := func() *httptest.ResponseRecorder {
		out := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/wxapp/getCode", strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wx-test"}`, acc.ID)))
		app.callWXApp(out, req, false, call)
		return out
	}
	out := request()
	if out.Code != http.StatusConflict {
		t.Fatalf("HTTP status = %d", out.Code)
	}
	for _, secret := range []string{"private-account", "private-token", "private-login-buffer"} {
		if strings.Contains(out.Body.String(), secret) {
			t.Fatal("409 exposed private error content")
		}
	}
	var envelope struct {
		Code int `json:"code"`
		Data struct {
			Rescan bool                 `json:"rescan_required"`
			Errors []upstreamDiagnostic `json:"upstream_errors"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	want := []upstreamDiagnostic{{Stage: "shortlink_application", UpstreamCode: -14},
		{Stage: "manual_auth", UpstreamCode: -10},
		{Stage: "login_buffer", UpstreamCode: -101},
		{Stage: "refresh_token", UpstreamCode: -109},
		{Stage: "check_login", UpstreamCode: -109, WeChatCode: 42007}}
	if envelope.Code != 409 || !envelope.Data.Rescan || !reflect.DeepEqual(envelope.Data.Errors, want) {
		t.Fatalf("unexpected diagnostics: %+v", envelope)
	}
	stored, err := app.db.GetAccount(context.Background(), acc.ID)
	if err != nil || stored.Credentials != nil || accountStatus(stored) != "expired" {
		t.Fatal("diagnostic reporting changed terminal cleanup")
	}
	second := request()
	if second.Code != 409 || refreshes != 1 || strings.Contains(second.Body.String(), "42007") {
		t.Fatal("already-expired request probed upstream or invented historical diagnostics")
	}
}

func TestDiagnosticsDoNotInferCodesFromTransportOrIdentifiers(t *testing.T) {
	for _, err := range []error{
		errors.New("proxy failed error 42007 private-token"),
		fmt.Errorf("HTTP failure: %w", context.DeadlineExceeded),
	} {
		if len(credentialDiagnostics(err)) != 0 {
			t.Fatal("transport error reported as a business rejection")
		}
	}
	for _, message := range []string{"rid: 42007", "rid=abc42007def", "error 420070", "error 142007"} {
		got := credentialDiagnostics(&protocol.RefreshRejectedError{Code: -109, Operation: "private-token", Message: message})
		if len(got) != 1 || got[0].WeChatCode != 0 || got[0].Stage != "refresh_token" {
			t.Fatal("diagnostic copied a private operation or inferred an error code from an identifier")
		}
	}
}

func TestRefreshOutIncludesCurrentBusinessCodes(t *testing.T) {
	acc := &store.WechatAccount{ID: 1}
	err := &protocol.RefreshRejectedError{Code: 42002, Operation: "refresh_token"}
	out := refreshOut(acc, "expired", err)
	want := []upstreamDiagnostic{{Stage: "refresh_token", UpstreamCode: 42002}}
	if !reflect.DeepEqual(out["upstream_errors"], want) {
		t.Fatal("refresh response lost typed upstream code")
	}
}
