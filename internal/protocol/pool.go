package protocol

import (
	"context"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"strconv"
	"sync"
	"time"

	"yyb_go/internal/store"
)

// ErrLoginCodeUnavailable describes a decrypted application response without a
// wx.login code. It does not establish that the account's session expired.
var ErrLoginCodeUnavailable = errors.New("wx.login returned no code; check the application's AppID or authorization")

var ErrCachedSessionUnavailable = errors.New("authorization rejected and no reusable protocol session; re-scan required")

type Config struct {
	ShortlinkTimeout        time.Duration
	LoginTimeout            time.Duration
	SessionTTL              time.Duration
	DNSCacheTTL             time.Duration
	MaxShortlinkConcurrency int
	MaxLoginConcurrency     int
}

func DefaultConfig() Config {
	return Config{
		ShortlinkTimeout:        8 * time.Second,
		LoginTimeout:            30 * time.Second,
		SessionTTL:              30 * time.Minute,
		DNSCacheTTL:             30 * time.Minute,
		MaxShortlinkConcurrency: 1000,
		MaxLoginConcurrency:     32,
	}
}

type WmpfSession struct {
	Session          AppSession
	PSK              pskEntry
	ShortlinkTargets []Target
	CreatedAt        time.Time
	TCPProxy         string
	FallbackDirect   bool
	CachedOnly       bool
}

type Pool struct {
	cfg Config
	db  *store.DB

	mu    sync.Mutex
	locks map[string]chan struct{}
	// codeLocks serialize wx.login requests for one account. Different
	// accounts keep running concurrently, while a single account cannot have
	// two one-time login codes consumed at the same time.
	codeLocks   map[int64]chan struct{}
	codeRepairs map[string]codeRepairAttempt

	loginSem     chan struct{}
	shortlinkSem chan struct{}
}

func NewPool(cfg Config, db *store.DB) *Pool {
	def := DefaultConfig()
	if cfg.ShortlinkTimeout == 0 {
		cfg.ShortlinkTimeout = def.ShortlinkTimeout
	}
	if cfg.LoginTimeout == 0 {
		cfg.LoginTimeout = def.LoginTimeout
	}
	if cfg.SessionTTL == 0 {
		cfg.SessionTTL = def.SessionTTL
	}
	if cfg.DNSCacheTTL == 0 {
		cfg.DNSCacheTTL = def.DNSCacheTTL
	}
	if cfg.MaxShortlinkConcurrency == 0 {
		cfg.MaxShortlinkConcurrency = def.MaxShortlinkConcurrency
	}
	if cfg.MaxLoginConcurrency == 0 {
		cfg.MaxLoginConcurrency = def.MaxLoginConcurrency
	}
	return &Pool{
		cfg:          cfg,
		db:           db,
		locks:        map[string]chan struct{}{},
		codeLocks:    map[int64]chan struct{}{},
		codeRepairs:  map[string]codeRepairAttempt{},
		loginSem:     make(chan struct{}, cfg.MaxLoginConcurrency),
		shortlinkSem: make(chan struct{}, cfg.MaxShortlinkConcurrency),
	}
}

func (p *Pool) GetCode(ctx context.Context, loginBuffer, appID string, accountID int64, tcpProxy string, fallbackDirect bool, expected ...*store.WechatAccount) (map[string]any, error) {
	codeLock := p.codeLockFor(accountID)
	if err := acquire(ctx, codeLock); err != nil {
		return nil, err
	}
	defer release(codeLock)

	return p.getCode(ctx, loginBuffer, appID, accountID, tcpProxy, fallbackDirect, expected...)
}

func (p *Pool) getCode(ctx context.Context, loginBuffer, appID string, accountID int64, tcpProxy string, fallbackDirect bool, expected ...*store.WechatAccount) (map[string]any, error) {
	call := func(ctx context.Context, st WmpfSession) (map[string]any, error) {
		return p.requestLoginCode(ctx, st, appID)
	}
	if len(expected) > 0 && expected[0].SessionOnly() {
		return p.run(ctx, loginBuffer, accountID, tcpProxy, fallbackDirect, call, expected...)
	}
	return retryLoginCode(ctx, func() (map[string]any, error) {
		return p.run(ctx, loginBuffer, accountID, tcpProxy, fallbackDirect, call, expected...)
	}, func() error {
		if err := p.invalidateExpected(ctx, accountID, tcpProxy, expected...); err != nil {
			return err
		}
		if tcpProxy != "" && fallbackDirect {
			return p.invalidateExpected(ctx, accountID, "", expected...)
		}
		return nil
	})
}

