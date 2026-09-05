package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

const schema = `
CREATE TABLE IF NOT EXISTS wechat_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    openid          TEXT    NOT NULL UNIQUE,
    uin             INTEGER,
    alias           TEXT,
    nickname        TEXT,
    remark          TEXT,
    avatar          TEXT,
    user_info       TEXT,
    login_buffer    TEXT    NOT NULL,
    credentials     TEXT,
    status          TEXT,
    last_checked_at INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wxacc_uin ON wechat_accounts(uin);

CREATE TABLE IF NOT EXISTS sessions (
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
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS features (
    code        INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS account_script_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES wechat_accounts(id) ON DELETE CASCADE,
    script_key    TEXT    NOT NULL,
    ql_cron_id    INTEGER NOT NULL,
    schedule      TEXT    NOT NULL,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    UNIQUE(account_id, script_key)
);
CREATE INDEX IF NOT EXISTS idx_account_script_jobs_account ON account_script_jobs(account_id);

CREATE TABLE IF NOT EXISTS account_push_settings (
    account_id     INTEGER PRIMARY KEY REFERENCES wechat_accounts(id) ON DELETE CASCADE,
    channel        TEXT    NOT NULL DEFAULT 'none',
    token_env_name TEXT    NOT NULL DEFAULT '',
    topic_env_name TEXT    NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS proxy_provider_profiles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    provider   TEXT    NOT NULL DEFAULT 'ipzan',
    proxy_type TEXT    NOT NULL DEFAULT 'http',
    api_url    TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_proxy_settings (
    account_id             INTEGER PRIMARY KEY REFERENCES wechat_accounts(id) ON DELETE CASCADE,
    mode                   TEXT    NOT NULL DEFAULT 'direct',
    proxy_type             TEXT    NOT NULL DEFAULT 'http',
    static_proxy           TEXT    NOT NULL DEFAULT '',
    api_url                TEXT    NOT NULL DEFAULT '',
    provider_profile_id    INTEGER,
    region_code            TEXT    NOT NULL DEFAULT '',
    region_province        TEXT    NOT NULL DEFAULT '',
    region_city            TEXT    NOT NULL DEFAULT '',
    refresh_ahead_seconds  INTEGER NOT NULL DEFAULT 300,
    created_at             INTEGER NOT NULL,
    updated_at             INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
`

var defaultFeatures = []Feature{
	{Code: 1001, Name: "getCode", Description: stringPtr("wx.login code"), Enabled: true},
	{Code: 1002, Name: "getPhoneNumber", Description: stringPtr("取手机号"), Enabled: true},
	{Code: 1003, Name: "operateWxData", Description: stringPtr("通用云函数代理"), Enabled: true},
}

type DB struct {
	sql *sql.DB
}

type WechatAccount struct {
	ID            int64          `json:"id"`
	OpenID        string         `json:"openid"`
	UIN           *int64         `json:"uin,omitempty"`
	Alias         *string        `json:"alias,omitempty"`
	Nickname      *string        `json:"nickname,omitempty"`
	Remark        *string        `json:"remark,omitempty"`
	Avatar        *string        `json:"avatar,omitempty"`
	UserInfo      map[string]any `json:"user_info,omitempty"`
	LoginBuffer   string         `json:"login_buffer,omitempty"`
	Credentials   map[string]any `json:"credentials,omitempty"`
	Status        *string        `json:"status,omitempty"`
	LastCheckedAt *int64         `json:"last_checked_at,omitempty"`
	CreatedAt     int64          `json:"created_at"`
	UpdatedAt     int64          `json:"updated_at"`
}

