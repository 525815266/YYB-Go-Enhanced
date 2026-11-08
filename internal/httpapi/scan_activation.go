package httpapi

import (
	"context"
	"errors"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

// LDPlayer supplies a scan ticket, not renewable OAuth credentials. Waiting for
// a script's first call can leave it unused until ManualAuth already rejects it.
// Prepare it after ownership and the final proxy have been saved, before panel
// export or reporting successful confirmation. Existing authorizations are not
// probed or silently logged in by this path.
func (a *App) activateScannedAccount(ctx context.Context, acc *store.WechatAccount) error {
	if protocol.ProviderOf(acc) != "ldplayer" {
		return nil
	}
	proxy, fallback, err := a.resolveWXAppProxy(ctx, acc.ID)
	prepare := a.prepareScanSession
	if prepare == nil {
		prepare = a.pool.PrepareSession
	}
	if err == nil {
		err = prepare(ctx, acc, proxy, fallback)
	}
	fields := eventlog.ForAccount(acc)
	if err == nil {
		a.events.Emit("account.session_prepared", fields)
		return nil
	}
	fields.Rejections = eventRejections(err)
	a.events.Emit("account.session_failed", fields)
	status := "unknown"
	var rejected *protocol.ManualAuthRejectedError
	if errors.As(err, &rejected) && rejected.Code == -10 {
		status = "expired"
	}
	// The browser may disconnect during the login. Persist the result using the
	// authorization snapshot, so neither cancellation nor a newer scan can make
	// an unprepared account look successfully activated.
	saveCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	applied, saveErr := a.db.ApplyCredentialRefresh(saveCtx, acc, acc.LoginBuffer, acc.Credentials, status)
	if saveErr != nil {
		return errors.Join(err, saveErr)
	}
	if !applied {
		return store.ErrAccountAuthorizationChanged
	}
	return err
}
