package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/protocol"
)

// Malformed bodies and nonexistent QR sessions keep these requests entirely
// local, including when this regression is run against the vulnerable code.
func TestProtocolRoutesRequireConfiguredToken(t *testing.T) {
	routes := []struct {
		path       string
		getStatus  int
		postStatus int
	}{
		{"/wx/oauth", 405, 400},
		{"/wxapp/getCode", 405, 400},
		{"/wxapp/getPhoneNumber", 405, 400},
		{"/wxapp/operateWxData", 405, 400},
		{"/wx/code", 405, 400},
		{"/wx/getuserinfo", 400, 400},
		{"/wx/encryptkey", 405, 400},
		{"/wx/getlatestuserkey", 405, 400},
		{"/wx/getphonenumber", 405, 400},
		{"/wx/cloud", 405, 400},
		{"/wx/mpgeta8key", 405, 400},
		{"/wx/appmsgext", 405, 400},
		{"/wx/appmsglike", 405, 400},
		{"/wx/qrcodeauth", 405, 400},
		{"/wx/qrcodeauth/synthetic-missing/image", 404, 405},
		{"/wx/qrcodeauth/synthetic-missing/poll", 404, 405},
		{"/wx/qrcodeauth/synthetic-missing/confirm", 405, 404},
		{"/wx/qrcodeauth/synthetic-missing/cancel", 405, 200},
	}
	for _, driver := range []string{"none", "sqlite"} {
		for _, token := range []string{"", "synthetic-protocol-token"} {
			t.Run(fmt.Sprintf("driver=%s/token=%t", driver, token != ""), func(t *testing.T) {
				app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: driver, ProtocolToken: token})
				if err != nil {
					t.Fatal(err)
				}
				defer app.Close()
				handler := app.Handler()
				for _, route := range routes {
					for _, method := range []string{http.MethodGet, http.MethodPost} {
						for _, authorization := range []string{"", "Bearer wrong", "Bearer synthetic-protocol-token"} {
							t.Run(method+route.path+"/"+authorization, func(t *testing.T) {
								want := route.getStatus
								if method == http.MethodPost {
									want = route.postStatus
								}
								if token != "" && authorization != "Bearer "+token {
									want = http.StatusUnauthorized
								}
								req := httptest.NewRequest(method, route.path, strings.NewReader("{"))
								req.Header.Set("Content-Type", "application/json")
								req.Header.Set("Authorization", authorization)
								recorder := httptest.NewRecorder()
								handler.ServeHTTP(recorder, req)
								if recorder.Code != want || !json.Valid(recorder.Body.Bytes()) {
									t.Fatalf("status=%d, want=%d; body=%s", recorder.Code, want, recorder.Body.String())
								}
							})
						}
					}
				}
			})
		}
	}
}

