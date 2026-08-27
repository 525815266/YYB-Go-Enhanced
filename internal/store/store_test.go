package store

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"
)

func TestOpenMigratesWMPFSessionsTableToSessions(t *testing.T) {
	path := filepath.Join(t.TempDir(), "yyb.db")
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("sql.Open() error = %v", err)
	}
	ctx := context.Background()
	if _, err = raw.ExecContext(ctx, `
CREATE TABLE wechat_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    openid          TEXT    NOT NULL UNIQUE,
    uin             INTEGER,
    alias           TEXT,
    nickname        TEXT,
    avatar          TEXT,
    user_info       TEXT,
    login_buffer    TEXT    NOT NULL,
    credentials     TEXT,
    status          TEXT,
    last_checked_at INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE TABLE wmpf_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    wechat_account_id INTEGER NOT NULL REFERENCES wechat_accounts(id) ON DELETE CASCADE,
    uin               INTEGER,
    tcp_proxy         TEXT    NOT NULL DEFAULT '',
    session_blob      TEXT    NOT NULL,
    expires_at        INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    UNIQUE(wechat_account_id, tcp_proxy)
);
CREATE INDEX idx_sess_expires ON wmpf_sessions(expires_at);
INSERT INTO wechat_accounts(id, openid, login_buffer, created_at, updated_at)
VALUES(1, 'openid-1', 'login-buffer', 10, 10);
INSERT INTO wmpf_sessions(id, wechat_account_id, uin, tcp_proxy, session_blob, expires_at, created_at, updated_at)
VALUES(7, 1, 12345, '', '{"ready":true}', ?, 20, 20);
`, time.Now().Add(time.Hour).Unix()); err != nil {
		_ = raw.Close()
		t.Fatalf("seed old schema: %v", err)
	}
	if err = raw.Close(); err != nil {
		t.Fatalf("close seed db: %v", err)
	}

	db, err := Open(path)
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	defer db.Close()

	oldExists, err := sqliteTableExists(ctx, db.sql, "wmpf_sessions")
	if err != nil {
		t.Fatalf("check old table: %v", err)
	}
	if oldExists {
		t.Fatalf("old wmpf_sessions table still exists")
	}
	newExists, err := sqliteTableExists(ctx, db.sql, "sessions")
	if err != nil {
		t.Fatalf("check new table: %v", err)
	}
	if !newExists {
		t.Fatalf("new sessions table does not exist")
	}

	session, err := db.GetSession(ctx, 1, "")
	if err != nil {
		t.Fatalf("GetSession() error = %v", err)
	}
	if session.ID != 7 {
		t.Fatalf("session id = %d, want 7", session.ID)
	}
	if ready, ok := session.SessionBlob["ready"].(bool); !ok || !ready {
		t.Fatalf("session blob = %#v", session.SessionBlob)
	}

	if err := db.SetAccountRemark(ctx, 1, " Boom "); err != nil {
		t.Fatalf("SetAccountRemark() error = %v", err)
	}
	account, err := db.GetAccount(ctx, 1)
	if err != nil {
		t.Fatalf("GetAccount() after remark error = %v", err)
	}
	if account.Remark == nil || *account.Remark != "Boom" {
		t.Fatalf("remark = %#v, want Boom", account.Remark)
	}
	if err := db.SetAccountRemark(ctx, 1, " "); err != nil {
		t.Fatalf("clear account remark: %v", err)
	}
	account, err = db.GetAccount(ctx, 1)
	if err != nil || account.Remark != nil {
		t.Fatalf("cleared remark account = %#v, err = %v", account, err)
	}

	if err := db.SetSetting(ctx, "qinglong_url", "http://qinglong:5700"); err != nil {
		t.Fatalf("SetSetting() error = %v", err)
	}
	setting, err := db.GetSetting(ctx, "qinglong_url")
	if err != nil || setting != "http://qinglong:5700" {
		t.Fatalf("GetSetting() = %q, %v", setting, err)
	}
}

func TestAccountPublicRecommendsRescanAfterTwentyFiveDays(t *testing.T) {
	oldObservation := time.Now().Add(-26 * 24 * time.Hour).Unix()
	account := &WechatAccount{Credentials: map[string]any{
		"refreshtoken":              "refresh",
		"refresh_token_observed_at": float64(oldObservation),
	}}

	public := account.Public()
	if !public.RescanRecommended {
		t.Fatal("RescanRecommended = false, want true")
	}
	if public.RefreshTokenObservedAt == nil || *public.RefreshTokenObservedAt != oldObservation {
		t.Fatalf("RefreshTokenObservedAt = %#v, want %d", public.RefreshTokenObservedAt, oldObservation)
	}

	account.Credentials["refresh_token_observed_at"] = time.Now().Add(-24 * 24 * time.Hour).Unix()
	if account.Public().RescanRecommended {
		t.Fatal("RescanRecommended = true before 25 days")
	}
}

func TestUpsertAccountReusesLowestFreeID(t *testing.T) {
	db, err := Open(":memory:")
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	defer db.Close()
	ctx := context.Background()
	first, err := db.UpsertAccount(ctx, "openid-1", "buffer", nil, nil, nil, nil, nil, nil)
	if err != nil || first.ID != 1 {
		t.Fatalf("first account = %+v, err=%v", first, err)
	}
	if err := db.DeleteAccount(ctx, first.ID); err != nil {
		t.Fatalf("DeleteAccount() error = %v", err)
	}
	recreated, err := db.UpsertAccount(ctx, "openid-2", "buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatalf("recreated account error = %v", err)
	}
	if recreated.ID != 1 {
		t.Fatalf("recreated account id = %d, want 1", recreated.ID)
	}
}

func TestUpsertAccountDoesNotConsumeIDOnDuplicate(t *testing.T) {
	db, err := Open(":memory:")
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	defer db.Close()
	ctx := context.Background()
	status := "alive"
	first, err := db.UpsertAccount(ctx, "same-openid", "buffer-1", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("first UpsertAccount() error = %v", err)
	}
	updated, err := db.UpsertAccount(ctx, "same-openid", "buffer-2", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("duplicate UpsertAccount() error = %v", err)
	}
	if updated.ID != first.ID || updated.LoginBuffer != "buffer-2" {
		t.Fatalf("duplicate upsert = id %d/%d buffer %q", first.ID, updated.ID, updated.LoginBuffer)
	}
	next, err := db.UpsertAccount(ctx, "next-openid", "buffer-3", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("next UpsertAccount() error = %v", err)
	}
	if next.ID != first.ID+1 {
		t.Fatalf("next account id = %d, want %d", next.ID, first.ID+1)
	}
}
