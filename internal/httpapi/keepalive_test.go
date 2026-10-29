package httpapi

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestRefreshAccountRenewsDueCredentials(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()

	acc := insertKeepAliveTestAccount(t, app, "openid-due", time.Now().Add(10*time.Minute))
	calls := 0
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		calls++
		creds.AccessToken = "access-new"
		creds.RefreshToken = "refresh-new"
		creds.ExpiresAt = time.Now().Add(2 * time.Hour).Unix()
		return creds, nil
	}

	status, refreshed, err := app.refreshAccount(context.Background(), acc, false)
	if err != nil {
		t.Fatalf("refreshAccount() error = %v", err)
	}
	if status != "alive" || !refreshed || calls != 1 {
		t.Fatalf("refreshAccount() = status %q, refreshed %v, calls %d", status, refreshed, calls)
	}
	updated, err := app.db.GetAccount(context.Background(), acc.ID)
	if err != nil {
		t.Fatalf("GetAccount() error = %v", err)
	}
	if updated.LoginBuffer != "buffer-old" || updated.Credentials["accesstoken"] != "access-new" || updated.Credentials["refreshtoken"] != "refresh-new" {
		t.Fatalf("updated credentials = %#v, login buffer = %q", updated.Credentials, updated.LoginBuffer)
	}
	if observed, ok := updated.Credentials["refresh_token_observed_at"]; ok && protocol.CredentialsFromMap(map[string]any{"refresh_token_observed_at": observed}).RefreshTokenObservedAt != 0 {
		t.Fatal("keepalive refresh must not invent a scan observation timestamp for a legacy account")
	}
}

func TestRefreshingOneLegacyAccountDoesNotBackfillAnother(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()

	first := insertKeepAliveTestAccount(t, app, "openid-legacy-first", time.Now().Add(10*time.Minute))
	second := insertKeepAliveTestAccount(t, app, "openid-legacy-second", time.Now().Add(10*time.Minute))
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		creds.AccessToken = "access-refreshed"
		creds.ExpiresAt = time.Now().Add(2 * time.Hour).Unix()
		return creds, nil
	}

	if _, _, err := app.refreshAccount(context.Background(), first, false); err != nil {
		t.Fatalf("refresh first legacy account: %v", err)
	}
	untouched, err := app.db.GetAccount(context.Background(), second.ID)
	if err != nil {
		t.Fatalf("read second legacy account: %v", err)
	}
	if observed, ok := untouched.Credentials["refresh_token_observed_at"]; ok && protocol.CredentialsFromMap(map[string]any{"refresh_token_observed_at": observed}).RefreshTokenObservedAt != 0 {
		t.Fatal("refreshing one account must not backfill another account's scan timestamp")
	}
}

func TestRefreshAccountSkipsFreshCredentials(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()

	acc := insertKeepAliveTestAccount(t, app, "openid-fresh", time.Now().Add(2*time.Hour))
	calls := 0
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		calls++
		return protocol.LoginBufferCredentials{}, nil
	}

	status, refreshed, err := app.refreshAccount(context.Background(), acc, false)
	if err != nil {
		t.Fatalf("refreshAccount() error = %v", err)
	}
	if status != "alive" || refreshed || calls != 0 {
		t.Fatalf("refreshAccount() = status %q, refreshed %v, calls %d", status, refreshed, calls)
	}
}

func TestProxyAccountRefreshAheadIsIndependent(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-proxy-ahead", time.Now().Add(2*time.Hour))
	if _, err := app.db.UpsertAccountProxySetting(context.Background(), acc.ID, "api", "http", "", "https://proxy.example/get", nil, "", "", "", 300, nil); err != nil {
		t.Fatalf("UpsertAccountProxySetting() error = %v", err)
	}
	ahead, err := app.accountRefreshAhead(context.Background(), acc.ID)
	if err != nil || ahead != 5*time.Minute {
		t.Fatalf("default proxy refresh ahead = %v, %v", ahead, err)
	}
	if _, err := app.db.UpsertAccountProxySetting(context.Background(), acc.ID, "api", "http", "", "https://proxy.example/get", nil, "", "", "", 2700, nil); err != nil {
		t.Fatalf("UpsertAccountProxySetting(custom) error = %v", err)
	}
	ahead, err = app.accountRefreshAhead(context.Background(), acc.ID)
	if err != nil || ahead != 45*time.Minute {
		t.Fatalf("custom proxy refresh ahead = %v, %v", ahead, err)
	}
}