// Only a wx.login transport payload failure may rebuild a stale session and
// retry once. A decrypted empty application response is not a stale session.
// Never replay arbitrary OperateWXData operations, which can have side effects.
func retryLoginCode(ctx context.Context, call func() (map[string]any, error), invalidate func() error) (map[string]any, error) {
	result, err := call()
	emptyCode := err == nil && (result == nil || result["code"] == nil || result["code"] == "")
	if emptyCode {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, ErrLoginCodeUnavailable
	}
	if IsApplicationRequestRejection(err) || !errors.Is(err, ErrShortlinkPayload) {
		return result, err
	}
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if resetErr := invalidate(); resetErr != nil {
		return nil, errors.Join(err, fmt.Errorf("reset code session: %w", resetErr))
	}
	firstErr := err
	result, err = call()
	if err != nil {
		return nil, errors.Join(firstErr, fmt.Errorf("code retry after session rebuild failed: %w", err))
	}
	if result == nil || result["code"] == nil || result["code"] == "" {
		return nil, errors.Join(firstErr, fmt.Errorf("code retry after session rebuild: %w", ErrLoginCodeUnavailable))
	}
	return result, nil
}

func (p *Pool) GetPhoneNumber(ctx context.Context, loginBuffer, appID string, accountID int64, tcpProxy string, fallbackDirect bool, expected ...*store.WechatAccount) (map[string]any, error) {
	return p.run(ctx, loginBuffer, accountID, tcpProxy, fallbackDirect, func(ctx context.Context, st WmpfSession) (map[string]any, error) {
		plain := buildPhoneRequest(st.Session.UIN, appID, st.Session.HostAppID)
		envelope, err := buildTransferPacket(st.Session, plain)
		if err != nil {
			return nil, err
		}
		code, resp, err := p.sendEnvelope(ctx, st, envelope)
		if err != nil {
			return nil, err
		}
		return parseRawResponse(code, resp), nil
	}, expected...)
}

func (p *Pool) OperateWXData(ctx context.Context, loginBuffer, appID string, payload map[string]any, accountID int64, tcpProxy string, fallbackDirect bool, expected ...*store.WechatAccount) (map[string]any, error) {
	return p.run(ctx, loginBuffer, accountID, tcpProxy, fallbackDirect, func(ctx context.Context, st WmpfSession) (map[string]any, error) {
		plain, err := buildOperateRequest(st.Session.UIN, appID, payload, st.Session.HostAppID)
		if err != nil {
			return nil, err
		}
		envelope, err := buildTransferPacket(st.Session, plain)
		if err != nil {
			return nil, err
		}
		code, resp, err := p.sendEnvelope(ctx, st, envelope)
		if err != nil {
			return nil, err
		}
		return parseRawResponse(code, resp), nil
	}, expected...)
}

func (p *Pool) Invalidate(ctx context.Context, accountID int64, tcpProxy string) error {
	return p.db.InvalidateSession(ctx, accountID, tcpProxy)
}

func (p *Pool) invalidateExpected(ctx context.Context, accountID int64, proxy string, expected ...*store.WechatAccount) error {
	acc, err := p.db.GetAccount(ctx, accountID)
	if err != nil {
		return err
	}
	if acc.SessionOnly() {
		return ErrCachedSessionUnavailable
	}
	if len(expected) > 0 {
		return p.db.InvalidateSessionForAccount(ctx, expected[0], proxy)
	}
	return p.Invalidate(ctx, accountID, proxy)
}

