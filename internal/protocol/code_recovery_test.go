package protocol

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

func TestRejectedCodeRecoveryCommitsOnlyValidatedSession(t *testing.T) {
	for _, outcome := range []string{"success", "ticket_rejected", "new_session_rejected", "empty_code", "route_mismatch", "rescan", "credential_rotation", "manual_expiry"} {
		t.Run(outcome, func(t *testing.T) {
			ctx := context.Background()
			db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			status := "alive"
			acc, err := db.UpsertAccount(ctx, "synthetic-repair-account", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "synthetic-access", "refresh_token_observed_at": 123}, &status)
			if err != nil {
				t.Fatal(err)
			}
			st := WmpfSession{Session: AppSession{UIN: 1234, SendKey: []byte("send"), RecvKey: []byte("recv")},
				PSK: pskEntry{PSKType: 1, PreSharedKey: "001122", TicketEntry: "aabbcc", ExpiredAt: time.Now().Add(time.Hour).Unix()}}
			if err = db.PutSession(ctx, acc.ID, nil, st.toBlob(), st.PSK.ExpiredAt, ""); err != nil {
				t.Fatal(err)
			}
			before, _ := db.GetSession(ctx, acc.ID, "")
			beforeJSON, _ := json.Marshal(before.SessionBlob)
			pool := NewPool(DefaultConfig(), db)
			calls := 0
			probe := func(context.Context) (WmpfSession, map[string]any, error) {
				calls++
				st.Session.UIN = 5678
				switch outcome {
				case "ticket_rejected":
					return st, nil, errors.New("synthetic ticket refusal")
				case "new_session_rejected":
					return st, nil, &ShortlinkRejectedError{Code: -14}
				case "empty_code":
					return st, map[string]any{"code": ""}, nil
				case "route_mismatch":
					st.TCPProxy = "socks5://example.invalid:1080"
				case "rescan":
					if err := db.SetAccountCredential(ctx, acc.ID, "new-scan-buffer", map[string]any{"accesstoken": "new-scan"}); err != nil {
						t.Fatal(err)
					}
				case "credential_rotation":
					if _, err := db.ApplyCredentialRefresh(ctx, acc, acc.LoginBuffer, map[string]any{"accesstoken": "new-key"}, "alive"); err != nil {
						t.Fatal(err)
					}
				case "manual_expiry":
					if err := db.SetAccountStatus(ctx, acc.ID, "expired"); err != nil {
						t.Fatal(err)
					}
				}
				return st, map[string]any{"code": "synthetic-code"}, nil
			}
			original := &ShortlinkRejectedError{Code: -14}
			result, err := pool.recoverCodeSession(ctx, acc, "wx-target", "", original, probe)
			if outcome == "success" {
				if err != nil || result["code"] != "synthetic-code" {
					t.Fatal("validated code session did not commit", err)
				}
				stored, _ := db.GetSession(ctx, acc.ID, "")
				got, parseErr := sessionFromBlob(stored.SessionBlob)
				if parseErr != nil || got.Session.UIN != 5678 {
					t.Fatal("validated replacement session missing")
				}
				account, _ := db.GetAccount(ctx, acc.ID)
				if !account.SameAuthorization(acc) || account.LoginBuffer != acc.LoginBuffer {
					t.Fatal("session repair changed authorization")
				}
			} else {
				var repair *CodeSessionRecoveryError
				if err == nil || !errors.As(err, &repair) || !errors.Is(err, original) {
					t.Fatal("repair failure lost the original rejection")
				}
				stored, cacheErr := db.GetSession(ctx, acc.ID, "")
				// Manual expiry clears credentials; it may also remove the cache.
				if outcome != "manual_expiry" {
					afterJSON, _ := json.Marshal(stored.SessionBlob)
					if cacheErr != nil || string(afterJSON) != string(beforeJSON) {
						t.Fatal("failed candidate overwrote the original cache")
					}
				}
			}
			if outcome != "rescan" && outcome != "credential_rotation" && outcome != "manual_expiry" {
				// A failed repair (cooldown) and a successful one (the session was
				// just verified) both hold the same target off.
				_, _ = pool.recoverCodeSession(ctx, acc, "wx-target", "", original, probe)
				if calls != 1 {
					t.Fatal("a repeated -14 must not start another login right after a repair")
				}
				_, _ = pool.recoverCodeSession(ctx, acc, "wx-other", "", original, probe)
				want := 2
				if outcome == "success" {
					// The session was just verified, so another AppID's -14 is that AppID's own.
					want = 1
				}
				if calls != want {
					t.Fatal("a failed repair on one target blocked another; a verified session must not log in again")
				}
			}
		})
	}
}

