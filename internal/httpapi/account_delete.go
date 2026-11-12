package httpapi

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// Keep ownership from surviving a deleted account and claiming a reused ID.
func (a *App) deleteAccountWithOwner(ctx context.Context, id int64) error {
	var owner int64
	if a.auth != nil {
		var err error
		owner, err = a.auth.AccountOwner(ctx, id)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if owner > 0 {
			if err = a.auth.RemoveAccountOwner(ctx, id, owner); err != nil {
				return err
			}
		}
	}
	if err := a.db.DeleteAccount(ctx, id); err != nil {
		if owner > 0 {
			rollbackCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if rollbackErr := a.auth.ClaimAccount(rollbackCtx, id, owner); rollbackErr != nil {
				return fmt.Errorf("删除账号失败且用户归属回滚失败：%w", rollbackErr)
			}
		}
		return err
	}
	return nil
}