func (p *Pool) run(ctx context.Context, loginBuffer string, accountID int64, tcpProxy string, fallbackDirect bool, op func(context.Context, WmpfSession) (map[string]any, error), expected ...*store.WechatAccount) (map[string]any, error) {
	st, err := p.state(ctx, loginBuffer, accountID, tcpProxy, fallbackDirect, expected...)
	if err == nil {
		st.FallbackDirect = fallbackDirect
		res, err := op(ctx, st)
		if err == nil || tcpProxy == "" || !fallbackDirect || IsApplicationRequestRejection(err) || st.CachedOnly {
			return res, err
		}
		_ = p.invalidateExpected(ctx, accountID, tcpProxy, expected...)
	}
	if tcpProxy != "" && fallbackDirect {
		st, err = p.state(ctx, loginBuffer, accountID, "", false, expected...)
		if err != nil {
			return nil, err
		}
		return op(ctx, st)
	}
	return nil, err
}

func (p *Pool) state(ctx context.Context, loginBuffer string, accountID int64, tcpProxy string, fallbackDirect bool, expected ...*store.WechatAccount) (WmpfSession, error) {
	// Include the queue wait in the login budget, not just the socket work.
	ctx, cancel := context.WithTimeout(ctx, p.cfg.LoginTimeout)
	defer cancel()
	acc, err := p.db.GetAccount(ctx, accountID)
	if err != nil {
		return WmpfSession{}, err
	}
	if len(expected) > 0 && !acc.SameAuthorization(expected[0]) {
		return WmpfSession{}, store.ErrAccountAuthorizationChanged
	}
	if st, err := p.cachedSession(ctx, accountID, tcpProxy); err == nil {
		st.CachedOnly = acc.SessionOnly()
		return st, nil
	} else if acc.SessionOnly() {
		return WmpfSession{}, ErrCachedSessionUnavailable
	} else if !errors.Is(err, sql.ErrNoRows) {
		return WmpfSession{}, err
	}
	lock := p.lockFor(fmt.Sprintf("%d\x00%s", accountID, tcpProxy))
	if err := acquire(ctx, lock); err != nil {
		return WmpfSession{}, err
	}
	defer release(lock)
	acc, err = p.db.GetAccount(ctx, accountID)
	if err != nil {
		return WmpfSession{}, err
	}
	if len(expected) > 0 && !acc.SameAuthorization(expected[0]) {
		return WmpfSession{}, store.ErrAccountAuthorizationChanged
	}
	if st, err := p.cachedSession(ctx, accountID, tcpProxy); err == nil {
		st.CachedOnly = acc.SessionOnly()
		return st, nil
	} else if !errors.Is(err, sql.ErrNoRows) {
		return WmpfSession{}, err
	}
	acc, err = p.db.GetAccount(ctx, accountID)
	if err != nil {
		return WmpfSession{}, err
	}
	if len(expected) > 0 && !acc.SameAuthorization(expected[0]) {
		return WmpfSession{}, store.ErrAccountAuthorizationChanged
	}
	if acc.SessionOnly() {
		return WmpfSession{}, ErrCachedSessionUnavailable
	}
	if err = acquire(ctx, p.loginSem); err != nil {
		return WmpfSession{}, err
	}
	defer release(p.loginSem)
	st, err := p.loginAndSession(ctx, loginBuffer, tcpProxy, fallbackDirect)
	if err != nil {
		return WmpfSession{}, err
	}
	uin := st.Session.UIN
	expiresAt := sessionExpiresAt(st, p.cfg.SessionTTL)
	blob := st.toBlob()
	if err = p.db.PutSessionForAccount(ctx, acc, uin, blob, expiresAt, tcpProxy); err != nil {
		return WmpfSession{}, err
	}
	return st, nil
}

