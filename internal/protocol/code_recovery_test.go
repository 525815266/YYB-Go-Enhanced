package protocol

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"testing"
	"time"

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
				_, _ = pool.recoverCodeSession(ctx, acc, "wx-target", "", original, probe)
				want := 1
				if outcome == "success" {
					want = 2
				}
				if calls != want {
					t.Fatal("failed recovery must cool down; successful recovery must not suppress the next request")
				}
				_, _ = pool.recoverCodeSession(ctx, acc, "wx-other", "", original, probe)
				if calls != want+1 {
					t.Fatal("one target's cooldown blocked another target")
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
