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

// The official-account OAuth request consumes the shortlink ticket that backs
// the session, so the next oauth_authorize on that session is always rejected
// with -14. RecoverOAuthSession must rebuild the session so one account can
// obtain more than one code. It reuses recoverCodeSession, which only commits
// the new session after a non-empty code is produced for the same AppID.
func TestRecoverOAuthSessionCommitsOnlyValidatedSession(t *testing.T) {
	for _, outcome := range []string{"success", "new_session_rejected", "empty_code", "route_mismatch"} {
		t.Run(outcome, func(t *testing.T) {
			ctx := context.Background()
			db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			status := "alive"
			acc, err := db.UpsertAccount(ctx, "synthetic-oauth-account", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "synthetic-access"}, &status)
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
			probe := func(context.Context) (WmpfSession, map[string]any, error) {
				st.Session.UIN = 5678
				switch outcome {
				case "new_session_rejected":
					return st, nil, &ShortlinkRejectedError{Code: -14}
				case "empty_code":
					return st, map[string]any{"code": ""}, nil
				case "route_mismatch":
					st.TCPProxy = "socks5://example.invalid:1080"
				}
				return st, map[string]any{"code": "synthetic-oauth-code"}, nil
			}

			original := &ShortlinkRejectedError{Code: -14}
			result, err := pool.recoverCodeSession(ctx, acc, "wx-oauth-target", "", original, probe)
			stored, _ := db.GetSession(ctx, acc.ID, "")
			storedJSON, _ := json.Marshal(stored.SessionBlob)

			switch outcome {
			case "success":
				if err != nil || result["code"] != "synthetic-oauth-code" {
					t.Fatalf("validated OAuth session did not commit: %v", err)
				}
				if string(beforeJSON) == string(storedJSON) {
					t.Fatal("recovered session was not written back")
				}
			default:
				if err == nil {
					t.Fatal("failed OAuth recovery must not succeed")
				}
				if string(beforeJSON) != string(storedJSON) {
					t.Fatal("failed OAuth recovery must keep the original session")
				}
			}
		})
	}
}

// Only an application level rejection may enter recovery. A transport failure
// or any other error must not rebuild the session.
func TestOAuthRecoveryOnlyOnApplicationRejection(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	status := "alive"
	acc, err := db.UpsertAccount(ctx, "synthetic-oauth-scope", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "synthetic-access"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)

	probe := func(context.Context) (WmpfSession, map[string]any, error) {
		t.Fatal("recovery must not run for a non application rejection")
		return WmpfSession{}, nil, nil
	}

	transport := &ShortlinkRejectedError{Code: -14, Transport: true}
	if _, err := pool.recoverCodeSession(ctx, acc, "wx-oauth-target", "", transport, probe); err == nil {
		t.Fatal("transport rejection must not enter recovery")
	}
	if _, err := pool.recoverCodeSession(ctx, acc, "wx-oauth-target", "", errors.New("synthetic failure"), probe); err == nil {
		t.Fatal("plain error must not enter recovery")
	}
}