func TestRefreshAccountRetriesAfterFailure(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()

	acc := insertKeepAliveTestAccount(t, app, "openid-retry", time.Now().Add(10*time.Minute))
	calls := 0
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		calls++
		if calls == 1 {
			return protocol.LoginBufferCredentials{}, errors.New("temporary failure")
		}
		creds.AccessToken = "access-recovered"
		creds.ExpiresAt = time.Now().Add(2 * time.Hour).Unix()
		return creds, nil
	}

	status, _, err := app.refreshAccount(context.Background(), acc, false)
	if err == nil || status != "alive" {
		t.Fatalf("first refresh = status %q, error %v", status, err)
	}
	status, refreshed, err := app.refreshAccount(context.Background(), acc, false)
	if err != nil || status != "alive" || !refreshed || calls != 2 {
		t.Fatalf("second refresh = status %q, refreshed %v, calls %d, error %v", status, refreshed, calls, err)
	}
}

func TestKeepAliveSkipsFreshAndBackoffAccounts(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()

	now := time.Now()
	due := insertKeepAliveTestAccount(t, app, "openid-backoff-due", now.Add(10*time.Minute))
	app.setKeepAliveRetry(due.ID, now.Add(time.Minute))
	if !app.keepAliveShouldSkip(context.Background(), due, now) {
		t.Fatal("recently failed due account should be in keepalive backoff")
	}

	fresh := insertKeepAliveTestAccount(t, app, "openid-backoff-fresh", now.Add(2*time.Hour))
	app.setKeepAliveRetry(fresh.ID, now.Add(time.Minute))
	if !app.keepAliveShouldSkip(context.Background(), fresh, now) {
		t.Fatal("fresh account should be skipped before scheduling refresh")
	}

	if app.keepAliveShouldSkip(context.Background(), due, now.Add(2*time.Minute)) {
		t.Fatal("elapsed retry window should not remain in keepalive backoff")
	}
}

func TestManualLivenessCheckDoesNotRotateFreshCredentials(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()

	acc := insertKeepAliveTestAccount(t, app, "openid-manual-fresh", time.Now().Add(2*time.Hour))
	calls := 0
	app.refreshCredentials = func(_ context.Context, _ protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		calls++
		return protocol.LoginBufferCredentials{}, errors.New("should not refresh fresh credentials")
	}

	status, err := app.refreshLiveness(context.Background(), acc)
	if err != nil || status != "alive" || calls != 0 {
		t.Fatalf("refreshLiveness() = status %q, calls %d, error %v", status, calls, err)
	}
}

func TestForcedTransientRefreshFailureDoesNotExpireFreshAccount(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()

	acc := insertKeepAliveTestAccount(t, app, "openid-force-transient", time.Now().Add(2*time.Hour))
	app.refreshCredentials = func(_ context.Context, _ protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		return protocol.LoginBufferCredentials{}, errors.New("proxy connection timed out")
	}

	status, refreshed, err := app.refreshAccount(context.Background(), acc, true)
	if err == nil || refreshed || status != "alive" {
		t.Fatalf("forced refresh = status %q, refreshed %v, error %v", status, refreshed, err)
	}
	updated, getErr := app.db.GetAccount(context.Background(), acc.ID)
	if getErr != nil || accountStatus(updated) != "alive" {
		t.Fatalf("stored account = status %q, error %v", accountStatus(updated), getErr)
	}
}

func TestTransientRefreshFailureAfterAccessExpiryRemainsRetryable(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()

	acc := insertKeepAliveTestAccount(t, app, "openid-expired-access", time.Now().Add(-time.Minute))
	app.refreshCredentials = func(_ context.Context, _ protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		return protocol.LoginBufferCredentials{}, errors.New("proxy API temporarily unavailable")
	}

	status, refreshed, err := app.refreshAccount(context.Background(), acc, false)
	if err == nil || refreshed || status != "unknown" {
		t.Fatalf("expired access refresh = status %q, refreshed %v, error %v", status, refreshed, err)
	}
	updated, getErr := app.db.GetAccount(context.Background(), acc.ID)
	if getErr != nil || accountStatus(updated) != "unknown" {
		t.Fatalf("stored account = status %q, error %v", accountStatus(updated), getErr)
	}
}

func TestDefinitiveRefreshRejectionExpiresAccount(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()

	acc := insertKeepAliveTestAccount(t, app, "openid-refresh-rejected", time.Now().Add(2*time.Hour))
	app.refreshCredentials = func(_ context.Context, _ protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		return protocol.LoginBufferCredentials{}, &protocol.RefreshRejectedError{Code: 401, Message: "refresh token expired"}
	}

	status, refreshed, err := app.refreshAccount(context.Background(), acc, true)
	if err == nil || refreshed || status != "expired" {
		t.Fatalf("rejected refresh = status %q, refreshed %v, error %v", status, refreshed, err)
	}
	updated, getErr := app.db.GetAccount(context.Background(), acc.ID)
	if getErr != nil || accountStatus(updated) != "expired" {
		t.Fatalf("stored account = status %q, error %v", accountStatus(updated), getErr)
	}
}

