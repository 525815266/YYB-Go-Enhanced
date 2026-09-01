package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "github.com/go-sql-driver/mysql"
	"golang.org/x/crypto/bcrypt"
	_ "modernc.org/sqlite"
)

const mysqlSchema = `
CREATE TABLE IF NOT EXISTS users (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('admin','user') NOT NULL DEFAULT 'user',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    login_count BIGINT NOT NULL DEFAULT 0,
    last_login_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_sessions (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    token_hash BINARY(32) NOT NULL UNIQUE,
    user_agent VARCHAR(255) NOT NULL DEFAULT '',
    ip_address VARCHAR(64) NOT NULL DEFAULT '',
    expires_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    last_seen_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_user_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_user_sessions_user (user_id),
    INDEX idx_user_sessions_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS auth_settings (
    setting_key VARCHAR(64) NOT NULL PRIMARY KEY,
    setting_value VARCHAR(255) NOT NULL,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS account_owners (
    account_id BIGINT NOT NULL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    claimed_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_account_owners_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_account_owners_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
`

const sqliteSchema = `
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    login_count INTEGER NOT NULL DEFAULT 0,
    last_login_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash BLOB NOT NULL UNIQUE,
    user_agent TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    expires_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions(expires_at);

CREATE TABLE IF NOT EXISTS auth_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS account_owners (
    account_id INTEGER NOT NULL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    claimed_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_owners_user ON account_owners(user_id);
`

var ErrInvalidCredentials = errors.New("用户名或密码错误")

type Store struct {
	db     *sql.DB
	driver string
}

