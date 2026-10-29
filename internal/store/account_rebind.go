package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"time"
)

var ErrAccountIdentityConflict = errors.New("scan identity already belongs to another account")
var ErrAccountLinkChanged = errors.New("update link expired or target identity changed")

// UpdateAccountFromLink replaces the authorization in place. Link consumption,
// identity uniqueness and session removal commit together, or all roll back.
// Relations keyed by account ID (owner, proxies, jobs, push) are preserved.
func (db *DB) UpdateAccountFromLink(ctx context.Context, link *AccountLink, openID, buffer string, nickname, avatar *string, userInfo, credentials map[string]any) (*WechatAccount, error) {
	if link == nil || link.Kind != "update" || strings.TrimSpace(openID) == "" || buffer == "" {
		return nil, errors.New("invalid account replacement")
	}
	userJSON, err := marshalNullable(userInfo)
	if err != nil {
		return nil, err
	}
	credJSON, err := marshalNullable(credentials)
	if err != nil {
		return nil, err
	}
	tx, err := db.sql.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	now := time.Now().Unix()
	var current string
	err = tx.QueryRowContext(ctx, `SELECT a.openid FROM wechat_accounts a JOIN account_links l ON l.account_id=a.id
WHERE l.id=? AND l.token_hash=? AND l.kind='update' AND a.id=? AND a.openid=? AND l.expected_openid=?
AND l.used_at IS NULL AND l.revoked_at IS NULL AND l.expires_at>?`, link.ID, link.TokenHash, link.AccountID, link.ExpectedOpenID, link.ExpectedOpenID, now).Scan(&current)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrAccountLinkChanged
	}
	if err != nil {
		return nil, err
	}
	var duplicate int64
	err = tx.QueryRowContext(ctx, "SELECT id FROM wechat_accounts WHERE openid=? AND id<>?", openID, link.AccountID).Scan(&duplicate)
	if err == nil {
		return nil, ErrAccountIdentityConflict
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	if openID != current {
		var encoded string
		err = tx.QueryRowContext(ctx, "SELECT value FROM app_settings WHERE key=?", accountReferenceKey(link.AccountID)).Scan(&encoded)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		var previous []string
		if encoded != "" {
			if err = json.Unmarshal([]byte(encoded), &previous); err != nil {
				return nil, err
			}
		}
		previous = append(previous, current)
		data, _ := json.Marshal(previous)
		if _, err = tx.ExecContext(ctx, `INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at`, accountReferenceKey(link.AccountID), string(data), now); err != nil {
			return nil, err
		}
	}
	_, err = tx.ExecContext(ctx, `UPDATE wechat_accounts SET openid=?,login_buffer=?,credentials=?,nickname=?,alias=?,avatar=?,user_info=?,
uin=NULL,status='alive',last_checked_at=?,updated_at=? WHERE id=?`, openID, buffer, credJSON, nullableString(nickname), nullableString(nickname), nullableString(avatar), userJSON, now, now, link.AccountID)
	if err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM sessions WHERE wechat_account_id=?", link.AccountID); err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM account_links WHERE account_id=? AND kind='update'", link.AccountID); err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, "UPDATE sqlite_sequence SET seq=(SELECT COALESCE(MAX(id),0) FROM account_links) WHERE name='account_links'"); err != nil {
		return nil, err
	}
	updated, err := scanAccountRows(tx.QueryRowContext(ctx, selectAccountSQL+" WHERE id=?", link.AccountID))
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return updated, nil
}

var ErrAccountAuthorizationChanged = errors.New("account authorization changed; retry with the current account")

// The raw credential snapshot survives copies used with a newly fetched buffer.
// Such a buffer may be used without rewriting the persisted authorization.
func (a *WechatAccount) SameAuthorization(other *WechatAccount) bool {
	return a != nil && other != nil && a.ID == other.ID && a.OpenID == other.OpenID && a.credentialJSON == other.credentialJSON
}

func (db *DB) InvalidateSessionForAccount(ctx context.Context, account *WechatAccount, proxy string) error {
	_, err := db.sql.ExecContext(ctx, `DELETE FROM sessions WHERE wechat_account_id=? AND tcp_proxy=? AND EXISTS
(SELECT 1 FROM wechat_accounts WHERE id=? AND openid=? AND credentials IS ?)`, account.ID, proxy, account.ID, account.OpenID, account.credentialJSON)
	return err
}

func accountReferenceKey(id int64) string {
	return "account_reference_migration_" + strconv.FormatInt(id, 10)
}

// Keep reference aliases until the external panel acknowledges the migration.
func (db *DB) PendingAccountReferences(ctx context.Context, id int64) ([]string, string, error) {
	encoded, err := db.GetSetting(ctx, accountReferenceKey(id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, "", nil
	}
	if err != nil {
		return nil, "", err
	}
	var refs []string
	err = json.Unmarshal([]byte(encoded), &refs)
	return refs, encoded, err
}

func (db *DB) CompleteAccountReferences(ctx context.Context, id int64, expected string) error {
	_, err := db.sql.ExecContext(ctx, "DELETE FROM app_settings WHERE key=? AND value=?", accountReferenceKey(id), expected)
	return err
}

// PutSessionForAccount prevents a login started before a rescan/channel switch
// from restoring that old identity's session or UIN after the replacement.
func (db *DB) PutSessionForAccount(ctx context.Context, account *WechatAccount, uin int64, sessionBlob map[string]any, expiresAt int64, tcpProxy string) error {
	blob, err := json.Marshal(sessionBlob)
	if err != nil {
		return err
	}
	tx, err := db.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := time.Now().Unix()
	result, err := tx.ExecContext(ctx, `UPDATE wechat_accounts SET uin=? WHERE id=? AND openid=? AND login_buffer=?`, uin, account.ID, account.OpenID, account.LoginBuffer)
	if err != nil {
		return err
	}
	count, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if count != 1 {
		return ErrAccountAuthorizationChanged
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO sessions (wechat_account_id,uin,tcp_proxy,session_blob,expires_at,created_at,updated_at)
VALUES(?,?,?,?,?,?,?) ON CONFLICT(wechat_account_id,tcp_proxy) DO UPDATE SET
uin=excluded.uin,session_blob=excluded.session_blob,expires_at=excluded.expires_at,updated_at=excluded.updated_at`, account.ID, uin, tcpProxy, string(blob), expiresAt, now, now)
	if err != nil {
		return err
	}
	return tx.Commit()
}
