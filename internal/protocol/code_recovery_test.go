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
			result, err := pool.recoverCodeSession(ctx, acc, "", original, probe)
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
				_, _ = pool.recoverCodeSession(ctx, acc, "", original, probe)
				if calls != 1 {
					t.Fatal("repeated rejection bypassed repair cooldown")
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
	_, err := pool.recoverCodeSession(context.Background(), acc, "", original, func(context.Context) (WmpfSession, map[string]any, error) {
		t.Fatal("rejected authorization attempted a new session")
		return WmpfSession{}, nil, nil
	})
	if !errors.Is(err, original) {
		t.Fatal("limited request lost its error")
	}
}