type AccountPublic struct {
	ID                     int64   `json:"id"`
	OpenID                 string  `json:"openid"`
	UIN                    *int64  `json:"uin"`
	Alias                  *string `json:"alias"`
	Nickname               *string `json:"nickname"`
	Remark                 *string `json:"remark"`
	Avatar                 *string `json:"avatar"`
	Status                 *string `json:"status"`
	RefreshTokenObservedAt *int64  `json:"refresh_token_observed_at,omitempty"`
	CredentialExpiresAt    *int64  `json:"credential_expires_at,omitempty"`
	CredentialExpiresIn    int64   `json:"credential_expires_in,omitempty"`
	RescanRecommended      bool    `json:"rescan_recommended"`
	LastCheckedAt          *int64  `json:"last_checked_at"`
	CreatedAt              int64   `json:"created_at"`
	UpdatedAt              int64   `json:"updated_at"`
}

type SessionRow struct {
	ID              int64
	WechatAccountID int64
	UIN             *int64
	TCPProxy        string
	SessionBlob     map[string]any
	ExpiresAt       int64
	CreatedAt       int64
	UpdatedAt       int64
}

type Feature struct {
	Code        int     `json:"code"`
	Name        string  `json:"name"`
	Description *string `json:"description"`
	Enabled     bool    `json:"enabled"`
}