func TestSessionOnlyCodeRejectionCannotStartNewLogin(t *testing.T) {
	status := store.StatusSessionOnly
	acc := &store.WechatAccount{ID: 1, Status: &status}
	pool := NewPool(DefaultConfig(), nil)
	original := &ShortlinkRejectedError{Code: -14}
	_, err := pool.recoverCodeSession(context.Background(), acc, "wx-target", "", original, func(context.Context) (WmpfSession, map[string]any, error) {
		t.Fatal("rejected authorization attempted a new session")
		return WmpfSession{}, nil, nil
	})
	if !errors.Is(err, original) {
		t.Fatal("limited request lost its error")
	}
}

func TestCodeTicketRecoveryStages(t *testing.T) {
	for _, tc := range []struct {
		name                                  string
		savedErr, fetchErr, freshErr, codeErr error
		logins, fetches, requests             int
		credentialRecovery                    bool
	}{
		{name: "saved_ticket_success", logins: 1, requests: 1},
		{name: "saved_rejected_fresh_success", savedErr: &ManualAuthRejectedError{Code: -10}, logins: 2, fetches: 1, requests: 1},
		{name: "saved_network_error", savedErr: context.DeadlineExceeded, logins: 1},
		{name: "saved_other_rejection", savedErr: &ManualAuthRejectedError{Code: -13}, logins: 1},
		{name: "target_rejected", codeErr: &ShortlinkRejectedError{Code: -14}, logins: 1, requests: 1},
		{name: "target_empty", codeErr: ErrLoginCodeUnavailable, logins: 1, requests: 1},
		{name: "fresh_access_rejected", savedErr: &ManualAuthRejectedError{Code: -10}, fetchErr: &LoginBufferRejectedError{Code: -101}, logins: 1, fetches: 1, credentialRecovery: true},
		{name: "fresh_ticket_other_error", savedErr: &ManualAuthRejectedError{Code: -10}, fetchErr: &LoginBufferRejectedError{Code: 1}, logins: 1, fetches: 1},
		{name: "fresh_ticket_network_error", savedErr: &ManualAuthRejectedError{Code: -10}, fetchErr: context.DeadlineExceeded, logins: 1, fetches: 1},
		{name: "fresh_manual_rejected", savedErr: &ManualAuthRejectedError{Code: -10}, freshErr: &ManualAuthRejectedError{Code: -10}, logins: 2, fetches: 1},
		{name: "fresh_target_rejected", savedErr: &ManualAuthRejectedError{Code: -10}, codeErr: &ShortlinkRejectedError{Code: -14}, logins: 2, fetches: 1, requests: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logins, fetches, requests := 0, 0, 0
			_, result, err := recoverCodeWithTickets(context.Background(), "saved", func(_ context.Context, ticket string) (WmpfSession, error) {
				logins++
				if logins == 1 {
					if ticket != "saved" {
						t.Fatal("saved ticket was skipped")
					}
					return WmpfSession{}, tc.savedErr
				}
				if ticket != "fresh" {
					t.Fatal("fresh ticket was not used")
				}
				return WmpfSession{}, tc.freshErr
			}, func(context.Context) (string, error) {
				fetches++
				return "fresh", tc.fetchErr
			}, func(context.Context, WmpfSession) (map[string]any, error) {
				requests++
				return map[string]any{"code": "synthetic-code"}, tc.codeErr
			})
			if logins != tc.logins || fetches != tc.fetches || requests != tc.requests {
				t.Fatalf("logins=%d fetches=%d requests=%d", logins, fetches, requests)
			}
			var marked *CodeCredentialRecoveryError
			if errors.As(err, &marked) != tc.credentialRecovery {
				t.Fatal("wrong credential recovery classification")
			}
			wantErr := tc.fetchErr
			if wantErr == nil {
				wantErr = tc.freshErr
			}
			if wantErr == nil {
				wantErr = tc.codeErr
			}
			if wantErr == nil && tc.fetches == 0 {
				wantErr = tc.savedErr
			}
			if wantErr != nil && !errors.Is(err, wantErr) {
				t.Fatal("lost final rejection", err)
			}
			if wantErr == nil && (err != nil || result["code"] != "synthetic-code") {
				t.Fatal("recovery did not return code", err)
			}
		})
	}
}

