package store

import (
	"context"
	"database/sql"
	"time"
)

type AccountLink struct {
	ID              int64
	TokenHash       string
	Kind            string
	AccountID       int64
	OwnerUserID     *int64
	ExpectedOpenID  string
	TokenCiphertext string
	ExpiresAt       int64
	UsedAt          *int64
	RevokedAt       *int64
	CreatedAt       int64
}

type AccountLinkRecord struct {
	AccountLink
	Status string `json:"status"`
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
	return db.CreateAccountLinkWithCiphertext(ctx, tokenHash, "", kind, accountID, ownerUserID, expectedOpenID, expiresAt)
}

func (db *DB) CreateAccountLinkWithCiphertext(ctx context.Context, tokenHash, tokenCiphertext, kind string, accountID int64, ownerUserID *int64, expectedOpenID string, expiresAt int64) (*AccountLink, error) {
	now := time.Now().Unix()
	result, err := db.sql.ExecContext(ctx, `INSERT INTO account_links
		(token_hash, token_ciphertext, kind, account_id, owner_user_id, expected_openid, expires_at, created_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`, tokenHash, tokenCiphertext, kind, accountID, nullableInt64Ptr(ownerUserID), expectedOpenID, expiresAt, now)
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
	var owner, used, revoked sql.NullInt64
	err := db.sql.QueryRowContext(ctx, `SELECT id, token_hash, token_ciphertext, kind, account_id, owner_user_id,
		expected_openid, expires_at, used_at, revoked_at, created_at FROM account_links WHERE id=?`, id).Scan(
		&link.ID, &link.TokenHash, &link.TokenCiphertext, &link.Kind, &link.AccountID, &owner,
		&link.ExpectedOpenID, &link.ExpiresAt, &used, &revoked, &link.CreatedAt)
	if err != nil {
		return nil, err
	}
	link.OwnerUserID = nullableInt64(owner)
	link.UsedAt = nullableInt64(used)
	link.RevokedAt = nullableInt64(revoked)
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
		WHERE id=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?`, now, id, now)
	if err != nil {
		return false, err
	}
	count, err := result.RowsAffected()
	return count == 1, err
}

func (db *DB) PurgeExpiredAccountLinks(ctx context.Context) (int64, error) {
	now := time.Now().Unix()
	result, err := db.sql.ExecContext(ctx, `DELETE FROM account_links
		WHERE expires_at<=? OR used_at IS NOT NULL OR revoked_at IS NOT NULL`, now)
	if err != nil {
		return 0, err
	}
	count, err := result.RowsAffected()
	if err != nil {
		return 0, err
	}
	if count > 0 {
		if err = db.reclaimAccountLinkSequence(ctx); err != nil {
			return count, err
		}
	}
	return count, nil
}

// reclaimAccountLinkSequence lets SQLite reuse IDs after one-time links have
// been removed. Link IDs are only management handles (the token is the real
// capability), so there is no value in retaining an ever-growing sequence.
func (db *DB) reclaimAccountLinkSequence(ctx context.Context) error {
	var maxID sql.NullInt64
	if err := db.sql.QueryRowContext(ctx, "SELECT MAX(id) FROM account_links").Scan(&maxID); err != nil {
		return err
	}
	if !maxID.Valid {
		_, err := db.sql.ExecContext(ctx, "DELETE FROM sqlite_sequence WHERE name='account_links'")
		return err
	}
	_, err := db.sql.ExecContext(ctx, "UPDATE sqlite_sequence SET seq=? WHERE name='account_links'", maxID.Int64)
	return err
}

func (db *DB) ListAccountLinks(ctx context.Context, ownerUserID *int64) ([]AccountLinkRecord, error) {
	query := `SELECT id, token_hash, token_ciphertext, kind, account_id, owner_user_id, expected_openid, expires_at, used_at, revoked_at, created_at FROM account_links`
	args := []any{}
	if ownerUserID != nil {
		query += " WHERE owner_user_id=?"
		args = append(args, *ownerUserID)
	}
	query += " ORDER BY created_at DESC, id DESC"
	rows, err := db.sql.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AccountLinkRecord
	now := time.Now().Unix()
	for rows.Next() {
		var item AccountLinkRecord
		var owner, used, revoked sql.NullInt64
		if err := rows.Scan(&item.ID, &item.TokenHash, &item.TokenCiphertext, &item.Kind, &item.AccountID, &owner, &item.ExpectedOpenID, &item.ExpiresAt, &used, &revoked, &item.CreatedAt); err != nil {
			return nil, err
		}
		item.OwnerUserID, item.UsedAt, item.RevokedAt = nullableInt64(owner), nullableInt64(used), nullableInt64(revoked)
		switch {
		case item.RevokedAt != nil:
			item.Status = "revoked"
		case item.UsedAt != nil:
			item.Status = "used"
		case item.ExpiresAt <= now:
			item.Status = "expired"
		default:
			item.Status = "active"
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

func (db *DB) ListActiveAccountLinksForTarget(ctx context.Context, kind string, accountID int64, ownerUserID *int64) ([]AccountLinkRecord, error) {
	items, err := db.ListAccountLinks(ctx, ownerUserID)
	if err != nil {
		return nil, err
	}
	out := make([]AccountLinkRecord, 0)
	for _, item := range items {
		if item.Status == "active" && item.Kind == kind && (kind == "add" || item.AccountID == accountID) {
			out = append(out, item)
		}
	}
	return out, nil
}

func (db *DB) RevokeAccountLink(ctx context.Context, id int64, ownerUserID *int64) error {
	query := "DELETE FROM account_links WHERE id=?"
	args := []any{id}
	if ownerUserID != nil {
		query += " AND owner_user_id=?"
		args = append(args, *ownerUserID)
	}
	result, err := db.sql.ExecContext(ctx, query, args...)
	if err != nil {
		return err
	}
	n, _ := result.RowsAffected()
	if n == 0 {
		return sql.ErrNoRows
	}
	return db.reclaimAccountLinkSequence(ctx)
}

func (db *DB) DeleteAccountLink(ctx context.Context, id int64, ownerUserID *int64) error {
	query := "DELETE FROM account_links WHERE id=?"
	args := []any{id}
	if ownerUserID != nil {
		query += " AND owner_user_id=?"
		args = append(args, *ownerUserID)
	}
	result, err := db.sql.ExecContext(ctx, query, args...)
	if err != nil {
		return err
	}
	n, _ := result.RowsAffected()
	if n == 0 {
		return sql.ErrNoRows
	}
	return db.reclaimAccountLinkSequence(ctx)
}
