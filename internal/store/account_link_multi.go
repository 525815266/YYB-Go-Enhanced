package store

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"time"
)

type AccountLinkScan struct {
	Product   string `json:"product"`
	AccountID int64  `json:"account_id"`
}

func (db *DB) AccountLinkScans(ctx context.Context, id int64) ([]AccountLinkScan, error) {
	rows, err := db.sql.QueryContext(ctx, "SELECT product,account_id FROM account_link_scans WHERE link_id=? ORDER BY product", id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]AccountLinkScan, 0)
	for rows.Next() {
		var s AccountLinkScan
		if err := rows.Scan(&s.Product, &s.AccountID); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// SaveAccountLinkScan commits authorization, fleet membership, proxy inheritance
// and durable link progress together. The caller verifies unionid and ownership;
// snapshots prevent that verification from racing an account replacement.
// claim runs before commit because the ownership store can be a separate DB.
// The caller compensates a newly created claim if this transaction fails.
func (db *DB) SaveAccountLinkScan(ctx context.Context, link *AccountLink, primary, target *WechatAccount, product, openID, buffer string, nickname, avatar *string, info, creds map[string]any, claim func(int64, bool) error, confirmSameWechat ...bool) (*WechatAccount, error) {
	if link == nil || !link.AllowMultiChannel || openID == "" || buffer == "" {
		return nil, ErrAccountLinkChanged
	}
	userJSON, err := marshalNullable(info)
	if err != nil {
		return nil, err
	}
	credJSON, err := marshalNullable(creds)
	if err != nil {
		return nil, err
	}
	tx, err := db.sql.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	now := time.Now().Unix()
	var root sql.NullInt64
	err = tx.QueryRowContext(ctx, `SELECT multi_primary_id FROM account_links WHERE id=? AND token_hash=?
AND allow_multi_channel=1 AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?`, link.ID, link.TokenHash, now).Scan(&root)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrAccountLinkChanged
	}
	if err != nil {
		return nil, err
	}
	if root.Valid != (primary != nil) || (primary != nil && root.Int64 != primary.ID) {
		return nil, ErrAccountLinkChanged
	}
	if link.Kind == "update" {
		base, err := scanAccountRows(tx.QueryRowContext(ctx, selectAccountSQL+" WHERE id=?", link.AccountID))
		if err != nil {
			return nil, err
		}
		baseRoot := base.ID
		if base.FleetPrimaryID != nil {
			baseRoot = *base.FleetPrimaryID
		}
		if base.OpenID != link.ExpectedOpenID || !root.Valid || baseRoot != root.Int64 {
			return nil, ErrAccountLinkChanged
		}
	}
	if primary != nil {
		current, err := scanAccountRows(tx.QueryRowContext(ctx, selectAccountSQL+" WHERE id=?", primary.ID))
		if err != nil {
			return nil, err
		}
		if !current.SameAuthorization(primary) || (current.FleetPrimaryID != nil && *current.FleetPrimaryID != current.ID) {
			return nil, ErrAccountLinkChanged
		}
		rows, err := tx.QueryContext(ctx, selectAccountSQL+" WHERE id=? OR fleet_primary_id=?", primary.ID, primary.ID)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			member, scanErr := scanAccountRows(rows)
			if scanErr != nil {
				rows.Close()
				return nil, scanErr
			}
			if member.Public().ScanProduct == product && (target == nil || target.ID != member.ID) {
				rows.Close()
				return nil, ErrAccountIdentityConflict
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return nil, err
		}
	}
	var id int64
	if target != nil {
		current, err := scanAccountRows(tx.QueryRowContext(ctx, selectAccountSQL+" WHERE id=?", target.ID))
		if err != nil {
			return nil, err
		}
		replaceTicket := len(confirmSameWechat) > 0 && confirmSameWechat[0] && product == "ldplayer" && current.ScanProduct() == "ldplayer" &&
			current.Status != nil && *current.Status == "expired" && strings.HasPrefix(current.OpenID, "ldplayer:") && strings.HasPrefix(openID, "ldplayer:")
		if !current.SameAuthorization(target) || current.OpenID != openID && !replaceTicket || primary == nil ||
			(current.ID != primary.ID && (current.FleetPrimaryID == nil || *current.FleetPrimaryID != primary.ID)) {
			return nil, ErrAccountIdentityConflict
		}
		id = current.ID
		_, err = tx.ExecContext(ctx, `UPDATE wechat_accounts SET openid=?,login_buffer=?,credentials=?,nickname=?,alias=?,avatar=?,user_info=?,
uin=NULL,status='alive',last_checked_at=?,updated_at=? WHERE id=?`, openID, buffer, credJSON, nullableString(nickname), nullableString(nickname), nullableString(avatar), userJSON, now, now, id)
		if err != nil {
			return nil, err
		}
		if _, err = tx.ExecContext(ctx, "DELETE FROM sessions WHERE wechat_account_id=?", id); err != nil {
			return nil, err
		}
		if replaceTicket && id == link.AccountID {
			if _, err = tx.ExecContext(ctx, "UPDATE account_links SET expected_openid=? WHERE id=?", openID, link.ID); err != nil {
				return nil, err
			}
		}
	} else {
		var duplicate int64
		err = tx.QueryRowContext(ctx, "SELECT id FROM wechat_accounts WHERE openid=?", openID).Scan(&duplicate)
		if err == nil {
			return nil, ErrAccountIdentityConflict
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		if err = tx.QueryRowContext(ctx, `SELECT MIN(candidate) FROM (SELECT 1 AS candidate UNION ALL SELECT id+1 FROM wechat_accounts)
WHERE candidate NOT IN (SELECT id FROM wechat_accounts)`).Scan(&id); err != nil {
			return nil, err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO wechat_accounts(id,openid,login_buffer,credentials,nickname,alias,avatar,user_info,status,created_at,updated_at)
VALUES(?,?,?,?,?,?,?,?,'alive',?,?)`, id, openID, buffer, credJSON, nullableString(nickname), nullableString(nickname), nullableString(avatar), userJSON, now, now)
		if err != nil {
			return nil, err
		}
		// Missing settings mean direct access, just as for the source account.
		_, err = tx.ExecContext(ctx, `INSERT INTO account_proxy_settings(account_id,mode,proxy_type,static_proxy,api_url,provider_profile_id,
region_code,region_province,region_city,refresh_ahead_seconds,protocol_proxy_mode,created_at,updated_at)
SELECT ?,mode,proxy_type,static_proxy,api_url,provider_profile_id,region_code,region_province,region_city,refresh_ahead_seconds,protocol_proxy_mode,?,?
FROM account_proxy_settings WHERE account_id=?`, id, now, now, link.AccountID)
		if err != nil {
			return nil, err
		}
	}
	if !root.Valid {
		root = sql.NullInt64{Int64: id, Valid: true}
	}
	if _, err = tx.ExecContext(ctx, "UPDATE wechat_accounts SET fleet_primary_id=? WHERE id=? OR id=?", root.Int64, root.Int64, id); err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, "UPDATE account_links SET multi_primary_id=? WHERE id=?", root.Int64, link.ID); err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO account_link_scans(link_id,product,account_id) VALUES(?,?,?)
ON CONFLICT(link_id,product) DO UPDATE SET account_id=excluded.account_id`, link.ID, product, id); err != nil {
		return nil, err
	}
	if claim != nil {
		if err = claim(id, target == nil); err != nil {
			return nil, err
		}
	}
	acc, err := scanAccountRows(tx.QueryRowContext(ctx, selectAccountSQL+" WHERE id=?", id))
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return acc, nil
}

func (db *DB) FinishAccountLinkScans(ctx context.Context, link *AccountLink) (bool, error) {
	now := time.Now().Unix()
	result, err := db.sql.ExecContext(ctx, `UPDATE account_links SET used_at=? WHERE id=? AND token_hash=? AND allow_multi_channel=1
AND used_at IS NULL AND revoked_at IS NULL AND expires_at>? AND EXISTS(SELECT 1 FROM account_link_scans WHERE link_id=?)`, now, link.ID, link.TokenHash, now, link.ID)
	if err != nil {
		return false, err
	}
	n, err := result.RowsAffected()
	return n == 1, err
}
