package protocol

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/store"
)

func TestStaleSavedTicketEscalatesToFreshTicketOnSecondRepair(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	status := "alive"
	acc, err := db.UpsertAccount(ctx, "stale-ticket-account", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "acc-tok", "refresh_token_observed_at": 100}, &status)
	if err != nil {
		t.Fatal(err)
	}
	cfg := DefaultConfig()
	cfg.FreshSessionWindow = -1
	cfg.RecoveryCooldown = -1
	pool := NewPool(cfg, db)

	// First repair: saved ticket passes ManualAuth, but target returns -14 -> does NOT fetch fresh ticket yet,
	// and marks the saved ticket as stale for subsequent repairs.
	fetches := 0
	st1, fresh1, _, err1 := recoverSessionWithTickets(ctx, acc.LoginBuffer, pool.hasStaleSavedTicket(acc, ""),
		func(_ context.Context, ticket string) (WmpfSession, error) {
			return WmpfSession{Session: AppSession{UIN: 111}}, nil
		},
		func(context.Context) (string, error) {
			fetches++
			return "fresh-buffer", nil
		},
		func(_ context.Context, _ WmpfSession) (map[string]any, error) {
			return nil, &ShortlinkRejectedError{Code: -14}
		},
	)
	_ = st1
	if err1 == nil || fetches != 0 || fresh1 != "" {
		t.Fatalf("first -14 repair must not fetch fresh ticket yet: err=%v fetches=%d fresh=%q", err1, fetches, fresh1)
	}
	pool.markStaleSavedTicket(acc, "")
	if !pool.hasStaleSavedTicket(acc, "") {
		t.Fatal("stale saved ticket marker was not recorded")
	}

	// Second repair under same authorization: saved ticket still passes ManualAuth and gets -14,
	// so it escalates to fetch("fresh-buffer") and succeeds without waiting 1 hour for ManualAuth -10.
	_, fresh2, res2, err2 := recoverSessionWithTickets(ctx, acc.LoginBuffer, pool.hasStaleSavedTicket(acc, ""),
		func(_ context.Context, ticket string) (WmpfSession, error) {
			if ticket == "fresh-buffer" {
				return WmpfSession{Session: AppSession{UIN: 222}}, nil
			}
			return WmpfSession{Session: AppSession{UIN: 111}}, nil
		},
		func(context.Context) (string, error) {
			fetches++
			return "fresh-buffer", nil
		},
		func(_ context.Context, st WmpfSession) (map[string]any, error) {
			if st.Session.UIN == 222 {
				return map[string]any{"code": "recovered-code"}, nil
			}
			return nil, &ShortlinkRejectedError{Code: -14}
		},
	)
	if err2 != nil || fetches != 1 || fresh2 != "fresh-buffer" || res2["code"] != "recovered-code" {
		t.Fatalf("second repair failed to escalate to fresh ticket: err=%v fetches=%d fresh=%q res=%v", err2, fetches, fresh2, res2)
	}
}

func TestRecoverPhoneSessionCommitsValidatedSession(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	status := "alive"
	acc, err := db.UpsertAccount(ctx, "phone-repair-account", "saved-buffer", nil, nil, nil, nil, map[string]any{"accesstoken": "acc-tok"}, &status)
	if err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)
	// Even if GetCode just verified the old session, GetPhoneNumber repair must not be skipped.
	verifiedSess := WmpfSession{
		Session: AppSession{UIN: 1001, SendKey: []byte("k1"), RecvKey: []byte("k2")},
		PSK:     pskEntry{PSKType: 1, PreSharedKey: "aa", TicketEntry: "bb", ExpiredAt: time.Now().Add(time.Hour).Unix()},
	}
	if err := db.PutSession(ctx, acc.ID, nil, verifiedSess.toBlob(), verifiedSess.PSK.ExpiredAt, ""); err != nil {
		t.Fatal(err)
	}
	pool.markSessionVerified(acc, acc.ID, verifiedSess)

	original := &ShortlinkRejectedError{Code: -14}
	rebuilt := verifiedSess
	rebuilt.Session.UIN = 2002
	res, err := pool.recoverSessionCommon(ctx, acc, "wx-phone-app", "", "phone", "get_phone_number", false, false, original,
		func(context.Context) (WmpfSession, map[string]any, error) {
			return rebuilt, map[string]any{"encryptedData": "enc", "iv": "iv"}, nil
		},
		func(m map[string]any) error {
			if len(m) == 0 {
				return errors.New("empty")
			}
			return nil
		},
	)
	if err != nil || res["encryptedData"] != "enc" {
		t.Fatalf("phone session recovery failed: err=%v res=%v", err, res)
	}
	stored, err := db.GetSession(ctx, acc.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	got, err := sessionFromBlob(stored.SessionBlob)
	if err != nil || got.Session.UIN != 2002 {
		t.Fatalf("phone session recovery did not persist replacement session: err=%v uin=%d", err, got.Session.UIN)
	}
}
