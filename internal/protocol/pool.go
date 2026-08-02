package protocol

import (
	"context"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"yyb_go/internal/store"
)

type Config struct {
	ShortlinkTimeout        time.Duration
	LoginTimeout            time.Duration
	SessionTTL              time.Duration
	DNSCacheTTL             time.Duration
	MaxShortlinkConcurrency int
	MaxLoginConcurrency     int
	TCPProxy                string
	TCPProxyFallbackDirect  bool
}

func DefaultConfig() Config {
	return Config{
		ShortlinkTimeout:        8 * time.Second,
		LoginTimeout:            30 * time.Second,
		SessionTTL:              30 * time.Minute,
		DNSCacheTTL:             30 * time.Minute,
		MaxShortlinkConcurrency: 1000,
		MaxLoginConcurrency:     32,
		TCPProxyFallbackDirect:  true,
	}
}

type WmpfSession struct {
	Session          AppSession
	PSK              pskEntry
	ShortlinkTargets []Target
	CreatedAt        time.Time
	TCPProxy         string
}

type Pool struct {
	cfg Config
	db  *store.DB

	mu    sync.Mutex
	locks map[string]*sync.Mutex

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
		locks:        map[string]*sync.Mutex{},
		loginSem:     make(chan struct{}, cfg.MaxLoginConcurrency),
		shortlinkSem: make(chan struct{}, cfg.MaxShortlinkConcurrency),
	}
}

func (p *Pool) GetCode(ctx context.Context, loginBuffer, appID string, accountID int64, tcpProxy string) (map[string]any, error) {
	return p.run(ctx, loginBuffer, accountID, tcpProxy, func(ctx context.Context, st WmpfSession) (map[string]any, error) {
		hostAppID := st.Session.HostAppID
		if len(hostAppID) == 0 {
			hostAppID = hostAppIDDefault
		}
		plain := buildJSAPIPlaintext(st.Session.UIN, appID, jsLoginURL, jsLoginCmdID, nil, hostAppID, nil)
		envelope, err := buildTransferPacket(st.Session, plain)
		if err != nil {
			return nil, err
		}
		code, resp, err := p.sendEnvelope(ctx, st, envelope)
		if err != nil {
			return nil, err
		}
		return map[string]any{
			"code":    string(code),
			"codeLen": len(code),
			"respHex": hex.EncodeToString(resp),
			"errMsg":  "login:ok",
		}, nil
	})
}

func (p *Pool) GetPhoneNumber(ctx context.Context, loginBuffer, appID string, accountID int64, tcpProxy string) (map[string]any, error) {
	return p.run(ctx, loginBuffer, accountID, tcpProxy, func(ctx context.Context, st WmpfSession) (map[string]any, error) {
		plain := buildPhoneRequest(st.Session.UIN, appID)
		envelope, err := buildTransferPacket(st.Session, plain)
		if err != nil {
			return nil, err
		}
		code, resp, err := p.sendEnvelope(ctx, st, envelope)
		if err != nil {
			return nil, err
		}
		return parseRawResponse(code, resp), nil
	})
}

func (p *Pool) OperateWXData(ctx context.Context, loginBuffer, appID string, payload map[string]any, accountID int64, tcpProxy string) (map[string]any, error) {
	return p.run(ctx, loginBuffer, accountID, tcpProxy, func(ctx context.Context, st WmpfSession) (map[string]any, error) {
		plain, err := buildOperateRequest(st.Session.UIN, appID, payload)
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
	})
}

func (p *Pool) Invalidate(ctx context.Context, accountID int64, tcpProxy string) error {
	return p.db.InvalidateSession(ctx, accountID, effectiveProxy(tcpProxy, p.cfg.TCPProxy))
}

func (p *Pool) run(ctx context.Context, loginBuffer string, accountID int64, tcpProxy string, op func(context.Context, WmpfSession) (map[string]any, error)) (map[string]any, error) {
	effective := effectiveProxy(tcpProxy, p.cfg.TCPProxy)
	st, err := p.state(ctx, loginBuffer, accountID, effective)
	if err == nil {
		res, err := op(ctx, st)
		if err == nil || effective == "" || !p.cfg.TCPProxyFallbackDirect {
			return res, err
		}
		_ = p.Invalidate(ctx, accountID, effective)
	}
	if effective != "" && p.cfg.TCPProxyFallbackDirect {
		st, err = p.state(ctx, loginBuffer, accountID, "")
		if err != nil {
			return nil, err
		}
		return op(ctx, st)
	}
	return nil, err
}