func (p *Pool) cachedSession(ctx context.Context, accountID int64, tcpProxy string) (WmpfSession, error) {
	row, err := p.db.GetSession(ctx, accountID, tcpProxy)
	legacyDeadline := errors.Is(err, sql.ErrNoRows)
	if legacyDeadline {
		row, err = p.db.GetSessionForRecovery(ctx, accountID, tcpProxy)
	}
	if err != nil {
		return WmpfSession{}, err
	}
	st, err := sessionFromBlob(row.SessionBlob)
	if err != nil {
		return WmpfSession{}, err
	}
	if st.PSK.ExpiredAt > 0 && st.PSK.ExpiredAt <= time.Now().Unix() {
		return WmpfSession{}, sql.ErrNoRows
	}
	if legacyDeadline {
		// Older releases capped the cache at 30 minutes even when the issued
		// PSK remained valid. Reuse that existing state without rewriting its
		// deadline or doing ManualAuth/OAuth; genuine rejection still rebuilds.
		key, keyErr := hex.DecodeString(st.PSK.PreSharedKey)
		ticket, ticketErr := hex.DecodeString(st.PSK.TicketEntry)
		if st.PSK.PSKType != 1 || st.PSK.ExpiredAt <= time.Now().Unix() ||
			keyErr != nil || len(key) == 0 || ticketErr != nil || len(ticket) == 0 || st.TCPProxy != tcpProxy {
			return WmpfSession{}, sql.ErrNoRows
		}
	}
	return st, nil
}

// This checks local eligibility only; it does not assert server acceptance.
func (p *Pool) HasReusableSession(ctx context.Context, accountID int64, tcpProxy string) bool {
	st, err := p.cachedSession(ctx, accountID, tcpProxy)
	if err != nil || st.PSK.PSKType != 1 || st.PSK.ExpiredAt <= time.Now().Unix() {
		return false
	}
	key, keyErr := hex.DecodeString(st.PSK.PreSharedKey)
	ticket, ticketErr := hex.DecodeString(st.PSK.TicketEntry)
	return keyErr == nil && len(key) > 0 && ticketErr == nil && len(ticket) > 0
}

func (p *Pool) lockFor(key string) chan struct{} {
	p.mu.Lock()
	defer p.mu.Unlock()
	if l := p.locks[key]; l != nil {
		return l
	}
	l := make(chan struct{}, 1)
	p.locks[key] = l
	return l
}

func (p *Pool) codeLockFor(accountID int64) chan struct{} {
	p.mu.Lock()
	defer p.mu.Unlock()
	if lock := p.codeLocks[accountID]; lock != nil {
		return lock
	}
	lock := make(chan struct{}, 1)
	p.codeLocks[accountID] = lock
	return lock
}

func (p *Pool) loginAndSession(ctx context.Context, loginBuffer, tcpProxy string, fallbackDirect bool) (WmpfSession, error) {
	if loginBuffer == "" {
		return WmpfSession{}, fmt.Errorf("login_buffer is empty")
	}
	// HTTPDNS is optional: leave time for the official hostname fallback when
	// both HTTPDNS endpoints are unreachable.
	dnsTimeout := min(p.cfg.ShortlinkTimeout, p.cfg.LoginTimeout/3)
	dnsCtx, cancelDNS := context.WithTimeout(ctx, dnsTimeout)
	targets, err := getLonglinkTargets(dnsCtx, dnsTimeout, p.cfg.DNSCacheTTL)
	cancelDNS()
	if err != nil {
		return WmpfSession{}, fmt.Errorf("HTTPDNS LongLink failed: %w", err)
	}
	targets = longlinkLoginTargets(targets, 6)
	st, err := tryLonglinkCandidates(ctx, targets, p.cfg.LoginTimeout, func(attemptCtx context.Context, target Target) (WmpfSession, error) {
		return p.loginTarget(attemptCtx, target, loginBuffer, tcpProxy, fallbackDirect)
	})
	if err != nil {
		return WmpfSession{}, err
	}
	st.ShortlinkTargets = getShortlinkTargets(ctx, p.cfg.ShortlinkTimeout, p.cfg.DNSCacheTTL)
	return st, nil
}