type User struct {
	ID          int64      `json:"id"`
	Username    string     `json:"username"`
	DisplayName string     `json:"display_name"`
	Role        string     `json:"role"`
	Enabled     bool       `json:"enabled"`
	LoginCount  int64      `json:"login_count"`
	LastLoginAt *time.Time `json:"last_login_at"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   time.Time  `json:"updated_at"`
}

type Session struct {
	ID         int64     `json:"id"`
	UserID     int64     `json:"user_id"`
	UserAgent  string    `json:"user_agent"`
	IPAddress  string    `json:"ip_address"`
	ExpiresAt  time.Time `json:"expires_at"`
	CreatedAt  time.Time `json:"created_at"`
	LastSeenAt time.Time `json:"last_seen_at"`
}

func Open(ctx context.Context, driver, dsn string) (*Store, error) {
	driver = strings.ToLower(strings.TrimSpace(driver))
	if driver != "mysql" && driver != "sqlite" {
		return nil, fmt.Errorf("unsupported auth database driver %q", driver)
	}
	db, err := sql.Open(driver, dsn)
	if err != nil {
		return nil, err
	}
	if driver == "sqlite" {
		db.SetMaxOpenConns(1)
	} else {
		db.SetMaxOpenConns(10)
		db.SetMaxIdleConns(5)
		db.SetConnMaxLifetime(30 * time.Minute)
	}
	if err = db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("connect auth %s: %w", driver, err)
	}
	schema := mysqlSchema
	if driver == "sqlite" {
		schema = sqliteSchema
		if _, err = db.ExecContext(ctx, "PRAGMA foreign_keys = ON"); err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("configure auth sqlite: %w", err)
		}
	}
	for _, statement := range strings.Split(schema, ";") {
		statement = strings.TrimSpace(statement)
		if statement == "" {
			continue
		}
		if _, err = db.ExecContext(ctx, statement); err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("migrate auth %s: %w", driver, err)
		}
	}
	if err := ensureLoginCountColumn(ctx, db, driver); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("migrate auth login count: %w", err)
	}
	return &Store{db: db, driver: driver}, nil
}

func ensureLoginCountColumn(ctx context.Context, db *sql.DB, driver string) error {
	if driver == "sqlite" {
		rows, err := db.QueryContext(ctx, "PRAGMA table_info(users)")
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
			if name == "login_count" {
				return nil
			}
		}
		if err := rows.Err(); err != nil {
			return err
		}
		_, err = db.ExecContext(ctx, "ALTER TABLE users ADD COLUMN login_count INTEGER NOT NULL DEFAULT 0")
		return err
	}
	_, err := db.ExecContext(ctx, "ALTER TABLE users ADD COLUMN login_count BIGINT NOT NULL DEFAULT 0")
	if err != nil && !strings.Contains(strings.ToLower(err.Error()), "duplicate") {
		return err
	}
	return nil
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) BootstrapAdmin(ctx context.Context, username, password string) error {
	username = normalizeUsername(username)
	if password == "" {
		return nil
	}
	if username == "" {
		return errors.New("首次管理员用户名和密码不能为空")
	}
	var count int
	if err := s.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM users").Scan(&count); err != nil {
		return err
	}
	if count > 0 {
		return nil
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), 12)
	if err != nil {
		return err
	}
	now := time.Now().UTC()
	_, err = s.db.ExecContext(ctx, `INSERT INTO users
        (username, display_name, password_hash, role, enabled, created_at, updated_at)
        VALUES (?, ?, ?, 'admin', TRUE, ?, ?)`, username, username, string(hash), now, now)
	return err
}

// RegisterUser promotes only the first registered account to administrator.
// The role decision and insert happen in one statement, so concurrent requests
// cannot both observe an empty users table.
func (s *Store) RegisterUser(ctx context.Context, username, displayName, password string) (*User, error) {
	username = normalizeUsername(username)
	displayName = strings.TrimSpace(displayName)
	if err := ValidateUsername(username); err != nil {
		return nil, err
	}
	if displayName == "" || len([]rune(displayName)) > 100 {
		return nil, errors.New("显示名长度应为 1-100 个字符")
	}
	if err := ValidatePassword(password); err != nil {
		return nil, err
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), 12)
	if err != nil {
		return nil, err
	}
	now := time.Now().UTC()
	result, err := s.db.ExecContext(ctx, `INSERT INTO users
        (username, display_name, password_hash, role, enabled, created_at, updated_at)
        VALUES (?, ?, ?, CASE WHEN (SELECT COUNT(*) FROM users)=0 THEN 'admin' ELSE 'user' END, TRUE, ?, ?)`,
		username, displayName, string(hash), now, now)
	if err != nil {
		if isDuplicateError(err) {
			return nil, errors.New("用户名已存在")
		}
		return nil, err
	}
	id, _ := result.LastInsertId()
	return s.GetUser(ctx, id)
}

func (s *Store) CreateUser(ctx context.Context, username, displayName, password, role string) (*User, error) {
	username = normalizeUsername(username)
	displayName = strings.TrimSpace(displayName)
	if err := ValidateUsername(username); err != nil {
		return nil, err
	}
	if displayName == "" || len([]rune(displayName)) > 100 {
		return nil, errors.New("显示名长度应为 1-100 个字符")
	}
	if err := ValidatePassword(password); err != nil {
		return nil, err
	}
	if role != "admin" {
		role = "user"
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), 12)
	if err != nil {
		return nil, err
	}
	now := time.Now().UTC()
	result, err := s.db.ExecContext(ctx, `INSERT INTO users
        (username, display_name, password_hash, role, enabled, created_at, updated_at)
        VALUES (?, ?, ?, ?, TRUE, ?, ?)`, username, displayName, string(hash), role, now, now)
	if err != nil {
		if isDuplicateError(err) {
			return nil, errors.New("用户名已存在")
		}
		return nil, err
	}
	id, _ := result.LastInsertId()
	return s.GetUser(ctx, id)
}

func (s *Store) Authenticate(ctx context.Context, username, password string) (*User, error) {
	var user User
	var hash string
	err := s.db.QueryRowContext(ctx, `SELECT id, username, display_name, password_hash, role, enabled, login_count,
        last_login_at, created_at, updated_at FROM users WHERE username=?`, normalizeUsername(username)).Scan(
		&user.ID, &user.Username, &user.DisplayName, &hash, &user.Role, &user.Enabled,
		&user.LoginCount,
		&user.LastLoginAt, &user.CreatedAt, &user.UpdatedAt,
	)
	if err != nil || bcrypt.CompareHashAndPassword([]byte(hash), []byte(password)) != nil {
		return nil, ErrInvalidCredentials
	}
	if !user.Enabled {
		return nil, errors.New("账号已停用，请联系管理员")
	}
	now := time.Now().UTC()
	_, _ = s.db.ExecContext(ctx, "UPDATE users SET last_login_at=?, login_count=login_count+1, updated_at=? WHERE id=?", now, now, user.ID)
	user.LoginCount++
	user.LastLoginAt = &now
	return &user, nil
}

func (s *Store) CreateSession(ctx context.Context, userID int64, userAgent, ip string, ttl time.Duration) (string, *Session, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", nil, err
	}
	token := base64.RawURLEncoding.EncodeToString(raw)
	hash := sha256.Sum256([]byte(token))
	now := time.Now().UTC()
	expires := now.Add(ttl)
	result, err := s.db.ExecContext(ctx, `INSERT INTO user_sessions
        (user_id, token_hash, user_agent, ip_address, expires_at, created_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)`, userID, hash[:], trimTo(userAgent, 255), trimTo(ip, 64), expires, now, now)
	if err != nil {
		return "", nil, err
	}
	id, _ := result.LastInsertId()
	return token, &Session{ID: id, UserID: userID, UserAgent: trimTo(userAgent, 255), IPAddress: trimTo(ip, 64), ExpiresAt: expires, CreatedAt: now, LastSeenAt: now}, nil
}

func (s *Store) UserBySession(ctx context.Context, token string) (*User, *Session, error) {
	hash := sha256.Sum256([]byte(token))
	var user User
	var session Session
	err := s.db.QueryRowContext(ctx, `SELECT u.id, u.username, u.display_name, u.role, u.enabled,
        u.last_login_at, u.created_at, u.updated_at,
        s.id, s.user_id, s.user_agent, s.ip_address, s.expires_at, s.created_at, s.last_seen_at
        FROM user_sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.expires_at>?`, hash[:], time.Now().UTC()).Scan(
		&user.ID, &user.Username, &user.DisplayName, &user.Role, &user.Enabled,
		&user.LastLoginAt, &user.CreatedAt, &user.UpdatedAt,
		&session.ID, &session.UserID, &session.UserAgent, &session.IPAddress, &session.ExpiresAt, &session.CreatedAt, &session.LastSeenAt,
	)
	if err != nil || !user.Enabled {
		return nil, nil, sql.ErrNoRows
	}
	if time.Since(session.LastSeenAt) > 5*time.Minute {
		_, _ = s.db.ExecContext(ctx, "UPDATE user_sessions SET last_seen_at=? WHERE id=?", time.Now().UTC(), session.ID)
	}
	return &user, &session, nil
}

func (s *Store) DeleteSession(ctx context.Context, token string) error {
	hash := sha256.Sum256([]byte(token))
	_, err := s.db.ExecContext(ctx, "DELETE FROM user_sessions WHERE token_hash=?", hash[:])
	return err
}

func (s *Store) DeleteOtherSessions(ctx context.Context, userID, keepID int64) error {
	_, err := s.db.ExecContext(ctx, "DELETE FROM user_sessions WHERE user_id=? AND id<>?", userID, keepID)
	return err
}

func (s *Store) ListSessions(ctx context.Context, userID int64) ([]Session, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id, user_id, user_agent, ip_address, expires_at, created_at, last_seen_at
        FROM user_sessions WHERE user_id=? AND expires_at>? ORDER BY last_seen_at DESC`, userID, time.Now().UTC())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Session
	for rows.Next() {
		var item Session
		if err := rows.Scan(&item.ID, &item.UserID, &item.UserAgent, &item.IPAddress, &item.ExpiresAt, &item.CreatedAt, &item.LastSeenAt); err != nil {
			return nil, err
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

func (s *Store) UpdateProfile(ctx context.Context, userID int64, displayName string) (*User, error) {
	displayName = strings.TrimSpace(displayName)
	if displayName == "" || len([]rune(displayName)) > 100 {
		return nil, errors.New("显示名长度应为 1-100 个字符")
	}
	_, err := s.db.ExecContext(ctx, "UPDATE users SET display_name=?, updated_at=? WHERE id=?", displayName, time.Now().UTC(), userID)
	if err != nil {
		return nil, err
	}
	return s.GetUser(ctx, userID)
}

func (s *Store) ChangePassword(ctx context.Context, userID int64, current, next string, sessionID int64) error {
	if err := ValidatePassword(next); err != nil {
		return err
	}
	var hash string
	if err := s.db.QueryRowContext(ctx, "SELECT password_hash FROM users WHERE id=?", userID).Scan(&hash); err != nil {
		return err
	}
	if bcrypt.CompareHashAndPassword([]byte(hash), []byte(current)) != nil {
		return errors.New("当前密码错误")
	}
	nextHash, err := bcrypt.GenerateFromPassword([]byte(next), 12)
	if err != nil {
		return err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, "UPDATE users SET password_hash=?, updated_at=? WHERE id=?", string(nextHash), time.Now().UTC(), userID); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM user_sessions WHERE user_id=? AND id<>?", userID, sessionID); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) ListUsers(ctx context.Context) ([]User, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id, username, display_name, role, enabled, login_count, last_login_at, created_at, updated_at FROM users ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []User
	for rows.Next() {
		var u User
		if err := rows.Scan(&u.ID, &u.Username, &u.DisplayName, &u.Role, &u.Enabled, &u.LoginCount, &u.LastLoginAt, &u.CreatedAt, &u.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, u)
	}
	return out, rows.Err()
}

func (s *Store) GetUser(ctx context.Context, id int64) (*User, error) {
	var u User
	err := s.db.QueryRowContext(ctx, `SELECT id, username, display_name, role, enabled, login_count, last_login_at, created_at, updated_at FROM users WHERE id=?`, id).Scan(&u.ID, &u.Username, &u.DisplayName, &u.Role, &u.Enabled, &u.LoginCount, &u.LastLoginAt, &u.CreatedAt, &u.UpdatedAt)
	return &u, err
}

func (s *Store) UpdateUser(ctx context.Context, actorID, id int64, role string, enabled bool) (*User, error) {
	if role != "admin" {
		role = "user"
	}
	if actorID == id && (!enabled || role != "admin") {
		return nil, errors.New("不能停用自己或移除自己的管理员权限")
	}
	if _, err := s.db.ExecContext(ctx, "UPDATE users SET role=?, enabled=?, updated_at=? WHERE id=?", role, enabled, time.Now().UTC(), id); err != nil {
		return nil, err
	}
	if !enabled {
		_, _ = s.db.ExecContext(ctx, "DELETE FROM user_sessions WHERE user_id=?", id)
	}
	return s.GetUser(ctx, id)
}

func (s *Store) AdminResetPassword(ctx context.Context, id int64, password string) error {
	if err := ValidatePassword(password); err != nil {
		return err
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), 12)
	if err != nil {
		return err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, "UPDATE users SET password_hash=?, updated_at=? WHERE id=?", string(hash), time.Now().UTC(), id); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM user_sessions WHERE user_id=?", id); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) DeleteUser(ctx context.Context, actorID, id int64) error {
	if actorID == id {
		return errors.New("不能删除当前登录用户")
	}
	result, err := s.db.ExecContext(ctx, "DELETE FROM users WHERE id=?", id)
	if err != nil {
		return err
	}
	affected, _ := result.RowsAffected()
	if affected == 0 {
		return sql.ErrNoRows
	}
	return nil
}

// ClaimAccount binds a YYB account to the user who added it. Existing claims
// are intentionally immutable so a re-scan cannot silently move an account.
func (s *Store) ClaimAccount(ctx context.Context, accountID, userID int64) error {
	now := time.Now().UTC()
	query := `INSERT INTO account_owners (account_id, user_id, claimed_at) VALUES (?, ?, ?)`
	if s.driver == "sqlite" {
		query = `INSERT OR IGNORE INTO account_owners (account_id, user_id, claimed_at) VALUES (?, ?, ?)`
	}
	_, err := s.db.ExecContext(ctx, query, accountID, userID, now)
	if err != nil && s.driver == "mysql" && isDuplicateError(err) {
		return nil
	}
	return err
}

func (s *Store) AccountOwner(ctx context.Context, accountID int64) (int64, error) {
	var userID int64
	err := s.db.QueryRowContext(ctx, "SELECT user_id FROM account_owners WHERE account_id=?", accountID).Scan(&userID)
	return userID, err
}

func (s *Store) AccountCounts(ctx context.Context) (map[int64]int, error) {
	rows, err := s.db.QueryContext(ctx, "SELECT user_id, COUNT(*) FROM account_owners GROUP BY user_id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	counts := map[int64]int{}
	for rows.Next() {
		var userID int64
		var count int
		if err := rows.Scan(&userID, &count); err != nil {
			return nil, err
		}
		counts[userID] = count
	}
	return counts, rows.Err()
}

// AccountIDs returns the YYB account IDs owned by a user. Ownership is kept
// separate from account credentials so the HTTP layer can scope account work
// to the signed-in user.
func (s *Store) AccountIDs(ctx context.Context, userID int64) (map[int64]struct{}, error) {
	rows, err := s.db.QueryContext(ctx, "SELECT account_id FROM account_owners WHERE user_id=?", userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := make(map[int64]struct{})
	for rows.Next() {
		var accountID int64
		if err := rows.Scan(&accountID); err != nil {
			return nil, err
		}
		ids[accountID] = struct{}{}
	}
	return ids, rows.Err()
}

// RemapAccountIDs updates ownership records after the YYB account database
// compacts its primary keys. Ownership lives in a separate database, so this
// operation is intentionally explicit and collision-safe.
func (s *Store) RemapAccountIDs(ctx context.Context, mapping map[int64]int64) error {
	if len(mapping) == 0 {
		return nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	for oldID := range mapping {
		if _, err = tx.ExecContext(ctx, "UPDATE account_owners SET account_id=? WHERE account_id=?", -oldID, oldID); err != nil {
			return err
		}
	}
	for oldID, newID := range mapping {
		if _, err = tx.ExecContext(ctx, "UPDATE account_owners SET account_id=? WHERE account_id=?", newID, -oldID); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *Store) RegistrationEnabled(ctx context.Context) (bool, error) {
	var value string
	err := s.db.QueryRowContext(ctx, "SELECT setting_value FROM auth_settings WHERE setting_key='registration_enabled'").Scan(&value)
	if errors.Is(err, sql.ErrNoRows) {
		return true, nil
	}
	return value == "true", err
}

func (s *Store) SetRegistrationEnabled(ctx context.Context, enabled bool) error {
	value := "false"
	if enabled {
		value = "true"
	}
	query := `INSERT INTO auth_settings (setting_key, setting_value, updated_at) VALUES ('registration_enabled', ?, ?)
        ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value), updated_at=VALUES(updated_at)`
	if s.driver == "sqlite" {
		query = `INSERT INTO auth_settings (setting_key, setting_value, updated_at) VALUES ('registration_enabled', ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at`
	}
	_, err := s.db.ExecContext(ctx, query, value, time.Now().UTC())
	return err
}

func ValidateUsername(value string) error {
	if len(value) < 3 || len(value) > 64 {
		return errors.New("用户名长度应为 3-64 个字符")
	}
	for _, r := range value {
		if !(r >= 'a' && r <= 'z') && !(r >= '0' && r <= '9') && r != '_' && r != '-' && r != '.' {
			return errors.New("用户名只能包含小写字母、数字、点、下划线和连字符")
		}
	}
	return nil
}

func ValidatePassword(value string) error {
	if len(value) < 10 || len(value) > 128 {
		return errors.New("密码长度应为 10-128 个字符")
	}
	return nil
}

func normalizeUsername(value string) string { return strings.ToLower(strings.TrimSpace(value)) }
func isDuplicateError(err error) bool {
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "duplicate") || strings.Contains(message, "unique constraint")
}
func trimTo(value string, max int) string {
	if len(value) > max {
		return value[:max]
	}
	return value
}