func TestIdenticalRejectedTicketIsNotReplayed(t *testing.T) {
	first := &ManualAuthRejectedError{Code: -10}
	logins := 0
	_, _, err := recoverCodeWithTickets(context.Background(), "saved", func(context.Context, string) (WmpfSession, error) {
		logins++
		return WmpfSession{}, first
	}, func(context.Context) (string, error) {
		return "saved", nil
	}, func(context.Context, WmpfSession) (map[string]any, error) {
		t.Fatal("rejected ticket must not reach a business request")
		return nil, nil
	})
	if logins != 1 || !errors.Is(err, first) {
		t.Fatal("identical ticket was replayed or first rejection lost")
	}
}

func TestCancelledSavedTicketRejectionDoesNotFetch(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	_, _, err := recoverCodeWithTickets(ctx, "saved", func(context.Context, string) (WmpfSession, error) {
		cancel()
		return WmpfSession{}, &ManualAuthRejectedError{Code: -10}
	}, func(context.Context) (string, error) {
		t.Fatal("cancelled recovery fetched a ticket")
		return "", nil
	}, func(context.Context, WmpfSession) (map[string]any, error) {
		t.Fatal("cancelled recovery sent a code request")
		return nil, nil
	})
	if err == nil {
		t.Fatal("cancelled recovery returned success")
	}
}

func TestActiveAccountDoesNotConsumeNativeKeyBeforeSavedTicket(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	status := "alive"
	acc, err := db.UpsertAccount(ctx, "synthetic-saved-first", "", nil, nil, nil, nil, map[string]any{"scan": 123}, &status)
	if err != nil {
		t.Fatal(err)
	}
	st := syntheticNativeSession()
	st.PSK.ExpiredAt = time.Now().Add(-time.Minute).Unix()
	if err := db.PutSessionForAccount(ctx, acc, st.Session.UIN, st.toBlob(), st.PSK.ExpiredAt, ""); err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)
	if !pool.hasNativeRecovery(ctx, acc.ID, "") {
		t.Fatal("native recovery fixture unavailable")
	}
	_, err = pool.state(ctx, "", acc.ID, "", false, acc)
	if err == nil || err.Error() != "login_buffer is empty" {
		t.Fatal("saved-ticket path was skipped", err)
	}
	if !pool.hasNativeRecovery(ctx, acc.ID, "") {
		t.Fatal("normal login consumed an independent native key")
	}
}

func testSession(send string, proxy string) WmpfSession {
	return WmpfSession{
		Session:  AppSession{UIN: 1234, SendKey: []byte(send), RecvKey: []byte("recv-" + send)},
		PSK:      pskEntry{PSKType: 1, PreSharedKey: "001122", TicketEntry: "aabbcc", ExpiredAt: time.Now().Add(time.Hour).Unix()},
		TCPProxy: proxy,
	}
}