func Open(path string) (*DB, error) {
	if path != ":memory:" {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return nil, err
		}
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err = db.ExecContext(ctx, "PRAGMA busy_timeout=5000"); err != nil {
		_ = db.Close()
		return nil, err
	}
	if path != ":memory:" {
		_, _ = db.ExecContext(ctx, "PRAGMA journal_mode=WAL")
	}
	_, _ = db.ExecContext(ctx, "PRAGMA synchronous=NORMAL")
	_, _ = db.ExecContext(ctx, "PRAGMA foreign_keys=ON")
	if err = migrateSessionsTable(ctx, db); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err = db.ExecContext(ctx, schema); err != nil {
		_ = db.Close()
		return nil, err
	}
	if err = migrateAccountRemark(ctx, db); err != nil {
		_ = db.Close()
		return nil, err
	}
	if err = migrateAccountProxySettings(ctx, db); err != nil {
		_ = db.Close()
		return nil, err
	}
	out := &DB{sql: db}
	if err = out.EnsureDefaultFeatures(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return out, nil
}

func (db *DB) Close() error {
	if db == nil || db.sql == nil {
		return nil
	}
	return db.sql.Close()
}

func (db *DB) EnsureDefaultFeatures(ctx context.Context) error {
	for _, f := range defaultFeatures {
		desc := nullableString(f.Description)
		if _, err := db.sql.ExecContext(ctx,
			"INSERT OR IGNORE INTO features(code, name, description, enabled) VALUES(?,?,?,1)",
			f.Code, f.Name, desc,
		); err != nil {
			return err
		}
	}
	return nil
}

func migrateSessionsTable(ctx context.Context, db *sql.DB) error {
	oldExists, err := sqliteTableExists(ctx, db, "wmpf_sessions")
	if err != nil {
		return err
	}
	if !oldExists {
		return nil
	}
	newExists, err := sqliteTableExists(ctx, db, "sessions")
	if err != nil {
		return err
	}
	if !newExists {
		if _, err = db.ExecContext(ctx, "DROP INDEX IF EXISTS idx_sess_expires"); err != nil {
			return err
		}
		_, err = db.ExecContext(ctx, "ALTER TABLE wmpf_sessions RENAME TO sessions")
		return err
	}
	if _, err = db.ExecContext(ctx, `
INSERT OR IGNORE INTO sessions
(id, wechat_account_id, uin, tcp_proxy, session_blob, expires_at, created_at, updated_at)
SELECT id, wechat_account_id, uin, tcp_proxy, session_blob, expires_at, created_at, updated_at
FROM wmpf_sessions`); err != nil {
		return err
	}
	_, err = db.ExecContext(ctx, "DROP TABLE wmpf_sessions")
	return err
}

func sqliteTableExists(ctx context.Context, db *sql.DB, name string) (bool, error) {
	var n int
	err := db.QueryRowContext(ctx, "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", name).Scan(&n)
	return n > 0, err
}

func migrateAccountRemark(ctx context.Context, db *sql.DB) error {
	rows, err := db.QueryContext(ctx, "PRAGMA table_info(wechat_accounts)")
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var cid, notNull, primaryKey int
		var name, columnType string
		var defaultValue any
		if err := rows.Scan(&cid, &name, &columnType, &notNull, &defaultValue, &primaryKey); err != nil {
			return err
		}
		if name == "remark" {
			return nil
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	_, err = db.ExecContext(ctx, "ALTER TABLE wechat_accounts ADD COLUMN remark TEXT")
	return err
}

func migrateAccountProxySettings(ctx context.Context, db *sql.DB) error {
	columns := []struct {
		name       string
		definition string
	}{
		{"provider_profile_id", "INTEGER"},
		{"region_code", "TEXT NOT NULL DEFAULT ''"},
		{"region_province", "TEXT NOT NULL DEFAULT ''"},
		{"region_city", "TEXT NOT NULL DEFAULT ''"},
		{"refresh_ahead_seconds", "INTEGER NOT NULL DEFAULT 300"},
	}
	for _, column := range columns {
		exists, err := sqliteColumnExists(ctx, db, "account_proxy_settings", column.name)
		if err != nil {
			return err
		}
		if exists {
			continue
		}
		if _, err = db.ExecContext(ctx, "ALTER TABLE account_proxy_settings ADD COLUMN "+column.name+" "+column.definition); err != nil {
			return err
		}
	}
	return nil
}

func sqliteColumnExists(ctx context.Context, db *sql.DB, table, wanted string) (bool, error) {
	rows, err := db.QueryContext(ctx, "PRAGMA table_info("+table+")")
	if err != nil {
		return false, err
	}
	defer rows.Close()
	for rows.Next() {
		var cid, notNull, primaryKey int
		var name, columnType string
		var defaultValue any
		if err := rows.Scan(&cid, &name, &columnType, &notNull, &defaultValue, &primaryKey); err != nil {
			return false, err
		}
		if name == wanted {
			return true, nil
		}
	}
	return false, rows.Err()
}

func (db *DB) UpsertAccount(ctx context.Context, openid, loginBuffer string, alias, nickname, avatar *string, userInfo map[string]any, credentials map[string]any, status *string) (*WechatAccount, error) {
	openid = strings.TrimSpace(openid)
	if openid == "" {
		return nil, errors.New("openid is required")
	}
	now := time.Now().Unix()
	userJSON, err := marshalNullable(userInfo)
	if err != nil {
		return nil, err
	}
	credJSON, err := marshalNullable(credentials)
	if err != nil {
		return nil, err
	}
	// Keep the lookup, lowest-free-ID calculation and write in one transaction.
	// Otherwise two simultaneous successful scans can both select the same gap.
	tx, err := db.sql.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()

	// Reuse the lowest free account slot. This keeps a single account at ID 1
	// after it is deleted and scanned again, while preserving existing IDs.
	var existingID int64
	err = tx.QueryRowContext(ctx, "SELECT id FROM wechat_accounts WHERE openid=?", openid).Scan(&existingID)
	if err == nil {
		_, err = tx.ExecContext(ctx,
			`UPDATE wechat_accounts SET login_buffer=?, alias=?, nickname=?, avatar=?, user_info=?, credentials=?, status=?, updated_at=? WHERE id=?`,
			loginBuffer, nullableString(alias), nullableString(nickname), nullableString(avatar),
			userJSON, credJSON, nullableString(status), now, existingID,
		)
	} else if errors.Is(err, sql.ErrNoRows) {
		rows, queryErr := tx.QueryContext(ctx, "SELECT id FROM wechat_accounts ORDER BY id")
		if queryErr != nil {
			return nil, queryErr
		}
		nextID := int64(1)
		for rows.Next() {
			var id int64
			if scanErr := rows.Scan(&id); scanErr != nil {
				_ = rows.Close()
				return nil, scanErr
			}
			if id < nextID {
				continue
			}
			if id == nextID {
				nextID++
				continue
			}
			break
		}
		if queryErr = rows.Err(); queryErr != nil {
			_ = rows.Close()
			return nil, queryErr
		}
		_ = rows.Close()
		_, err = tx.ExecContext(ctx,
			`INSERT INTO wechat_accounts
			(id, openid, login_buffer, alias, nickname, avatar, user_info, credentials, status, created_at, updated_at)
			VALUES(?,?,?,?,?,?,?,?,?,?,?)`,
			nextID, openid, loginBuffer, nullableString(alias), nullableString(nickname), nullableString(avatar),
			userJSON, credJSON, nullableString(status), now, now,
		)
	} else {
		return nil, err
	}
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return db.GetAccountByOpenID(ctx, openid)
}

func (db *DB) GetAccount(ctx context.Context, id int64) (*WechatAccount, error) {
	return db.scanAccount(db.sql.QueryRowContext(ctx, selectAccountSQL+" WHERE id=?", id))
}

func (db *DB) GetAccountByOpenID(ctx context.Context, openid string) (*WechatAccount, error) {
	return db.scanAccount(db.sql.QueryRowContext(ctx, selectAccountSQL+" WHERE openid=?", openid))
}

func (db *DB) GetAccountByUIN(ctx context.Context, uin int64) (*WechatAccount, error) {
	return db.scanAccount(db.sql.QueryRowContext(ctx, selectAccountSQL+" WHERE uin=?", uin))
}

func (db *DB) ResolveAccount(ctx context.Context, ref string) (*WechatAccount, error) {
	if ref == "" {
		return nil, sql.ErrNoRows
	}
	if isDigits(ref) {
		n, _ := strconv.ParseInt(ref, 10, 64)
		if acc, err := db.GetAccountByUIN(ctx, n); err == nil {
			return acc, nil
		} else if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		return db.GetAccount(ctx, n)
	}
	return db.GetAccountByOpenID(ctx, ref)
}

func (db *DB) ListAccounts(ctx context.Context) ([]*WechatAccount, error) {
	rows, err := db.sql.QueryContext(ctx, selectAccountSQL+" ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*WechatAccount
	for rows.Next() {
		acc, err := scanAccountRows(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, acc)
	}
	return out, rows.Err()
}

// IsIncomplete reports the narrow class of records that can only have come
// from an interrupted/legacy scan. An account with credentials, or one that
// was explicitly marked alive/expired, is always protected from cleanup.
func (a *WechatAccount) IsIncomplete() bool {
	if a == nil || strings.TrimSpace(a.OpenID) == "" {
		return true
	}
	status := ""
	if a.Status != nil {
		status = strings.ToLower(strings.TrimSpace(*a.Status))
	}
	if status == "alive" || status == "expired" {
		return false
	}
	return strings.TrimSpace(a.LoginBuffer) == "" && len(a.Credentials) == 0
}

// ListIncompleteAccounts returns only conservative cleanup candidates. It is
// intentionally separate from ListAccounts so normal account APIs never hide
// a record merely because its credentials are currently unavailable.
func (db *DB) ListIncompleteAccounts(ctx context.Context) ([]*WechatAccount, error) {
	accounts, err := db.ListAccounts(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]*WechatAccount, 0)
	for _, account := range accounts {
		if account.IsIncomplete() {
			out = append(out, account)
		}
	}
	return out, nil
}

// DeleteIncompleteAccounts deletes only IDs that are still incomplete at the
// time of the delete. Re-reading inside the transaction prevents a successful
// scan that reused the same ID between preview and confirmation from being
// accidentally removed.
func (db *DB) DeleteIncompleteAccounts(ctx context.Context, ids []int64) ([]int64, error) {
	if len(ids) == 0 {
		return []int64{}, nil
	}
	tx, err := db.sql.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()
	removed := make([]int64, 0, len(ids))
	seen := make(map[int64]struct{}, len(ids))
	for _, id := range ids {
		if id <= 0 {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		account, scanErr := scanAccountRows(tx.QueryRowContext(ctx, selectAccountSQL+" WHERE id=?", id))
		if errors.Is(scanErr, sql.ErrNoRows) {
			continue
		}
		if scanErr != nil {
			return nil, scanErr
		}
		if !account.IsIncomplete() {
			continue
		}
		result, deleteErr := tx.ExecContext(ctx, "DELETE FROM wechat_accounts WHERE id=?", id)
		if deleteErr != nil {
			return nil, deleteErr
		}
		if affected, _ := result.RowsAffected(); affected == 1 {
			removed = append(removed, id)
		}
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return removed, nil
}

// CompactAccountIDs moves existing accounts into the lowest contiguous IDs,
// preserving their current order. All YYB-owned child tables are remapped in
// the same transaction. The caller is responsible for remapping account
// ownership kept in the separate auth database.
func (db *DB) CompactAccountIDs(ctx context.Context) (map[int64]int64, error) {
	tx, err := db.sql.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()
	// Foreign keys are checked at commit, allowing the parent and child IDs to
	// be moved through a collision-free temporary namespace.
	if _, err = tx.ExecContext(ctx, "PRAGMA defer_foreign_keys=ON"); err != nil {
		return nil, err
	}
	rows, err := tx.QueryContext(ctx, "SELECT id FROM wechat_accounts ORDER BY id")
	if err != nil {
		return nil, err
	}
	var oldIDs []int64
	for rows.Next() {
		var id int64
		if err = rows.Scan(&id); err != nil {
			_ = rows.Close()
			return nil, err
		}
		oldIDs = append(oldIDs, id)
	}
	if err = rows.Err(); err != nil {
		_ = rows.Close()
		return nil, err
	}
	_ = rows.Close()
	mapping := make(map[int64]int64, len(oldIDs))
	for index, oldID := range oldIDs {
		newID := int64(index + 1)
		if oldID != newID {
			mapping[oldID] = newID
		}
	}
	if len(mapping) == 0 {
		return map[int64]int64{}, tx.Commit()
	}
	// Move every affected parent row to a negative temporary ID first. IDs are
	// positive in normal operation, so this namespace cannot collide.
	for oldID := range mapping {
		if _, err = tx.ExecContext(ctx, "UPDATE wechat_accounts SET id=? WHERE id=?", -oldID, oldID); err != nil {
			return nil, err
		}
	}
	childTables := []struct{ table, column string }{
		{"sessions", "wechat_account_id"},
		{"account_script_jobs", "account_id"},
		{"account_push_settings", "account_id"},
		{"account_proxy_settings", "account_id"},
	}
	for oldID := range mapping {
		for _, child := range childTables {
			if _, err = tx.ExecContext(ctx, "UPDATE "+child.table+" SET "+child.column+"=? WHERE "+child.column+"=?", -oldID, oldID); err != nil {
				return nil, err
			}
		}
	}
	for oldID, newID := range mapping {
		if _, err = tx.ExecContext(ctx, "UPDATE wechat_accounts SET id=? WHERE id=?", newID, -oldID); err != nil {
			return nil, err
		}
		for _, child := range childTables {
			if _, err = tx.ExecContext(ctx, "UPDATE "+child.table+" SET "+child.column+"=? WHERE "+child.column+"=?", newID, -oldID); err != nil {
				return nil, err
			}
		}
	}
	// Keep SQLite's AUTOINCREMENT metadata aligned for any external inserts.
	_, _ = tx.ExecContext(ctx, "UPDATE sqlite_sequence SET seq=? WHERE name='wechat_accounts'", int64(len(oldIDs)))
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return mapping, nil
}

func (db *DB) SetAccountUIN(ctx context.Context, id, uin int64) error {
	_, err := db.sql.ExecContext(ctx, "UPDATE wechat_accounts SET uin=?, updated_at=? WHERE id=?", uin, time.Now().Unix(), id)
	return err
}

func (db *DB) SetAccountProfile(ctx context.Context, id int64, nickname, avatar *string, userInfo map[string]any) error {
	userJSON, err := marshalNullable(userInfo)
	if err != nil {
		return err
	}
	_, err = db.sql.ExecContext(ctx,
		"UPDATE wechat_accounts SET nickname=?, avatar=?, user_info=?, updated_at=? WHERE id=?",
		nullableString(nickname), nullableString(avatar), userJSON, time.Now().Unix(), id,
	)
	return err
}

func (db *DB) SetAccountRemark(ctx context.Context, id int64, remark string) error {
	remark = strings.TrimSpace(remark)
	var value any
	if remark != "" {
		value = remark
	}
	_, err := db.sql.ExecContext(ctx, "UPDATE wechat_accounts SET remark=?, updated_at=? WHERE id=?", value, time.Now().Unix(), id)
	return err
}

func (db *DB) GetSetting(ctx context.Context, key string) (string, error) {
	var value string
	err := db.sql.QueryRowContext(ctx, "SELECT value FROM app_settings WHERE key=?", key).Scan(&value)
	return value, err
}

func (db *DB) SetSetting(ctx context.Context, key, value string) error {
	_, err := db.sql.ExecContext(ctx, `
INSERT INTO app_settings(key, value, updated_at) VALUES(?,?,?)
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at`, key, value, time.Now().Unix())
	return err
}

func (db *DB) SetAccountCredential(ctx context.Context, id int64, loginBuffer string, credentials map[string]any) error {
	credJSON, err := marshalNullable(credentials)
	if err != nil {
		return err
	}
	_, err = db.sql.ExecContext(ctx,
		"UPDATE wechat_accounts SET login_buffer=?, credentials=?, updated_at=? WHERE id=?",
		loginBuffer, credJSON, time.Now().Unix(), id,
	)
	return err
}

func (db *DB) SetAccountCredentialStatus(ctx context.Context, id int64, loginBuffer string, credentials map[string]any, status string) error {
	credJSON, err := marshalNullable(credentials)
	if err != nil {
		return err
	}
	now := time.Now().Unix()
	_, err = db.sql.ExecContext(ctx,
		"UPDATE wechat_accounts SET login_buffer=?, credentials=?, status=?, last_checked_at=?, updated_at=? WHERE id=?",
		loginBuffer, credJSON, status, now, now, id,
	)
	return err
}

func (db *DB) SetAccountStatus(ctx context.Context, id int64, status string) error {
	now := time.Now().Unix()
	_, err := db.sql.ExecContext(ctx,
		"UPDATE wechat_accounts SET status=?, last_checked_at=?, updated_at=? WHERE id=?",
		status, now, now, id,
	)
	return err
}

func (db *DB) DeleteAccount(ctx context.Context, id int64) error {
	_, err := db.sql.ExecContext(ctx, "DELETE FROM wechat_accounts WHERE id=?", id)
	return err
}

func (db *DB) GetSession(ctx context.Context, accountID int64, tcpProxy string) (*SessionRow, error) {
	row := db.sql.QueryRowContext(ctx,
		"SELECT id, wechat_account_id, uin, tcp_proxy, session_blob, expires_at, created_at, updated_at FROM sessions WHERE wechat_account_id=? AND tcp_proxy=? AND expires_at>?",
		accountID, tcpProxy, time.Now().Unix(),
	)
	return scanSession(row)
}

func (db *DB) PutSession(ctx context.Context, accountID int64, uin *int64, sessionBlob map[string]any, expiresAt int64, tcpProxy string) error {
	now := time.Now().Unix()
	blob, err := json.Marshal(sessionBlob)
	if err != nil {
		return err
	}
	_, err = db.sql.ExecContext(ctx,
		`INSERT INTO sessions
		(wechat_account_id, uin, tcp_proxy, session_blob, expires_at, created_at, updated_at)
		VALUES(?,?,?,?,?,?,?)
		ON CONFLICT(wechat_account_id, tcp_proxy) DO UPDATE SET
		uin=excluded.uin, session_blob=excluded.session_blob,
		expires_at=excluded.expires_at, updated_at=excluded.updated_at`,
		accountID, nullableInt(uin), tcpProxy, string(blob), expiresAt, now, now,
	)
	return err
}

func (db *DB) InvalidateSession(ctx context.Context, accountID int64, tcpProxy string) error {
	_, err := db.sql.ExecContext(ctx, "DELETE FROM sessions WHERE wechat_account_id=? AND tcp_proxy=?", accountID, tcpProxy)
	return err
}

func (db *DB) InvalidateAccountSessions(ctx context.Context, accountID int64) error {
	_, err := db.sql.ExecContext(ctx, "DELETE FROM sessions WHERE wechat_account_id=?", accountID)
	return err
}

func (db *DB) PurgeExpiredSessions(ctx context.Context) (int64, error) {
	res, err := db.sql.ExecContext(ctx, "DELETE FROM sessions WHERE expires_at<=?", time.Now().Unix())
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

func (db *DB) ListFeatures(ctx context.Context, onlyEnabled bool) ([]Feature, error) {
	sqlText := "SELECT code, name, description, enabled FROM features"
	if onlyEnabled {
		sqlText += " WHERE enabled=1"
	}
	sqlText += " ORDER BY code"
	rows, err := db.sql.QueryContext(ctx, sqlText)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Feature
	for rows.Next() {
		f, err := scanFeature(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, rows.Err()
}

func (db *DB) ResolveFeature(ctx context.Context, ref any) (*Feature, error) {
	switch v := ref.(type) {
	case float64:
		return db.GetFeature(ctx, int(v))
	case int:
		return db.GetFeature(ctx, v)
	case string:
		if isDigits(v) {
			n, _ := strconv.Atoi(v)
			return db.GetFeature(ctx, n)
		}
		return db.GetFeatureByName(ctx, v)
	default:
		return nil, sql.ErrNoRows
	}
}

func (db *DB) GetFeature(ctx context.Context, code int) (*Feature, error) {
	row := db.sql.QueryRowContext(ctx, "SELECT code, name, description, enabled FROM features WHERE code=?", code)
	f, err := scanFeature(row)
	if err != nil {
		return nil, err
	}
	return &f, nil
}

func (db *DB) GetFeatureByName(ctx context.Context, name string) (*Feature, error) {
	row := db.sql.QueryRowContext(ctx, "SELECT code, name, description, enabled FROM features WHERE name=? COLLATE NOCASE", name)
	f, err := scanFeature(row)
	if err != nil {
		return nil, err
	}
	return &f, nil
}

func (a *WechatAccount) Public() AccountPublic {
	var refreshTokenObservedAt *int64
	rescanRecommended := false
	var credentialExpiresAt *int64
	credentialExpiresIn := int64(0)
	credentialExpiresAtValue := int64Credential(a.Credentials, "expires_at")
	credentialExpiresIn = int64Credential(a.Credentials, "expires_in")
	if credentialExpiresIn <= 0 {
		credentialExpiresIn = 7200
	}
	if stringCredential(a.Credentials, "refreshtoken") != "" {
		if observedAt := int64Credential(a.Credentials, "refresh_token_observed_at"); observedAt > 0 {
			refreshTokenObservedAt = &observedAt
			rescanRecommended = time.Now().Unix()-observedAt >= int64((25*24*time.Hour)/time.Second)
		}
	}
	if credentialExpiresAtValue <= 0 {
		if refreshedAt := int64Credential(a.Credentials, "refresh_refreshed_at"); refreshedAt > 0 {
			credentialExpiresAtValue = refreshedAt + credentialExpiresIn
		}
	}
	if credentialExpiresAtValue > 0 {
		expiresAt := credentialExpiresAtValue
		credentialExpiresAt = &expiresAt
	}
	return AccountPublic{
		ID:                     a.ID,
		OpenID:                 a.OpenID,
		UIN:                    a.UIN,
		Alias:                  a.Alias,
		Nickname:               a.Nickname,
		Remark:                 a.Remark,
		Avatar:                 a.Avatar,
		Status:                 a.Status,
		RefreshTokenObservedAt: refreshTokenObservedAt,
		CredentialExpiresAt:    credentialExpiresAt,
		CredentialExpiresIn:    credentialExpiresIn,
		RescanRecommended:      rescanRecommended,
		LastCheckedAt:          a.LastCheckedAt,
		CreatedAt:              a.CreatedAt,
		UpdatedAt:              a.UpdatedAt,
	}
}

func stringCredential(values map[string]any, key string) string {
	value, _ := values[key].(string)
	return value
}

func int64Credential(values map[string]any, key string) int64 {
	switch value := values[key].(type) {
	case int64:
		return value
	case int:
		return int64(value)
	case float64:
		return int64(value)
	case json.Number:
		result, _ := value.Int64()
		return result
	default:
		return 0
	}
}

const selectAccountSQL = `SELECT id, openid, uin, alias, nickname, remark, avatar, user_info, login_buffer, credentials, status, last_checked_at, created_at, updated_at FROM wechat_accounts`

type accountScanner interface {
	Scan(dest ...any) error
}

type featureScanner interface {
	Scan(dest ...any) error
}

func (db *DB) scanAccount(row accountScanner) (*WechatAccount, error) {
	return scanAccountRows(row)
}

func scanAccountRows(row accountScanner) (*WechatAccount, error) {
	var (
		a                               WechatAccount
		uin, lastChecked                sql.NullInt64
		alias, nickname, remark, avatar sql.NullString
		userJSON, credJSON              sql.NullString
		status                          sql.NullString
	)
	err := row.Scan(
		&a.ID, &a.OpenID, &uin, &alias, &nickname, &remark, &avatar, &userJSON,
		&a.LoginBuffer, &credJSON, &status, &lastChecked, &a.CreatedAt, &a.UpdatedAt,
	)
	if err != nil {
		return nil, err
	}
	if uin.Valid {
		a.UIN = &uin.Int64
	}
	a.Alias = stringPtrFromNull(alias)
	a.Nickname = stringPtrFromNull(nickname)
	a.Remark = stringPtrFromNull(remark)
	a.Avatar = stringPtrFromNull(avatar)
	a.Status = stringPtrFromNull(status)
	if lastChecked.Valid {
		a.LastCheckedAt = &lastChecked.Int64
	}
	if userJSON.Valid && userJSON.String != "" {
		_ = json.Unmarshal([]byte(userJSON.String), &a.UserInfo)
	}
	if credJSON.Valid && credJSON.String != "" {
		_ = json.Unmarshal([]byte(credJSON.String), &a.Credentials)
	}
	return &a, nil
}

func scanSession(row accountScanner) (*SessionRow, error) {
	var s SessionRow
	var uin sql.NullInt64
	var blob string
	if err := row.Scan(&s.ID, &s.WechatAccountID, &uin, &s.TCPProxy, &blob, &s.ExpiresAt, &s.CreatedAt, &s.UpdatedAt); err != nil {
		return nil, err
	}
	if uin.Valid {
		s.UIN = &uin.Int64
	}
	if err := json.Unmarshal([]byte(blob), &s.SessionBlob); err != nil {
		return nil, fmt.Errorf("decode session_blob: %w", err)
	}
	return &s, nil
}

func scanFeature(row featureScanner) (Feature, error) {
	var f Feature
	var desc sql.NullString
	var enabled int
	if err := row.Scan(&f.Code, &f.Name, &desc, &enabled); err != nil {
		return Feature{}, err
	}
	f.Description = stringPtrFromNull(desc)
	f.Enabled = enabled != 0
	return f, nil
}

func marshalNullable(v map[string]any) (sql.NullString, error) {
	if v == nil {
		return sql.NullString{}, nil
	}
	b, err := json.Marshal(v)
	if err != nil {
		return sql.NullString{}, err
	}
	return sql.NullString{String: string(b), Valid: true}, nil
}

func nullableString(s *string) sql.NullString {
	if s == nil {
		return sql.NullString{}
	}
	return sql.NullString{String: *s, Valid: true}
}

func nullableInt(v *int64) sql.NullInt64 {
	if v == nil {
		return sql.NullInt64{}
	}
	return sql.NullInt64{Int64: *v, Valid: true}
}

func stringPtrFromNull(v sql.NullString) *string {
	if !v.Valid {
		return nil
	}
	return &v.String
}

func stringPtr(s string) *string { return &s }

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}
