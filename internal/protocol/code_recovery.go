package protocol

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
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

// DefaultFreshSessionWindow is how long after a successful request on a session
// a -14 is treated as the AppID's own refusal. Sessions do not expire within
// seconds, so a new login inside this window cannot help and only adds logins
// that the upstream may count against the authorization. YYB_FRESH_SESSION_WINDOW
// and tuning.json change it; zero switches the guard off.
//
// The guard rests on one assumption: that a session which just served a code can
// serve the next AppID too. On accounts whose session is used up by one code the
// next request needs a login, and the guard then refuses exactly the repair that
// would have worked (see docs/dense-batch-lab-20261103.md).
const DefaultFreshSessionWindow = 2 * time.Minute

// EffectiveFreshWindow turns Config.FreshSessionWindow into the window the pool
// applies: zero keeps the default, a negative value switches the guard off (0).
func EffectiveFreshWindow(configured time.Duration) time.Duration {
	switch {
	case configured == 0:
		return DefaultFreshSessionWindow
	case configured < 0:
		return 0
	default:
		return configured
	}
}

// FreshSessionWindow reports the window in force; zero means the guard is off.
func (p *Pool) FreshSessionWindow() time.Duration {
	return time.Duration(p.freshWindow.Load())
}

// SetFreshSessionWindow changes the window at run time; zero or less switches
// the guard off.
func (p *Pool) SetFreshSessionWindow(window time.Duration) {
	if window < 0 {
		window = 0
	}
	p.freshWindow.Store(int64(window))
}

// repairRegistrationHook runs just before a repair attempt is registered. Tests
// use it to cancel a request at exactly that point.
var repairRegistrationHook func()

// verifiedSession records the last wx.login that succeeded on an account's
// session for one route. Fingerprint identifies that very session, so a cache
// that was invalidated or replaced afterwards no longer counts as verified.
type verifiedSession struct {
	Account     *store.WechatAccount
	At          time.Time
	Fingerprint string
}

// sessionFingerprint identifies a session by a full SHA-256 over its key
// material and PSK entry, each part length-prefixed so different splits cannot
// collide. Only the digest is kept, never the keys.
func sessionFingerprint(st WmpfSession) string {
	h := sha256.New()
	for _, part := range [][]byte{st.Session.SendKey, st.Session.RecvKey, []byte(st.PSK.PreSharedKey), []byte(st.PSK.TicketEntry)} {
		var n [8]byte
		binary.BigEndian.PutUint64(n[:], uint64(len(part)))
		h.Write(n[:])
		h.Write(part)
	}
	return hex.EncodeToString(h.Sum(nil))
}

func sessionKey(accountID int64, proxy string) string {
	return fmt.Sprintf("%d|%s", accountID, proxy)
}

func firstAccount(expected []*store.WechatAccount) *store.WechatAccount {
	if len(expected) == 0 {
		return nil
	}
	return expected[0]
}

// markSessionVerified is called after a wx.login returned a code on st. acc may
// be nil when the caller did not pass the authorization snapshot; the route is
// the cache route key carried by st, which differs from the configured one after a direct
// fallback.
func (p *Pool) markSessionVerified(acc *store.WechatAccount, accountID int64, st WmpfSession) {
	window := p.FreshSessionWindow()
	now := time.Now()
	p.mu.Lock()
	defer p.mu.Unlock()
	for k, v := range p.verified {
		if now.Sub(v.At) >= window {
			delete(p.verified, k)
		}
	}
	if window <= 0 {
		return
	}
	p.verified[sessionKey(accountID, st.TCPProxy)] = verifiedSession{Account: acc, At: now, Fingerprint: sessionFingerprint(st)}
}

