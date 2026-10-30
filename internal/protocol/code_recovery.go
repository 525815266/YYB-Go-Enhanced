package protocol

import (
	"context"
	"errors"
	"fmt"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

// CodeSessionRecoveryError must not enter OAuth recovery: the original -14
// still does not establish that account credentials have expired.
type CodeSessionRecoveryError struct{ Cause error }

func (e *CodeSessionRecoveryError) Error() string { return e.Cause.Error() }
func (e *CodeSessionRecoveryError) Unwrap() error { return e.Cause }

type codeRepairAttempt struct {
	Account *store.WechatAccount
	At      time.Time
}

func (p *Pool) requestLoginCode(ctx context.Context, st WmpfSession, appID string) (map[string]any, error) {
	host := st.Session.HostAppID
	if len(host) == 0 {
		host = hostAppIDDefault
	}
	plain := buildJSAPIPlaintext(st.Session.UIN, appID, jsLoginURL, jsLoginCmdID, nil, host, nil)
	envelope, err := buildTransferPacket(st.Session, plain)
	if err != nil {
		return nil, err
	}
	code, _, err := p.sendEnvelope(ctx, st, envelope)
	if err != nil {
		return nil, err
	}
	if len(code) == 0 {
		return nil, ErrLoginCodeUnavailable
	}
	return map[string]any{"code": string(code), "errMsg": "login:ok"}, nil
}

// RecoverCodeSession first uses independent native recovery when available.
// Otherwise it validates a fresh ticket/session before replacing the cache.
// Native key rotation is saved before probing. Neither path rotates OAuth
// credentials or replays phone/data operations.
func (p *Pool) RecoverCodeSession(ctx context.Context, acc *store.WechatAccount, appID, proxy string, fallback bool, original error, fetch func(context.Context) (string, error)) (map[string]any, error) {
	return p.recoverCodeSession(ctx, acc, proxy, original, func(ctx context.Context) (WmpfSession, map[string]any, error) {
		if saved, err := p.nativeRecoveryState(ctx, acc.ID, proxy); err == nil {
			st, err := p.restoreNativeSession(ctx, acc, saved)
			if err != nil {
				return WmpfSession{}, nil, err
			}
			result, err := p.requestLoginCode(ctx, st, appID)
			return st, result, err
		}
		if acc.SessionOnly() {
			return WmpfSession{}, nil, ErrCachedSessionUnavailable
		}
		p.events.Emit("protocol.ticket_requested", eventlog.ForAccount(acc))
		buffer, err := fetch(ctx)
		if err != nil {
			return WmpfSession{}, nil, fmt.Errorf("fetch fresh code ticket: %w", err)
		}
		if err := acquire(ctx, p.loginSem); err != nil {
			return WmpfSession{}, nil, err
		}
		defer release(p.loginSem)
		st, err := p.loginAndSession(ctx, buffer, proxy, fallback)
		if err != nil {
			return WmpfSession{}, nil, err
		}
		result, err := p.requestLoginCode(ctx, st, appID)
		return st, result, err
	})
}

func (p *Pool) recoverCodeSession(ctx context.Context, acc *store.WechatAccount, proxy string, original error, probe func(context.Context) (WmpfSession, map[string]any, error)) (map[string]any, error) {
	fail := func(err error) (map[string]any, error) {
		return nil, &CodeSessionRecoveryError{Cause: errors.Join(original, err)}
	}
	if !IsApplicationRequestRejection(original) || acc == nil || acc.SessionOnly() && !p.hasNativeRecovery(ctx, acc.ID, proxy) {
		return nil, original
	}
	lock := p.codeLockFor(acc.ID)
	if err := acquire(ctx, lock); err != nil {
		return fail(err)
	}
	defer release(lock)
	stateLock := p.lockFor(fmt.Sprintf("%d\x00%s", acc.ID, proxy))
	if err := acquire(ctx, stateLock); err != nil {
		return fail(err)
	}
	defer release(stateLock)
	current, err := p.db.GetAccount(ctx, acc.ID)
	if err != nil {
		return fail(err)
	}
	if !current.SameAuthorization(acc) {
		return nil, store.ErrAccountAuthorizationChanged
	}
	if current.SessionOnly() && !p.hasNativeRecovery(ctx, current.ID, proxy) || current.Status != nil && *current.Status == "expired" {
		return nil, original
	}
	key := fmt.Sprintf("%d\x00%s", acc.ID, proxy)
	p.mu.Lock()
	previous, found := p.codeRepairs[key]
	allowed := !found || !previous.Account.SameAuthorization(current) || time.Since(previous.At) >= 5*time.Minute
	if allowed {
		p.codeRepairs[key] = codeRepairAttempt{Account: current, At: time.Now()}
	}
	p.mu.Unlock()
	if !allowed {
		p.events.Emit("protocol.recovery_deferred", eventlog.ForAccount(current))
		return nil, original
	}
	p.events.Emit("protocol.recovery_started", eventlog.ForAccount(current))
	st, result, err := probe(ctx)
	if err != nil {
		return fail(fmt.Errorf("code session recovery failed: %w", err))
	}
	if code, ok := result["code"].(string); !ok || code == "" {
		return fail(ErrLoginCodeUnavailable)
	}
	if st.TCPProxy != proxy {
		return fail(errors.New("fresh code session route mismatch"))
	}
	saveCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	if !st.nativePersisted {
		if err := p.db.PutSessionForAccount(saveCtx, current, st.Session.UIN, st.toBlob(), sessionExpiresAt(st, p.cfg.SessionTTL), proxy); err != nil {
			return fail(err)
		}
	} else {
		latest, err := p.db.GetAccount(saveCtx, current.ID)
		if err != nil {
			return fail(err)
		}
		if !latest.SameAuthorization(current) || latest.Status != nil && *latest.Status == "expired" {
			return fail(store.ErrAccountAuthorizationChanged)
		}
	}
	p.events.Emit("protocol.recovered", eventlog.ForAccount(current))
	return result, nil
}