// A -14 shortly after a wx.login returned a code on the session that is cached
// now is more likely the AppID's own refusal than an expired session, so it must
// not log in again. The marker belongs to that session and route only.
func TestFreshSessionSuppressesRecovery(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	status := "alive"
	acc, err := db.UpsertAccount(ctx, "synthetic-fresh-account", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "synthetic-access", "refresh_token_observed_at": 123}, &status)
	if err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)
	original := &ShortlinkRejectedError{Code: -14}
	probes := 0
	probe := func(context.Context) (WmpfSession, map[string]any, error) {
		probes++
		return WmpfSession{}, nil, errors.New("synthetic ticket refusal")
	}
	cache := func(st WmpfSession) {
		t.Helper()
		if err := db.PutSession(ctx, acc.ID, nil, st.toBlob(), st.PSK.ExpiredAt, st.TCPProxy); err != nil {
			t.Fatal(err)
		}
	}
	recoverFor := func(account *store.WechatAccount, target, proxy string) {
		t.Helper()
		_, _ = pool.recoverCodeSession(ctx, account, target, proxy, original, probe)
	}

	first := testSession("first", "")
	cache(first)
	pool.markSessionVerified(acc, acc.ID, first)
	for _, target := range []string{"wx-a", "wx-b"} {
		if _, err := pool.recoverCodeSession(ctx, acc, target, "", original, probe); !errors.Is(err, original) {
			t.Fatalf("suppressed recovery lost the original -14: %v", err)
		}
	}
	if probes != 0 {
		t.Fatal("recovery ran on a session that had just returned a code")
	}

	// A replaced cache is a new session: the earlier success does not cover it.
	cache(testSession("second", ""))
	recoverFor(acc, "wx-a", "")
	if probes != 1 {
		t.Fatal("a success on a replaced session suppressed recovery")
	}

	// The marker belongs to one route: a session on another route is not covered.
	routed := testSession("routed", "socks5://example.invalid:1080")
	cache(routed)
	pool.markSessionVerified(acc, acc.ID, routed)
	recoverFor(acc, "wx-c", "")
	if probes != 2 {
		t.Fatal("a success on another route suppressed recovery")
	}

	// After the window a -14 may repair the session again.
	second := testSession("second", "")
	cache(second)
	pool.markSessionVerified(acc, acc.ID, second)
	pool.mu.Lock()
	key := sessionKey(acc.ID, "")
	verified := pool.verified[key]
	verified.At = time.Now().Add(-DefaultFreshSessionWindow - time.Second)
	pool.verified[key] = verified
	pool.mu.Unlock()
	recoverFor(acc, "wx-b", "")
	if probes != 3 {
		t.Fatal("recovery stayed suppressed after the window")
	}

	// An invalidated cache leaves nothing to vouch for: the success no longer counts.
	cache(second)
	pool.markSessionVerified(acc, acc.ID, second)
	if err := db.InvalidateAccountSessions(ctx, acc.ID); err != nil {
		t.Fatal(err)
	}
	recoverFor(acc, "wx-e", "")
	if probes != 4 {
		t.Fatal("a success on an invalidated session suppressed recovery")
	}

	// A re-scan is a new authorization; the earlier success no longer applies.
	cache(second)
	pool.markSessionVerified(acc, acc.ID, second)
	if err := db.SetAccountCredential(ctx, acc.ID, "new-scan-buffer", map[string]any{"accesstoken": "new-scan"}); err != nil {
		t.Fatal(err)
	}
	rescanned, err := db.GetAccount(ctx, acc.ID)
	if err != nil {
		t.Fatal(err)
	}
	recoverFor(rescanned, "wx-d", "")
	if probes != 5 {
		t.Fatal("a success from before the re-scan suppressed recovery")
	}
}

// Skipping a recovery because the session was just verified is reported under
// its own event, apart from the failure cooldown.
func TestFreshSessionSkipHasItsOwnEvent(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	logger, err := eventlog.New(db)
	if err != nil {
		t.Fatal(err)
	}
	status := "alive"
	acc, err := db.UpsertAccount(ctx, "synthetic-skip-account", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "synthetic-access"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)
	pool.SetEventLogger(logger)
	st := testSession("skip", "")
	if err := db.PutSession(ctx, acc.ID, nil, st.toBlob(), st.PSK.ExpiredAt, ""); err != nil {
		t.Fatal(err)
	}
	pool.markSessionVerified(acc, acc.ID, st)
	original := &ShortlinkRejectedError{Code: -14}
	if _, err := pool.recoverCodeSession(ctx, acc, "wx-skip", "", original, func(context.Context) (WmpfSession, map[string]any, error) {
		t.Fatal("a just-verified session started a recovery")
		return WmpfSession{}, nil, nil
	}); !errors.Is(err, original) {
		t.Fatalf("skipped recovery lost the original -14: %v", err)
	}
	logger.Close()
	entries, err := db.ListSystemLogs(ctx, store.SystemLogFilter{Limit: 20})
	if err != nil {
		t.Fatal(err)
	}
	var skipped, deferred int
	for _, entry := range entries {
		switch entry.Event {
		case "protocol.recovery_skipped":
			skipped++
			if !strings.Contains(string(entry.Details), `"app_id":"wx-skip"`) {
				t.Fatalf("skip event lacks the AppID: %s", entry.Details)
			}
		case "protocol.recovery_deferred":
			deferred++
		}
	}
	if skipped != 1 || deferred != 0 {
		t.Fatalf("expected one recovery_skipped and no recovery_deferred, got %d and %d", skipped, deferred)
	}
}