func tryLonglinkCandidates(ctx context.Context, targets []Target, timeout time.Duration, login func(context.Context, Target) (WmpfSession, error)) (WmpfSession, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var failures []error
	for i, t := range targets {
		if err := ctx.Err(); err != nil {
			return WmpfSession{}, fmt.Errorf("LongLink login cancelled after %d/%d candidates: %w", i, len(targets), errors.Join(append(failures, err)...))
		}
		// Split the remaining budget across candidates, so one silent endpoint
		// cannot consume all the time reserved for fallback addresses/ports.
		deadline, _ := ctx.Deadline()
		attemptCtx, cancelAttempt := context.WithTimeout(ctx, time.Until(deadline)/time.Duration(len(targets)-i))
		st, err := login(attemptCtx, t)
		cancelAttempt()
		if err == nil {
			return st, nil
		}
		failures = append(failures, fmt.Errorf("candidate %d/%d %s: %w", i+1, len(targets), net.JoinHostPort(t.IP, strconv.Itoa(t.Port)), err))
		var rejected *ManualAuthRejectedError
		if errors.As(err, &rejected) && rejected.Code == -10 {
			// The endpoint accepted the handshake and rejected this ticket.
			// Trying the same ticket on every IP cannot repair it; let the
			// caller obtain a new login buffer within the remaining budget.
			return WmpfSession{}, fmt.Errorf("LongLink login rejected: %w", errors.Join(failures...))
		}
	}
	if len(failures) == 0 {
		failures = append(failures, fmt.Errorf("no LongLink candidates"))
	}
	return WmpfSession{}, fmt.Errorf("all LongLink candidates failed: %w", errors.Join(failures...))
}

func (p *Pool) loginTarget(ctx context.Context, target Target, loginBuffer, tcpProxy string, fallbackDirect bool) (WmpfSession, error) {
	mc, err := connectMmtls(ctx, target, p.cfg.LoginTimeout, tcpProxy, fallbackDirect)
	if err != nil {
		return WmpfSession{}, err
	}
	defer mc.close()
	meta, err := parseLoginBuffer(loginBuffer)
	if err != nil {
		return WmpfSession{}, err
	}
	appDeviceID, err := randomAppDeviceID()
	if err != nil {
		return WmpfSession{}, err
	}
	temp := &manualAuthTemp{}
	body, err := buildLoginBody(loginBuffer, meta.DeviceID, appDeviceID, temp)
	if err != nil {
		return WmpfSession{}, err
	}
	if err = mc.sendApp(cmdManualAuth, body); err != nil {
		return WmpfSession{}, fmt.Errorf("manualauth send: %w", err)
	}
	resp, err := mc.recvApp()
	if err != nil {
		return WmpfSession{}, fmt.Errorf("manualauth response: %w", err)
	}
	if resp.Cmd != cmdManualAuth {
		return WmpfSession{}, fmt.Errorf("manualauth failed: cmd=%d", resp.Cmd)
	}
	mar, err := parseLoginResponse(resp.Body, temp)
	if err != nil {
		return WmpfSession{}, err
	}
	appSess, err := extractSession(mar)
	if err != nil {
		return WmpfSession{}, err
	}
	appSess.DeviceID = meta.DeviceID
	appSess.HostAppID = meta.HostAppID
	psks, err := mc.extractPSKs()
	if err != nil {
		return WmpfSession{}, err
	}
	psk, ok := pickAccessPSK(psks)
	if !ok {
		return WmpfSession{}, fmt.Errorf("login finished but no access PSK was issued")
	}
	return WmpfSession{
		Session: appSess, PSK: psk, CreatedAt: time.Now(),
		TCPProxy: tcpProxy, FallbackDirect: fallbackDirect,
	}, nil
}

func (p *Pool) sendEnvelope(ctx context.Context, st WmpfSession, envelope []byte) ([]byte, []byte, error) {
	if err := acquire(ctx, p.shortlinkSem); err != nil {
		return nil, nil, err
	}
	defer release(p.shortlinkSem)
	reqCtx, cancel := context.WithTimeout(ctx, p.cfg.ShortlinkTimeout)
	defer cancel()
	return send0RTT(reqCtx, st.ShortlinkTargets, st.PSK, st.Session.RecvKey, envelope, p.cfg.ShortlinkTimeout, st.TCPProxy, st.FallbackDirect)
}

