package store

import (
	"context"
	"database/sql"
	"time"
)

type AccountLink struct {
	ID             int64
	TokenHash      string
	Kind           string
	AccountID      int64
	OwnerUserID    *int64
	ExpectedOpenID string
	ExpiresAt      int64
	UsedAt         *int64
	CreatedAt      int64
}

func nullableInt64Ptr(value *int64) any {
	if value == nil {
		return nil
	}
	return *value
}

func nullableInt64(value sql.NullInt64) *int64 {
	if !value.Valid {
		return nil
	}
	result := value.Int64
	return &result
}

func (db *DB) CreateAccountLink(ctx context.Context, tokenHash, kind string, accountID int64, ownerUserID *int64, expectedOpenID string, expiresAt int64) (*AccountLink, error) {
	now := time.Now().Unix()
	result, err := db.sql.ExecContext(ctx, `INSERT INTO account_links
		(token_hash, kind, account_id, owner_user_id, expected_openid, expires_at, created_at)
		VALUES (?, ?, ?, ?, ?, ?, ?)`, tokenHash, kind, accountID, nullableInt64Ptr(ownerUserID), expectedOpenID, expiresAt, now)
	if err != nil {
		return nil, err
	}
	id, err := result.LastInsertId()
	if err != nil {
		return nil, err
	}
	return db.GetAccountLink(ctx, id)
}

func (db *DB) GetAccountLink(ctx context.Context, id int64) (*AccountLink, error) {
	link := &AccountLink{}
	var owner, used sql.NullInt64
	err := db.sql.QueryRowContext(ctx, `SELECT id, token_hash, kind, account_id, owner_user_id,
		expected_openid, expires_at, used_at, created_at FROM account_links WHERE id=?`, id).Scan(
		&link.ID, &link.TokenHash, &link.Kind, &link.AccountID, &owner,
		&link.ExpectedOpenID, &link.ExpiresAt, &used, &link.CreatedAt)
	if err != nil {
		return nil, err
	}
	link.OwnerUserID = nullableInt64(owner)
	link.UsedAt = nullableInt64(used)
	return link, nil
}

func (db *DB) GetAccountLinkByHash(ctx context.Context, tokenHash string) (*AccountLink, error) {
	var id int64
	err := db.sql.QueryRowContext(ctx, "SELECT id FROM account_links WHERE token_hash=?", tokenHash).Scan(&id)
	if err != nil {
		return nil, err
	}
	return db.GetAccountLink(ctx, id)
}

// ConsumeAccountLink atomically reserves a valid link. A false result means
// another request already used it or it expired.
func (db *DB) ConsumeAccountLink(ctx context.Context, id int64) (bool, error) {
	now := time.Now().Unix()
	result, err := db.sql.ExecContext(ctx, `UPDATE account_links SET used_at=?
		WHERE id=? AND used_at IS NULL AND expires_at>?`, now, id, now)
	if err != nil {
		return false, err
	}
	count, err := result.RowsAffected()
	return count == 1, err
}

func (db *DB) PurgeExpiredAccountLinks(ctx context.Context) (int64, error) {
	result, err := db.sql.ExecContext(ctx, "DELETE FROM account_links WHERE expires_at<? OR used_at IS NOT NULL", time.Now().Unix())
	if err != nil {
		return 0, err
	}
	return result.RowsAffected()
}