func (p *Pool) state(ctx context.Context, loginBuffer string, accountID int64, tcpProxy string) (WmpfSession, error) {
	if row, err := p.db.GetSession(ctx, accountID, tcpProxy); err == nil {
		return sessionFromBlob(row.SessionBlob)
	} else if !errors.Is(err, sql.ErrNoRows) {
		return WmpfSession{}, err
	}
	lock := p.lockFor(fmt.Sprintf("%d\x00%s", accountID, tcpProxy))
	lock.Lock()
	defer lock.Unlock()
	if row, err := p.db.GetSession(ctx, accountID, tcpProxy); err == nil {
		return sessionFromBlob(row.SessionBlob)
	} else if !errors.Is(err, sql.ErrNoRows) {
		return WmpfSession{}, err
	}
	acc, err := p.db.GetAccount(ctx, accountID)
	if err != nil {
		return WmpfSession{}, err
	}
	if err = acquire(ctx, p.loginSem); err != nil {
		return WmpfSession{}, err
	}
	defer release(p.loginSem)
	loginCtx, cancel := context.WithTimeout(ctx, p.cfg.LoginTimeout)
	defer cancel()
	st, err := p.loginAndSession(loginCtx, loginBuffer, tcpProxy)
	if err != nil {
		return WmpfSession{}, err
	}
	uin := st.Session.UIN
	_ = p.db.SetAccountUIN(ctx, acc.ID, uin)
	expiresAt := sessionExpiresAt(st, p.cfg.SessionTTL)
	blob := st.toBlob()
	if err = p.db.PutSession(ctx, accountID, &uin, blob, expiresAt, tcpProxy); err != nil {
		return WmpfSession{}, err
	}
	return st, nil
}

func (p *Pool) lockFor(key string) *sync.Mutex {
	p.mu.Lock()
	defer p.mu.Unlock()
	if l := p.locks[key]; l != nil {
		return l
	}
	l := &sync.Mutex{}
	p.locks[key] = l
	return l
}

func (p *Pool) loginAndSession(ctx context.Context, loginBuffer, tcpProxy string) (WmpfSession, error) {
	if loginBuffer == "" {
		return WmpfSession{}, fmt.Errorf("login_buffer is empty")
	}
	targets, err := getLonglinkTargets(ctx, p.cfg.LoginTimeout, p.cfg.DNSCacheTTL)
	if err != nil {
		return WmpfSession{}, fmt.Errorf("HTTPDNS LongLink failed: %w", err)
	}
	targets = orderLonglinkTargets(targets, 6)
	var last error
	for _, t := range targets {
		mc, err := connectMmtls(ctx, t, p.cfg.LoginTimeout, tcpProxy, p.cfg.TCPProxyFallbackDirect)
		if err != nil {
			last = err
			continue
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
			last = err
			continue
		}
		resp, err := mc.recvApp()
		if err != nil {
			last = err
			continue
		}
		if resp.Cmd != cmdManualAuth {
			last = fmt.Errorf("manualauth failed: cmd=%d", resp.Cmd)
			continue
		}
		mar, err := parseLoginResponse(resp.Body, temp)
		if err != nil {
			last = err
			continue
		}
		appSess, err := extractSession(mar)
		if err != nil {
			last = err
			continue
		}
		appSess.DeviceID = meta.DeviceID
		appSess.HostAppID = meta.HostAppID
		psks, err := mc.extractPSKs()
		if err != nil {
			last = err
			continue
		}
		psk, ok := pickAccessPSK(psks)
		if !ok {
			last = fmt.Errorf("login finished but no access PSK was issued")
			continue
		}
		shortTargets := getShortlinkTargets(ctx, p.cfg.ShortlinkTimeout, p.cfg.DNSCacheTTL)
		return WmpfSession{
			Session:          appSess,
			PSK:              psk,
			ShortlinkTargets: shortTargets,
			CreatedAt:        time.Now(),
			TCPProxy:         tcpProxy,
		}, nil
	}
	if last == nil {
		last = fmt.Errorf("no LongLink candidates")
	}
	return WmpfSession{}, fmt.Errorf("all LongLink candidates failed: %w", last)
}

func (p *Pool) sendEnvelope(ctx context.Context, st WmpfSession, envelope []byte) ([]byte, []byte, error) {
	if err := acquire(ctx, p.shortlinkSem); err != nil {
		return nil, nil, err
	}
	defer release(p.shortlinkSem)
	reqCtx, cancel := context.WithTimeout(ctx, p.cfg.ShortlinkTimeout)
	defer cancel()
	fallback := p.cfg.TCPProxyFallbackDirect
	if st.TCPProxy != "" {
		fallback = false
	}
	return send0RTT(reqCtx, st.ShortlinkTargets, st.PSK, st.Session.RecvKey, envelope, p.cfg.ShortlinkTimeout, st.TCPProxy, fallback)
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

func effectiveProxy(req, def string) string {
	if req != "" {
		return req
	}
	return def
}

func sessionExpiresAt(st WmpfSession, ttl time.Duration) int64 {
	now := time.Now().Unix()
	exp := st.PSK.ExpiredAt
	ttlExp := now + int64(ttl.Seconds())
	if exp == 0 || exp > ttlExp {
		exp = ttlExp
	}
	return exp
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
		st.ShortlinkTargets = []Target{{IP: "120.241.131.173", Port: 80}}
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