// A cancelled request must not register a repair attempt (and its five minute
// backoff) that it can never run.
func TestCancelledRecoveryDoesNotRegisterAnAttempt(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	status := "alive"
	acc, err := db.UpsertAccount(context.Background(), "synthetic-cancel-account", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "synthetic-access"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	original := &ShortlinkRejectedError{Code: -14}
	_, err = pool.recoverCodeSession(ctx, acc, "wx-cancel", "", original, func(context.Context) (WmpfSession, map[string]any, error) {
		t.Fatal("a probe ran on a cancelled context")
		return WmpfSession{}, nil, nil
	})
	var repair *CodeSessionRecoveryError
	if !errors.As(err, &repair) || !errors.Is(err, original) {
		t.Fatalf("a cancelled recovery lost the original -14 or its type: %v", err)
	}
	pool.mu.Lock()
	registered := len(pool.codeRepairs)
	pool.mu.Unlock()
	if registered != 0 {
		t.Fatalf("a cancelled recovery registered %d repair attempts", registered)
	}
}

// A request cancelled between the session lookup and the registration of its
// repair attempt must not leave a five minute backoff behind. The hook cancels
// at exactly that point, so removing the re-check inside the critical section
// makes this test fail.
func TestRecoveryCancelledWhileRegisteringLeavesNoBackoff(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	status := "alive"
	acc, err := db.UpsertAccount(context.Background(), "synthetic-late-cancel", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "synthetic-access"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)
	ctx, cancel := context.WithCancel(context.Background())
	repairRegistrationHook = cancel
	t.Cleanup(func() { repairRegistrationHook = nil })
	original := &ShortlinkRejectedError{Code: -14}
	probed := false
	_, err = pool.recoverCodeSession(ctx, acc, "wx-late", "", original, func(context.Context) (WmpfSession, map[string]any, error) {
		probed = true
		return WmpfSession{}, nil, errors.New("synthetic probe failure")
	})
	var repair *CodeSessionRecoveryError
	if !errors.As(err, &repair) || !errors.Is(err, original) {
		t.Fatalf("a request cancelled while registering lost the original -14 or its type: %v", err)
	}
	if probed {
		t.Fatal("a cancelled request still ran the probe")
	}
	pool.mu.Lock()
	registered := len(pool.codeRepairs)
	pool.mu.Unlock()
	if registered != 0 {
		t.Fatalf("a request cancelled while registering left %d backoff entries", registered)
	}
	repairRegistrationHook = nil
	if _, err := pool.recoverCodeSession(context.Background(), acc, "wx-late", "", original, func(context.Context) (WmpfSession, map[string]any, error) {
		probed = true
		return WmpfSession{}, nil, errors.New("synthetic probe failure")
	}); err == nil || !probed {
		t.Fatalf("the next normal request must still be allowed to try: probed=%v err=%v", probed, err)
	}
}

// The session identity digest changes when any of its four parts changes, is
// stable for the same material, and does not confuse different splits.
func TestSessionFingerprintCoversEveryPartAndSplit(t *testing.T) {
	base := testSession("alpha", "")
	same := testSession("alpha", "")
	if sessionFingerprint(base) != sessionFingerprint(same) {
		t.Fatal("identical material produced different digests")
	}
	changed := []func(*WmpfSession){
		func(s *WmpfSession) { s.Session.SendKey = []byte("other") },
		func(s *WmpfSession) { s.Session.RecvKey = []byte("other") },
		func(s *WmpfSession) { s.PSK.PreSharedKey = "ffff" },
		func(s *WmpfSession) { s.PSK.TicketEntry = "ffff" },
	}
	for i, mutate := range changed {
		other := testSession("alpha", "")
		mutate(&other)
		if sessionFingerprint(other) == sessionFingerprint(base) {
			t.Fatalf("changing part %d did not change the digest", i)
		}
	}
	left := testSession("alpha", "")
	left.Session.SendKey, left.Session.RecvKey = []byte("ab"), []byte("c")
	right := testSession("alpha", "")
	right.Session.SendKey, right.Session.RecvKey = []byte("a"), []byte("bc")
	if sessionFingerprint(left) == sessionFingerprint(right) {
		t.Fatal("different splits of the same bytes collided")
	}
	// A database round trip (JSON out and back in) must not change it.
	raw, err := json.Marshal(base.toBlob())
	if err != nil {
		t.Fatal(err)
	}
	var stored map[string]any
	if err := json.Unmarshal(raw, &stored); err != nil {
		t.Fatal(err)
	}
	round, err := sessionFromBlob(stored)
	if err != nil {
		t.Fatal(err)
	}
	if sessionFingerprint(round) != sessionFingerprint(base) {
		t.Fatal("the digest changed across a persistence round trip")
	}
}
