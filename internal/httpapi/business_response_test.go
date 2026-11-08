package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http/httptest"
	"testing"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestBusinessResponseDoesNotRefreshOrDenyAppID(t *testing.T) {
	for _, rejection := range []error{&protocol.LoginCodeRejectedError{Stage: "jslogin_business", Code: -12028}, &protocol.LoginCodeRejectedError{Stage: "jslogin_result", Code: -7}, &protocol.ErrH5OAuthBusinessRejected{Ret: -5}, protocol.ErrLoginCodeMalformed} {
		app := newKeepAliveTestApp(t)
		acc := insertKeepAliveTestAccount(t, app, "synthetic-account", time.Now().Add(time.Hour))
		before, _ := json.Marshal(acc.Credentials)
		app.fetchLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (string, error) {
			t.Fatal("fetched ticket after business response")
			return "", nil
		}
		app.refreshCredentials = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
			t.Fatal("refreshed credentials after business response")
			return protocol.LoginBufferCredentials{}, nil
		}
		calls := 0
		ctx := withWXOperationContext(context.Background(), eventlog.OpGetCode)
		for range 5 {
			_, err := app.invokeWXApp(ctx, acc, "wx-business-rejected", nil, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
				calls++
				return nil, rejection
			})
			if !errors.Is(err, rejection) {
				t.Fatalf("lost upstream response: %v", err)
			}
		}
		if calls != 5 {
			t.Fatal("response caused local circuit or AppID deny")
		}
		if _, denied := app.dynamicAppIDs.check("wx-business-rejected"); denied {
			t.Fatal("account refusal globally denied AppID")
		}
		afterAcc, _ := app.db.GetAccount(ctx, acc.ID)
		after, _ := json.Marshal(afterAcc.Credentials)
		if string(before) != string(after) || accountStatus(afterAcc) != "alive" || acc.LoginBuffer != afterAcc.LoginBuffer {
			t.Fatal("business rejection changed account")
		}
		out := httptest.NewRecorder()
		writeProtocolCallError(out, rejection)
		if rejection != protocol.ErrLoginCodeMalformed {
			var response struct {
				Data struct {
					Rejected bool                 `json:"request_rejected"`
					Errors   []upstreamDiagnostic `json:"upstream_errors"`
				} `json:"data"`
			}
			if err := json.Unmarshal(out.Body.Bytes(), &response); err != nil || !response.Data.Rejected || len(response.Data.Errors) != 1 {
				t.Fatalf("missing typed response: %s", out.Body.String())
			}
		}
		if fleetFailover(rejection) {
			t.Fatal("business response fanned out to other accounts")
		}
		app.Close()
	}
}
