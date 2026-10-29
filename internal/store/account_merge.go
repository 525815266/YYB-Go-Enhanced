package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strconv"
	"time"
)

var ErrMergeSourceHasJobs = errors.New("source account has managed jobs; remove them before merging")

// MergeAccountAuthorization repairs an accidental extra account. The selected
// target keeps its ID and configuration; only the source authorization moves.
// An already configured source is refused, never silently stripped of its jobs.
func (db *DB) MergeAccountAuthorization(ctx context.Context, source, target *WechatAccount) (*WechatAccount, error) {
	if source.ID == target.ID {
		return nil, errors.New("source and target must differ")
	}
	userJSON, err := marshalNullable(source.UserInfo)
	if err != nil {
		return nil, err
	}
	tx, err := db.sql.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	for _, snapshot := range []*WechatAccount{source, target} {
		current, err := scanAccountRows(tx.QueryRowContext(ctx, selectAccountSQL+" WHERE id=?", snapshot.ID))
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrAccountAuthorizationChanged
		}
		if err != nil {
			return nil, err
		}
		if !current.SameAuthorization(snapshot) || current.LoginBuffer != snapshot.LoginBuffer {
			return nil, ErrAccountAuthorizationChanged
		}
	}
	if source.LoginBuffer == "" || len(source.Credentials) == 0 || source.Status != nil && *source.Status == "expired" {
		return nil, errors.New("source authorization is not usable")
	}
	var jobs int
	if err = tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM account_script_jobs WHERE account_id=?", source.ID).Scan(&jobs); err != nil {
		return nil, err
	}
	if jobs > 0 {
		return nil, ErrMergeSourceHasJobs
	}
	refs := []string{target.OpenID, strconv.FormatInt(source.ID, 10)}
	for _, id := range []int64{source.ID, target.ID} {
		var encoded string
		err = tx.QueryRowContext(ctx, "SELECT value FROM app_settings WHERE key=?", accountReferenceKey(id)).Scan(&encoded)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		if encoded != "" {
			var old []string
			if err = json.Unmarshal([]byte(encoded), &old); err != nil {
				return nil, err
			}
			refs = append(refs, old...)
		}
	}
	// Deleting source first releases its unique OpenID inside the same transaction.
	if _, err = tx.ExecContext(ctx, "DELETE FROM wechat_accounts WHERE id=?", source.ID); err != nil {
		return nil, err
	}
	now := time.Now().Unix()
	_, err = tx.ExecContext(ctx, `UPDATE wechat_accounts SET openid=?,login_buffer=?,credentials=?,nickname=?,alias=?,avatar=?,user_info=?,uin=NULL,status='alive',last_checked_at=?,updated_at=? WHERE id=?`, source.OpenID, source.LoginBuffer, source.credentialJSON, nullableString(source.Nickname), nullableString(source.Alias), nullableString(source.Avatar), userJSON, now, now, target.ID)
	if err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM sessions WHERE wechat_account_id=?", target.ID); err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM account_links WHERE account_id=?", target.ID); err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM app_settings WHERE key=?", accountReferenceKey(source.ID)); err != nil {
		return nil, err
	}
	encoded, err := json.Marshal(refs)
	if err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at`, accountReferenceKey(target.ID), string(encoded), now); err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, "UPDATE sqlite_sequence SET seq=(SELECT COALESCE(MAX(id),0) FROM account_links) WHERE name='account_links'"); err != nil {
		return nil, err
	}
	merged, err := scanAccountRows(tx.QueryRowContext(ctx, selectAccountSQL+" WHERE id=?", target.ID))
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return merged, nil
}
