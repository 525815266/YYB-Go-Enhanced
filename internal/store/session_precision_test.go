package store

import (
	"context"
	"encoding/json"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func TestSessionRoundTripPreservesIntegerPrecision(t *testing.T) {
	ctx := context.Background()
	for _, uin := range []int64{1234, 1<<53 + 1, -(1<<53 + 1), 1<<63 - 1, -1 << 63} {
		t.Run(strconv.FormatInt(uin, 10), func(t *testing.T) {
			db, err := Open(filepath.Join(t.TempDir(), "session.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			acc, err := db.UpsertAccount(ctx, "synthetic-precision", "buffer", nil, nil, nil, nil, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			blob := map[string]any{"session": map[string]any{"uin": uin}, "other_integer": uin}
			expiry := time.Now().Add(time.Hour).Unix()
			if err := db.PutSessionForAccount(ctx, acc, uin, blob, expiry, ""); err != nil {
				t.Fatal(err)
			}
			check := func(row *SessionRow, err error) {
				t.Helper()
				if err != nil {
					t.Fatal(err)
				}
				if row.UIN == nil || *row.UIN != uin {
					t.Fatal("SQL UIN did not survive the round trip")
				}
				for _, value := range []any{row.SessionBlob["session"].(map[string]any)["uin"], row.SessionBlob["other_integer"]} {
					raw, err := json.Marshal(value)
					if err != nil || string(raw) != strconv.FormatInt(uin, 10) {
						t.Fatalf("JSON integer changed across session persistence: %s", raw)
					}
				}
			}
			check(db.GetSession(ctx, acc.ID, ""))
			previous, err := db.GetSessionForRecovery(ctx, acc.ID, "")
			check(previous, err)
			if _, err := db.ReplaceNativeSessionForAccount(ctx, acc, previous, uin, previous.SessionBlob, expiry); err != nil {
				t.Fatal(err)
			}
			check(db.GetSession(ctx, acc.ID, ""))
		})
	}
}

func TestSessionDecodeRejectsTrailingJSON(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "invalid.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	acc, err := db.UpsertAccount(ctx, "synthetic-invalid-json", "buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	expiry := time.Now().Add(time.Hour).Unix()
	if err := db.PutSession(ctx, acc.ID, nil, map[string]any{}, expiry, ""); err != nil {
		t.Fatal(err)
	}
	for _, raw := range []string{`{} {}`, `{} trailing`, `{"session":`} {
		if _, err := db.sql.ExecContext(ctx, "UPDATE sessions SET session_blob=? WHERE wechat_account_id=?", raw, acc.ID); err != nil {
			t.Fatal(err)
		}
		if _, err := db.GetSession(ctx, acc.ID, ""); err == nil {
			t.Fatal("malformed or concatenated session JSON accepted")
		}
	}
}