func acquire(ctx context.Context, sem chan struct{}) error {
	select {
	case sem <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func release(sem chan struct{}) {
	select {
	case <-sem:
	default:
	}
}

func sessionExpiresAt(st WmpfSession, ttl time.Duration) int64 {
	if st.PSK.ExpiredAt > 0 {
		return st.PSK.ExpiredAt
	}
	return time.Now().Unix() + int64(ttl.Seconds())
}

func (st WmpfSession) toBlob() map[string]any {
	return map[string]any{
		"session": map[string]any{
			"send_key":   encBytes(st.Session.SendKey),
			"recv_key":   encBytes(st.Session.RecvKey),
			"f9":         encBytes(st.Session.F9),
			"uin":        st.Session.UIN,
			"ticket":     encBytes(st.Session.Ticket),
			"device_id":  encBytes(st.Session.DeviceID),
			"host_appid": encBytes(st.Session.HostAppID),
		},
		"psk_entry":         st.PSK,
		"shortlink_targets": targetsToAny(st.ShortlinkTargets),
		"tcp_proxy":         st.TCPProxy,
	}
}

func sessionFromBlob(blob map[string]any) (WmpfSession, error) {
	sm, ok := blob["session"].(map[string]any)
	if !ok {
		return WmpfSession{}, fmt.Errorf("session_blob missing session")
	}
	pm, ok := blob["psk_entry"].(map[string]any)
	if !ok {
		return WmpfSession{}, fmt.Errorf("session_blob missing psk_entry")
	}
	st := WmpfSession{
		Session: AppSession{
			SendKey:   decBytes(sm["send_key"]),
			RecvKey:   decBytes(sm["recv_key"]),
			F9:        decBytes(sm["f9"]),
			UIN:       int64FromAny(sm["uin"]),
			Ticket:    decBytes(sm["ticket"]),
			DeviceID:  decBytes(sm["device_id"]),
			HostAppID: decBytes(sm["host_appid"]),
		},
		PSK: pskEntry{
			PSKType:      int(int64FromAny(pm["psk_type"])),
			PreSharedKey: stringAny(pm["pre_shared_key"]),
			TicketEntry:  stringAny(pm["ticket_entry"]),
			Lifetime:     int64FromAny(pm["lifetime"]),
			ExpiredAt:    int64FromAny(pm["expired_at"]),
		},
		ShortlinkTargets: targetsFromAny(blob["shortlink_targets"]),
		TCPProxy:         stringAny(blob["tcp_proxy"]),
		CreatedAt:        time.Now(),
	}
	if len(st.ShortlinkTargets) == 0 {
		st.ShortlinkTargets = append([]Target(nil), shortlinkFallbackTargets...)
	}
	if len(st.Session.SendKey) == 0 || len(st.Session.RecvKey) == 0 || st.Session.UIN == 0 {
		return WmpfSession{}, fmt.Errorf("session_blob incomplete")
	}
	return st, nil
}

func encBytes(b []byte) map[string]any {
	if b == nil {
		b = []byte{}
	}
	return map[string]any{"__bytes__": hex.EncodeToString(b)}
}

func decBytes(v any) []byte {
	if m, ok := v.(map[string]any); ok {
		if s, ok := m["__bytes__"].(string); ok {
			b, _ := hex.DecodeString(s)
			return b
		}
	}
	if s, ok := v.(string); ok {
		return []byte(s)
	}
	return nil
}

func targetsToAny(ts []Target) [][]any {
	out := make([][]any, 0, len(ts))
	for _, t := range ts {
		out = append(out, []any{t.IP, t.Port})
	}
	return out
}

func targetsFromAny(v any) []Target {
	var out []Target
	arr, ok := v.([]any)
	if !ok {
		return nil
	}
	for _, it := range arr {
		pair, ok := it.([]any)
		if !ok || len(pair) < 2 {
			continue
		}
		out = append(out, Target{IP: stringAny(pair[0]), Port: int(int64FromAny(pair[1]))})
	}
	return out
}

func stringAny(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func int64FromAny(v any) int64 {
	switch x := v.(type) {
	case int:
		return int64(x)
	case int64:
		return x
	case float64:
		return int64(x)
	case json.Number:
		n, _ := x.Int64()
		return n
	default:
		return 0
	}
}