func TestUserInfoProtocolTokenAndAccountOwnership(t *testing.T) {
	const token = "synthetic-userinfo-token"
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite", ProtocolToken: token})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	sessions := map[string]string{"invalid": "invalid-session"}
	for _, role := range []string{"owner", "outsider", "admin"} {
		userRole := "user"
		if role == "admin" {
			userRole = "admin"
		}
		user, err := app.auth.CreateUser(ctx, role, role, "synthetic-password", userRole)
		if err != nil {
			t.Fatal(err)
		}
		session, _, err := app.auth.CreateSession(ctx, user.ID, "test", "192.0.2.10", time.Hour)
		if err != nil {
			t.Fatal(err)
		}
		sessions[role] = session
		if role == "owner" {
			for i := 1; i <= 2; i++ {
				status := "alive"
				creds := protocol.LoginBufferCredentials{
					OpenID: fmt.Sprintf("synthetic-openid-%d", i), AccessToken: "synthetic-access",
					RefreshToken: "synthetic-refresh", ExpiresAt: time.Now().Add(time.Hour).Unix(),
				}
				account, err := app.db.UpsertAccount(ctx, creds.OpenID, "synthetic-buffer", nil, nil, nil, nil, creds.ToMap(), &status)
				if err != nil {
					t.Fatal(err)
				}
				if account.ID != int64(i) {
					t.Fatalf("unexpected synthetic account ID: %d", account.ID)
				}
				if err := app.auth.ClaimAccount(ctx, account.ID, user.ID); err != nil {
					t.Fatal(err)
				}
			}
		}
	}
	fetchCalls := 0
	app.fetchUserInfo = func(_ context.Context, creds protocol.LoginBufferCredentials) (map[string]any, error) {
		fetchCalls++
		return map[string]any{"nickname": "Synthetic Account", "openid": creds.OpenID}, nil
	}
	handler := app.Handler()
	for _, method := range []string{http.MethodGet, http.MethodPost} {
		for _, ref := range []string{"1", "2", "synthetic-openid-1"} {
			for _, cookie := range []string{"", "invalid", "owner", "outsider", "admin"} {
				for _, authorization := range []string{"", "Bearer wrong", "Bearer " + token} {
					t.Run(method+"/"+ref+"/"+cookie+"/"+authorization, func(t *testing.T) {
						path, body := "/wx/getuserinfo?ref="+ref, ""
						if method == http.MethodPost {
							path, body = "/wx/getuserinfo", `{"ref":"`+ref+`"}`
						}
						req := httptest.NewRequest(method, path, strings.NewReader(body))
						req.Header.Set("Content-Type", "application/json")
						req.Header.Set("Authorization", authorization)
						if cookie != "" {
							req.AddCookie(&http.Cookie{Name: sessionCookie, Value: sessions[cookie]})
						}
						want, wantCalls := http.StatusUnauthorized, 0
						if authorization == "Bearer "+token {
							want, wantCalls = http.StatusOK, 1
							if cookie == "outsider" {
								want, wantCalls = http.StatusNotFound, 0
							}
						}
						before := fetchCalls
						recorder := httptest.NewRecorder()
						handler.ServeHTTP(recorder, req)
						if recorder.Code != want || fetchCalls-before != wantCalls {
							t.Fatalf("status=%d want=%d, upstream calls=%d want=%d; body=%s", recorder.Code, want, fetchCalls-before, wantCalls, recorder.Body.String())
						}
						if want == http.StatusOK && !strings.Contains(recorder.Body.String(), `"nickname":"Synthetic Account"`) {
							t.Fatalf("missing synthetic user info: %s", recorder.Body.String())
						}
					})
				}
			}
		}
	}

	// Browser-managed QR aliases and public account links retain their own
	// authorization boundaries; the protocol token is not a browser session.
	for _, tc := range []struct {
		path, authorization, cookie string
		status                      int
	}{
		{"/qr", "", sessions["owner"], 405},
		{"/qr", "Bearer " + token, "", 401},
		{"/accounts", "Bearer " + token, "", 401},
		{"/account-link/synthetic-missing", "", "", 410},
		{"/health", "", "", 200},
	} {
		req := httptest.NewRequest(http.MethodGet, tc.path, nil)
		req.Header.Set("Authorization", tc.authorization)
		if tc.cookie != "" {
			req.AddCookie(&http.Cookie{Name: sessionCookie, Value: tc.cookie})
		}
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, req)
		if recorder.Code != tc.status {
			t.Errorf("boundary %s: status=%d want=%d; body=%s", tc.path, recorder.Code, tc.status, recorder.Body.String())
		}
	}
}

func TestUnauthorizedProtocolQRDoesNotCancelSession(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), ProtocolToken: "synthetic-qr-token"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	handler := app.Handler()
	for _, method := range []string{http.MethodPost, http.MethodDelete} {
		for _, authorization := range []string{"", "Bearer wrong", "Bearer synthetic-qr-token"} {
			app.qrSessions["synthetic-session"] = &qrLoginSession{}
			req := httptest.NewRequest(method, "/wx/qrcodeauth/synthetic-session/cancel", nil)
			req.Header.Set("Authorization", authorization)
			recorder := httptest.NewRecorder()
			handler.ServeHTTP(recorder, req)
			_, exists := app.qrSessions["synthetic-session"]
			allowed := authorization == "Bearer synthetic-qr-token"
			want := http.StatusUnauthorized
			if allowed {
				want = http.StatusOK
			}
			if recorder.Code != want || exists == allowed {
				t.Fatalf("%s token=%q: status=%d want=%d, session exists=%t", method, authorization, recorder.Code, want, exists)
			}
		}
	}
}