// sessionRecentlyVerified reports whether a wx.login succeeded within
// the fresh-session window on the session that is cached for this account and route
// now, under the same authorization. A replaced or invalidated cache never
// matches, so a new session is not covered by an old success.
func (p *Pool) sessionRecentlyVerified(ctx context.Context, current *store.WechatAccount, proxy string) bool {
	window := p.FreshSessionWindow()
	if window <= 0 {
		return false
	}
	p.mu.Lock()
	v, ok := p.verified[sessionKey(current.ID, proxy)]
	p.mu.Unlock()
	if !ok || time.Since(v.At) >= window {
		return false
	}
	if v.Account != nil && !v.Account.SameAuthorization(current) {
		return false
	}
	cached, err := p.cachedSession(ctx, current.ID, proxy)
	return err == nil && sessionFingerprint(cached) == v.Fingerprint
}

func (p *Pool) requestLoginCode(ctx context.Context, st WmpfSession, appID string) (map[string]any, error) {
	host := st.Session.HostAppID
	if len(host) == 0 {
		host = hostAppIDDefault
	}
	plain := buildJSAPIPlaintext(st.Session.UIN, appID, jsLoginURL, jsLoginCmdID, nil, host, st.Device.SessDevice)
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
	device := p.deviceFor(ctx, acc)
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
			st.Device = device
			result, err := p.requestLoginCode(ctx, st, appID)
			return st, result, err
		}
		return recoverCodeWithTickets(ctx, acc.LoginBuffer, func(ctx context.Context, buffer string) (WmpfSession, error) {
			if err := acquire(ctx, p.loginSem); err != nil {
				return WmpfSession{}, err
			}
			defer release(p.loginSem)
			return p.loginAndSession(ctx, buffer, proxy, fallback, device)
		}, func(ctx context.Context) (string, error) {
			p.events.Emit("protocol.ticket_requested", eventlog.ForAccount(acc).WithTarget(appID, eventlog.OpGetCode, ""))
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
	verified := p.sessionRecentlyVerified(ctx, current, proxy)
	if err := ctx.Err(); err != nil {
		// A cancelled request must not register a repair attempt, and its
		// backoff, that it can never run. This also covers a cancellation that
		// made the session lookup above fail.
		return fail(err)
	}
	if verified {
		// A wx.login just returned a code on this very session, so this -14 is
		// more likely the AppID's own refusal than an expired session. Another
		// ManualAuth and ticket would only add logins.
		p.events.Emit("protocol.recovery_skipped", eventlog.ForAccount(current).WithTarget(appID, eventlog.OpGetCode, ""))
		return nil, original
	}
	key := fmt.Sprintf("%d\x00%s\x00%s", acc.ID, proxy, appID)
	if hook := repairRegistrationHook; hook != nil {
		hook()
	}
	p.mu.Lock()
	for k, attempt := range p.codeRepairs {
		if time.Since(attempt.At) >= 5*time.Minute {
			delete(p.codeRepairs, k)
		}
	}
	if err := ctx.Err(); err != nil {
		// Re-check inside the critical section: a request cancelled while it
		// waited for the lock must not leave a five minute backoff behind.
		p.mu.Unlock()
		return fail(err)
	}
	previous, found := p.codeRepairs[key]
	allowed := !found || !previous.Account.SameAuthorization(current) || time.Since(previous.At) >= 5*time.Minute
	if allowed && !p.AllowLogin(current.ID, ProviderOf(current)) {
		// The account has used its login budget. Keep the original error and
		// start no backoff for this AppID: once the window has moved on, a later
		// request may still repair it. The refusal is counted in the budget's own
		// summary event, so a burst does not add a log row per request.
		p.mu.Unlock()
		return nil, original
	}
	if allowed {
		p.codeRepairs[key] = codeRepairAttempt{Account: current, At: time.Now()}
	}
	p.mu.Unlock()
	if !allowed {
		p.events.Emit("protocol.recovery_deferred", eventlog.ForAccount(current).WithTarget(appID, eventlog.OpGetCode, ""))
		return nil, original
	}
	p.events.Emit("protocol.recovery_started", eventlog.ForAccount(current).WithTarget(appID, eventlog.OpGetCode, ""))
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
	p.events.Emit("protocol.recovered", eventlog.ForAccount(current).WithTarget(appID, eventlog.OpGetCode, ""))
	p.markSessionVerified(current, current.ID, st)
	p.mu.Lock()
	delete(p.codeRepairs, key)
	p.mu.Unlock()
	return result, nil
}
