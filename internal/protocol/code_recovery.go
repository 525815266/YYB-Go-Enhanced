package protocol

import (
	"context"
	"errors"
	"fmt"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

// The original -14 alone must not enter OAuth recovery. A separately typed
// ticket rejection can permit one credential refresh after saved-ticket login.
type CodeSessionRecoveryError struct{ Cause error }

func (e *CodeSessionRecoveryError) Error() string { return e.Cause.Error() }
func (e *CodeSessionRecoveryError) Unwrap() error { return e.Cause }

// CodeCredentialRecoveryError identifies a fresh-ticket access rejection,
// rather than inferring credential expiry from an application or native error.
type CodeCredentialRecoveryError struct{ Cause error }

func (e *CodeCredentialRecoveryError) Error() string { return e.Cause.Error() }
func (e *CodeCredentialRecoveryError) Unwrap() error { return e.Cause }

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

// Authorized accounts first try the saved-ticket path used before v0.2.50.
// Rejected OAuth accounts may only use independent native recovery. A candidate
// replaces the shared cache only after a nonempty code from the same target.
func (p *Pool) RecoverCodeSession(ctx context.Context, acc *store.WechatAccount, appID, proxy string, fallback bool, original error, fetch func(context.Context) (string, error)) (map[string]any, error) {
	return p.recoverCodeSession(ctx, acc, appID, proxy, original, func(ctx context.Context) (WmpfSession, map[string]any, error) {
		if acc.SessionOnly() {
			saved, err := p.nativeRecoveryState(ctx, acc.ID, proxy)
			if err != nil {
				return WmpfSession{}, nil, ErrCachedSessionUnavailable
			}
			st, err := p.restoreNativeSession(ctx, acc, saved)
			if err != nil {
				return WmpfSession{}, nil, err
			}
			result, err := p.requestLoginCode(ctx, st, appID)
			return st, result, err
		}
		return recoverCodeWithTickets(ctx, acc.LoginBuffer, func(ctx context.Context, buffer string) (WmpfSession, error) {
			if err := acquire(ctx, p.loginSem); err != nil {
				return WmpfSession{}, err
			}
			defer release(p.loginSem)
			return p.loginAndSession(ctx, buffer, proxy, fallback)
		}, func(ctx context.Context) (string, error) {
			p.events.Emit("protocol.ticket_requested", eventlog.ForAccount(acc))
			return fetch(ctx)
		}, func(ctx context.Context, st WmpfSession) (map[string]any, error) {
			return p.requestLoginCode(ctx, st, appID)
		})
	})
}

func recoverCodeWithTickets(ctx context.Context, saved string, login func(context.Context, string) (WmpfSession, error), fetch func(context.Context) (string, error), request func(context.Context, WmpfSession) (map[string]any, error)) (WmpfSession, map[string]any, error) {
	st, err := login(ctx, saved)
	if err == nil {
		result, err := request(ctx, st)
		return st, result, err
	}
	var rejected *ManualAuthRejectedError
	if ctx.Err() != nil || !errors.As(err, &rejected) || rejected.Code != -10 {
		return WmpfSession{}, nil, err
	}
	savedErr := err
	buffer, err := fetch(ctx)
	if err != nil {
		var access *LoginBufferRejectedError
		if errors.As(err, &access) && (access.Code == -101 || access.Code == 40014 || access.Code == 42001) {
			err = &CodeCredentialRecoveryError{Cause: err}
		}
		return WmpfSession{}, nil, errors.Join(savedErr, fmt.Errorf("fetch fresh code ticket: %w", err))
	}
	st, err = login(ctx, buffer)
	if err != nil {
		return WmpfSession{}, nil, errors.Join(savedErr, fmt.Errorf("fresh ticket login: %w", err))
	}
	result, err := request(ctx, st)
	if err != nil {
		return st, nil, errors.Join(savedErr, fmt.Errorf("fresh ticket code: %w", err))
	}
	return st, result, nil
}

func (p *Pool) recoverCodeSession(ctx context.Context, acc *store.WechatAccount, appID, proxy string, original error, probe func(context.Context) (WmpfSession, map[string]any, error)) (map[string]any, error) {
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
	key := fmt.Sprintf("%d\x00%s\x00%s", acc.ID, proxy, appID)
	p.mu.Lock()
	for k, attempt := range p.codeRepairs {
		if time.Since(attempt.At) >= 5*time.Minute {
			delete(p.codeRepairs, k)
		}
	}
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
	p.mu.Lock()
	delete(p.codeRepairs, key)
	p.mu.Unlock()
	return result, nil
}
