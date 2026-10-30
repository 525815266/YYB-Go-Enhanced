package store

import (
	"context"
	"encoding/json"
	"errors"
	"time"
)

var ErrSessionChanged = errors.New("protocol session or account authorization changed")

// ReplaceNativeSessionForAccount only rotates an existing same-route session.
// Unlike a new ManualAuth login, it may retain a session_only account's native
// authorization. It never changes OAuth credentials, scan time or account status.
// The exact stored blob is a CAS token, including the consumed-key marker.
func (db *DB) ReplaceNativeSessionForAccount(ctx context.Context, account *WechatAccount, previous *SessionRow, uin int64, sessionBlob map[string]any, expiresAt int64) (*SessionRow, error) {
	if account == nil || previous == nil || previous.WechatAccountID != account.ID || previous.blobJSON == "" {
		return nil, ErrSessionChanged
	}
	blob, err := json.Marshal(sessionBlob)
	if err != nil {
		return nil, err
	}
	now := time.Now().Unix()
	result, err := db.sql.ExecContext(ctx, `UPDATE sessions SET uin=?,session_blob=?,expires_at=?,updated_at=?
WHERE id=? AND wechat_account_id=? AND tcp_proxy=? AND session_blob=? AND EXISTS
(SELECT 1 FROM wechat_accounts WHERE id=? AND openid=? AND login_buffer=? AND credentials IS ?
AND COALESCE(status,'unknown')<>'expired')`, uin, string(blob), expiresAt, now,
		previous.ID, account.ID, previous.TCPProxy, previous.blobJSON,
		account.ID, account.OpenID, account.LoginBuffer, account.credentialJSON)
	if err != nil {
		return nil, err
	}
	count, err := result.RowsAffected()
	if err != nil {
		return nil, err
	}
	if count != 1 {
		return nil, ErrSessionChanged
	}
	next := *previous
	next.UIN, next.SessionBlob, next.ExpiresAt, next.UpdatedAt, next.blobJSON = &uin, sessionBlob, expiresAt, now, string(blob)
	return &next, nil
}