func TestAmbiguousTokenFailuresRemainRetryable(t *testing.T) {
	now := time.Now()
	for _, failure := range []error{
		errors.New("proxy authentication failed: token expired, please re-login"),
		errors.New("HTTP 502: upstream refresh_token expired"),
		errors.New("decode JSON: invalid token"),
		errors.New("refresh failed: code=-109 msg=42007 refresh_token exception"),
		errors.New("HTTP 502: GetLoginBuffer error [40188] [invalid scope]"),
		&protocol.RefreshRejectedError{Code: -109, Message: "access_token expired"},
		&protocol.RefreshRejectedError{Code: -109, Message: "invalid access token"},
		&protocol.RefreshRejectedError{Code: -109, Message: "token expired"},
		&protocol.LoginBufferRejectedError{Code: -101, Message: "access_token expired"},
	} {
		t.Run(failure.Error(), func(t *testing.T) {
			for _, state := range []struct {
				expiry time.Time
				want   string
			}{
				{now.Add(time.Hour), "alive"},
				{now.Add(-time.Minute), "unknown"},
			} {
				creds := protocol.LoginBufferCredentials{ExpiresAt: state.expiry.Unix()}
				if got := refreshFailureStatus("alive", creds, failure, now); got != state.want {
					t.Fatalf("refreshFailureStatus(%v) = %q, want %q", state.expiry, got, state.want)
				}
			}
		})
	}
}

func TestAccessTokenRejectionCanRecoverWithoutRescan(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	acc := insertKeepAliveTestAccount(t, app, "openid-access-retry", time.Now().Add(-time.Minute))
	calls := 0
	app.refreshCredentials = func(_ context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferCredentials, error) {
		calls++
		if calls == 1 {
			return protocol.LoginBufferCredentials{}, &protocol.RefreshRejectedError{Code: -109, Message: "access_token expired"}
		}
		creds.AccessToken = "access-recovered"
		creds.ExpiresAt = time.Now().Add(2 * time.Hour).Unix()
		return creds, nil
	}
	status, refreshed, err := app.refreshAccount(context.Background(), acc, false)
	if err == nil || refreshed || status != "unknown" {
		t.Fatalf("first refresh = %q, %v, %v; want retryable unknown", status, refreshed, err)
	}
	stored, err := app.db.GetAccount(context.Background(), acc.ID)
	if err != nil || stored.LoginBuffer != acc.LoginBuffer || stored.Credentials["refreshtoken"] != acc.Credentials["refreshtoken"] {
		t.Fatalf("retry must preserve existing credentials: %v", err)
	}
	status, refreshed, err = app.refreshAccount(context.Background(), acc, false)
	if err != nil || !refreshed || status != "alive" || calls != 2 {
		t.Fatalf("second refresh = %q, %v, %v, calls %d", status, refreshed, err, calls)
	}
}

func TestTencent42007ExpiresAccount(t *testing.T) {
	err := &protocol.RefreshRejectedError{Code: -109, Message: "WXRefresh go error, code: 42007, msg access_token and refresh_token exception"}
	if !definitiveCredentialFailure(err) {
		t.Fatal("42007 refresh token rejection should require a rescan")
	}
}

func TestCredentialFailureChecksEveryJoinedCause(t *testing.T) {
	ambiguous := &protocol.RefreshRejectedError{Code: -109, Operation: "refresh_token", Message: "RC_PARAMS_INVALID"}
	rejected := &protocol.RefreshRejectedError{Code: -109, Operation: "check_login", Message: "error 42007 | 42007"}
	for _, failure := range []error{
		fmt.Errorf("%w; check login: %w", ambiguous, rejected),
		fmt.Errorf("recovery: %w", errors.Join(ambiguous, fmt.Errorf("check login: %w", rejected))),
		errors.Join(ambiguous, &protocol.LoginBufferRejectedError{Code: 40188, Message: "invalid scope"}),
	} {
		if !definitiveCredentialFailure(failure) {
			t.Fatalf("explicit rejection hidden by earlier ambiguous failure: %v", failure)
		}
	}
	for _, failure := range []error{
		errors.Join(ambiguous, context.DeadlineExceeded),
		errors.Join(ambiguous, errors.New("proxy error 42007, refresh_token expired")),
		errors.Join(ambiguous, &protocol.RefreshRejectedError{Code: -109, Message: "access_token expired"}),
	} {
		if definitiveCredentialFailure(failure) {
			t.Fatalf("retryable joined failure expired an account: %v", failure)
		}
	}
}

