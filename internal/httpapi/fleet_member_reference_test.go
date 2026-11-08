package httpapi

import (
	"context"
	"fmt"
	"net/http/httptest"
	"strings"
	"testing"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestFleetMemberReferenceNeverReachesProtocol(t *testing.T) {
	app, primary, member := fleetTestSetup(t)
	calls := 0
	call := func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		calls++
		return map[string]any{"code": "synthetic"}, nil
	}
	for _, ref := range []string{fmt.Sprint(member.ID), member.OpenID} {
		w := httptest.NewRecorder()
		app.callWXApp(w, httptest.NewRequest("POST", "/wx/code", strings.NewReader(fmt.Sprintf(`{"ref":%q,"app_id":"wxsynthetic"}`, ref))), false, call)
		if w.Code != 409 || !strings.Contains(w.Body.String(), `"member_ref_not_allowed":true`) || !strings.Contains(w.Body.String(), fmt.Sprintf(`"primary_ref":%d`, primary.ID)) || calls != 0 {
			t.Fatal("direct member invoked protocol", w.Code, w.Body.String())
		}
	}
	// The dispatcher can still call a member on behalf of a primary request.
	usedMember := false
	_, err := app.invokeWithFleet(context.Background(), primary, "wxsynthetic", nil, func(_ context.Context, acc *store.WechatAccount, _ string, _ map[string]any, _ string, _ bool) (map[string]any, error) {
		if acc.ID == primary.ID {
			return nil, &protocol.ShortlinkRejectedError{Code: -14}
		}
		usedMember = true
		return map[string]any{"code": "synthetic"}, nil
	})
	if err != nil || !usedMember {
		t.Fatal("primary can no longer dispatch to member", err)
	}
}

func TestIntegrationRejectsMemberReference(t *testing.T) {
	app, primary, member := fleetTestSetup(t)
	app.cfg.IntegrationToken = "synthetic-integration-token"
	r := httptest.NewRequest("POST", "/integration/actions/get-code", strings.NewReader(fmt.Sprintf(`{"ref":"%d","app_id":"wxsynthetic"}`, member.ID)))
	r.Header.Set("Authorization", "Bearer synthetic-integration-token")
	w := httptest.NewRecorder()
	app.handleIntegrationGetCode(w, r)
	if w.Code != 409 || !strings.Contains(w.Body.String(), `"member_ref_not_allowed":true`) || !strings.Contains(w.Body.String(), fmt.Sprintf(`"primary_ref":%d`, primary.ID)) {
		t.Fatal("integration bypassed primary entry", w.Code, w.Body.String())
	}
}