func TestOfficialNumericRefreshRejectionsExpireAccount(t *testing.T) {
	for _, message := range []string{
		"CheckWXToken wx refresh return error 42007 | 42007",
		"CheckWXToken wx refresh return error 42002 | 42002",
		"CheckWXToken wx refresh return error 40030 | 40030",
	} {
		err := &protocol.RefreshRejectedError{Code: -109, Message: message}
		if !definitiveCredentialFailure(err) {
			t.Errorf("known WeChat rejection left account alive: %s", message)
		}
		if definitiveCredentialFailure(errors.New(message)) {
			t.Fatal("untyped transport error expired account")
		}
	}
	for _, message := range []string{"error 142007 | 142007", "error 420070 | 420070", "access_token expired 42001", "RC_PARAMS_INVALID rid=abc42007def", "RC_PARAMS_INVALID rid: 42007"} {
		if definitiveCredentialFailure(&protocol.RefreshRejectedError{Code: -109, Message: message}) {
			t.Errorf("unrecognized rejection expired account: %s", message)
		}
	}
}

func TestTencent40188ExpiresAccount(t *testing.T) {
	err := &protocol.LoginBufferRejectedError{Code: -101, Message: "GetLoginBuffer error [40188] [invalid scope]"}
	if !definitiveCredentialFailure(err) {
		t.Fatal("40188 invalid scope should require a rescan")
	}
}

func TestExplicitCredentialFailuresRemainTerminal(t *testing.T) {
	for _, failure := range []error{
		protocol.ErrMissingRefreshToken,
		&protocol.RefreshRejectedError{Code: 42007, Message: "refresh_token exception"},
		&protocol.LoginBufferRejectedError{Code: 40188, Message: "invalid scope"},
		&protocol.RefreshRejectedError{Code: -109, Message: "refreshToken expired"},
		&protocol.RefreshRejectedError{Code: -109, Message: "refresh-token invalid"},
		&protocol.RefreshRejectedError{Code: -109, Message: "刷新令牌已失效"},
		&protocol.RefreshRejectedError{Code: -109, Message: "please re-login"},
	} {
		if !definitiveCredentialFailure(fmt.Errorf("refresh account: %w", failure)) {
			t.Errorf("explicit credential rejection must remain terminal: %v", failure)
		}
	}
	if definitiveCredentialFailure(nil) {
		t.Fatal("nil must not expire an account")
	}
}

func TestRefreshOutDistinguishesRetryFromRescan(t *testing.T) {
	acc := &store.WechatAccount{ID: 7, OpenID: "openid-refresh-output"}
	retry := refreshOut(acc, "unknown", errors.New("proxy timeout"))
	if retry["rescan_required"] != false || retry["refresh_error"] != "proxy timeout" {
		t.Fatalf("retry output = %#v", retry)
	}
	rescan := refreshOut(acc, "expired", &protocol.RefreshRejectedError{Code: 401, Message: "token expired"})
	if rescan["rescan_required"] != true {
		t.Fatalf("rescan output = %#v", rescan)
	}
}

func TestCloseStopsKeepAliveLoop(t *testing.T) {
	app, err := NewApp(Config{
		ResourceRoot:      t.TempDir(),
		RequestTimeout:    time.Second,
		KeepAliveInterval: time.Hour,
		KeepAliveAhead:    45 * time.Minute,
	})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	done := make(chan error, 1)
	go func() {
		done <- app.Close()
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Close() error = %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Close() did not stop keepalive loop")
	}
}

func newKeepAliveTestApp(t *testing.T) *App {
	t.Helper()
	app, err := NewApp(Config{
		ResourceRoot:   t.TempDir(),
		RequestTimeout: time.Second,
		AvatarTimeout:  time.Second,
		SessionTTL:     time.Minute,
		QRSessionTTL:   time.Minute,
		KeepAliveAhead: 45 * time.Minute,
	})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	return app
}

func insertKeepAliveTestAccount(t *testing.T, app *App, openID string, expiresAt time.Time) *store.WechatAccount {
	t.Helper()
	status := "alive"
	creds := protocol.LoginBufferCredentials{
		OpenID:       openID,
		AccessToken:  "access-old",
		RefreshToken: "refresh-old",
		ExpiresAt:    expiresAt.Unix(),
		ExpiresIn:    7200,
	}
	acc, err := app.db.UpsertAccount(context.Background(), openID, "buffer-old", nil, nil, nil, nil, creds.ToMap(), &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	return acc
}
