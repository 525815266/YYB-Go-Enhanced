package protocol

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"sync"
	"time"
)

var (
	ErrShortlinkPayload = errors.New("AppData decrypt/parse failed")
	type8Prefix         = []byte{0x00, 0x00, 0x00, 0x10, 0x08, 0x00, 0x00, 0x00, 0x0b, 0x01, 0x00, 0x00, 0x00, 0x06, 0x00, 0x12}
	earlyAlert          = []byte{0x00, 0x00, 0x00, 0x03, 0x00, 0x01, 0x01}
)

// ShortlinkResponseError separates unauthenticated MMTLS records from an
// authenticated record whose application ciphertext/payload cannot be read.
// Stage is fixed diagnostic metadata; it contains no response bytes or keys.
type ShortlinkResponseError struct{ Stage string }

func (e *ShortlinkResponseError) Error() string {
	if e.Stage == "mmtls_records" {
		return "response missing ServerHello/AppData"
	}
	return fmt.Sprintf("%s: stage=%s", ErrShortlinkPayload, e.Stage)
}
func (e *ShortlinkResponseError) Unwrap() error {
	// Missing records were not a payload-rebuild signal before diagnostics
	// were added. Preserve that recovery policy for other protocol operations.
	if e.Stage == "mmtls_records" {
		return nil
	}
	return ErrShortlinkPayload
}

// ShortlinkRejectedError is an authenticated header rejection without an
// encrypted application body. The fixed code carries no request material.
type ShortlinkRejectedError struct {
	Code      int64
	Transport bool
}

func (e *ShortlinkRejectedError) Error() string {
	layer := "application"
	if e.Transport {
		layer = "transport"
	}
	return fmt.Sprintf("shortlink %s rejected: code=%d", layer, e.Code)
}

// Retain the payload error category; application -14 is excluded from rebuilds.
func (e *ShortlinkRejectedError) Unwrap() error { return ErrShortlinkPayload }

// A bare authenticated application -14 has also been observed between two
// successful calls using one session. It alone does not justify deleting the
// shared session or rotating account credentials. A later real ManualAuth
// rejection remains a separate recovery case.
func IsApplicationRequestRejection(err error) bool {
	switch e := err.(type) {
	case *ShortlinkRejectedError:
		return !e.Transport && e.Code == -14
	case interface{ Unwrap() []error }:
		children := e.Unwrap()
		if len(children) == 0 {
			return false
		}
		for _, child := range children {
			if !IsApplicationRequestRejection(child) {
				return false
			}
		}
		return true
	case interface{ Unwrap() error }:
		return IsApplicationRequestRejection(e.Unwrap())
	}
	return false
}

func build0RTTRequest(entry pskEntry, envelope []byte) ([]byte, []byte, []byte, []byte, error) {
	psk, err := hex.DecodeString(entry.PreSharedKey)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	ticket, err := hex.DecodeString(entry.TicketEntry)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	randomBytes := make([]byte, 32)
	if _, err = rand.Read(randomBytes); err != nil {
		return nil, nil, nil, nil, err
	}
	ts := uint32(time.Now().Unix())
	pskCH, err := buildPSKClientHello(randomBytes, ts, ticket)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	hEarly := sha256.Sum256(pskCH)
	ek := derivePSKOneWayKeys(psk, labelEarlyKeys, hEarly[:])
	type8 := append([]byte(nil), type8Prefix...)
	var tmp [4]byte
	binary.BigEndian.PutUint32(tmp[:], ts)
	type8 = append(type8, tmp[:]...)

	body := []byte{}
	r0, _ := buildRecord(ctPSKHandshake, pskCH)
	body = append(body, r0...)
	ct1, err := encryptRecordPayload(ek.Key, ek.IV, 1, ctPSKHandshake, type8)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	r1, _ := buildRecord(ctPSKHandshake, ct1)
	body = append(body, r1...)
	ct2, err := encryptRecordPayload(ek.Key, ek.IV, 2, ctAppData, envelope)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	r2, _ := buildRecord(ctAppData, ct2)
	body = append(body, r2...)
	ct3, err := encryptRecordPayload(ek.Key, ek.IV, 3, ctAlert, earlyAlert)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	r3, _ := buildRecord(ctAlert, ct3)
	body = append(body, r3...)
	return body, psk, pskCH, type8, nil
}

func httpPost(path, host string, body []byte) []byte {
	head := fmt.Sprintf("POST %s HTTP/1.0\r\nAccept: */*\r\nCache-Control: no-cache\r\nConnection: close\r\nContent-Length: %d\r\nContent-Type: application/octet-stream\r\nHost: %s\r\nUpgrade: mmtls\r\nUser-Agent: MicroMessenger Client\r\nX-Online-Host: %s\r\n\r\n", path, len(body), host, host)
	return append([]byte(head), body...)
}

// A failed connection has not sent the authorization payload, so selecting
// another endpoint remains safe even when response-based replay is disabled.
type shortlinkDialError struct{ error }

func (e *shortlinkDialError) Unwrap() error { return e.error }

// IsTimeoutError reports whether err is a context deadline, socket I/O
// deadline, or network timeout error.
func IsTimeoutError(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, os.ErrDeadlineExceeded) {
		return true
	}
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}

const (
	maxShortlinkAttempts    = 4
	shortlinkBaseCooldown   = 5 * time.Minute
	shortlinkMaxCooldown    = 30 * time.Minute
	shortlinkStrikeResetGap = 2 * time.Hour
)

type shortlinkHealthKey struct {
	ip    string
	port  int
	proxy string
}

type shortlinkPenalty struct {
	until   time.Time
	strikes int
	last    time.Time
}

// shortlinkHealth tracks process-local ShortLink endpoint timeouts/failures per
// egress route so a blackholed IP (e.g. a silent node in an old session blob)
// is placed behind healthy nodes instead of stalling every call.
var shortlinkHealth = struct {
	mu      sync.Mutex
	entries map[shortlinkHealthKey]shortlinkPenalty
	now     func() time.Time
}{
	entries: map[shortlinkHealthKey]shortlinkPenalty{},
	now:     time.Now,
}

func resetShortlinkHealth() {
	shortlinkHealth.mu.Lock()
	shortlinkHealth.entries = map[shortlinkHealthKey]shortlinkPenalty{}
	shortlinkHealth.now = time.Now
	shortlinkHealth.mu.Unlock()
}

func recordShortlinkSuccess(t Target, tcpProxy string) {
	key := shortlinkHealthKey{ip: t.IP, port: t.Port, proxy: tcpProxy}
	shortlinkHealth.mu.Lock()
	delete(shortlinkHealth.entries, key)
	shortlinkHealth.mu.Unlock()
}

func recordShortlinkFailure(t Target, tcpProxy string) {
	key := shortlinkHealthKey{ip: t.IP, port: t.Port, proxy: tcpProxy}
	shortlinkHealth.mu.Lock()
	defer shortlinkHealth.mu.Unlock()
	now := shortlinkHealth.now()
	p := shortlinkHealth.entries[key]
	if !p.last.IsZero() && now.Sub(p.last) > shortlinkStrikeResetGap {
		p.strikes = 0
	}
	p.strikes++
	cooldown := shortlinkBaseCooldown
	for i := 1; i < p.strikes && cooldown < shortlinkMaxCooldown; i++ {
		if cooldown > shortlinkMaxCooldown/2 {
			cooldown = shortlinkMaxCooldown
			break
		}
		cooldown *= 2
	}
	if cooldown > shortlinkMaxCooldown {
		cooldown = shortlinkMaxCooldown
	}
	p.until = now.Add(cooldown)
	p.last = now
	shortlinkHealth.entries[key] = p
}

func isShortlinkDemotedLocked(t Target, tcpProxy string, now time.Time) bool {
	p, ok := shortlinkHealth.entries[shortlinkHealthKey{ip: t.IP, port: t.Port, proxy: tcpProxy}]
	return ok && now.Before(p.until)
}

// selectShortlinkTargets normalizes candidate ordering, heals cached public
// sessions that lacked fallback targets, places cooled endpoints after healthy
// ones, and bounds per-call attempts while keeping an official hostname slot.
// Offline loopback/private targets remain hermetic for unit tests.
func selectShortlinkTargets(targets []Target, tcpProxy string) []Target {
	seen := make(map[Target]bool, len(targets)+len(shortlinkFallbackTargets)+1)
	deduped := make([]Target, 0, len(targets)+len(shortlinkFallbackTargets)+1)
	for _, t := range targets {
		if t.IP == "" || seen[t] {
			continue
		}
		seen[t] = true
		deduped = append(deduped, t)
	}
	publicMode := len(deduped) == 0 || hasPublicShortlinkTarget(deduped)
	if publicMode {
		for _, t := range shortlinkFallbackTargets {
			if t.IP == "" || t.Port <= 0 || seen[t] {
				continue
			}
			seen[t] = true
			deduped = append(deduped, t)
		}
		domainTarget := Target{IP: shortlinkDomain, Port: 80}
		if !seen[domainTarget] {
			seen[domainTarget] = true
			deduped = append(deduped, domainTarget)
		}
	}

	shortlinkHealth.mu.Lock()
	now := shortlinkHealth.now()
	healthy := make([]Target, 0, len(deduped))
	demoted := make([]Target, 0, len(deduped))
	for _, t := range deduped {
		if isShortlinkDemotedLocked(t, tcpProxy, now) {
			demoted = append(demoted, t)
		} else {
			healthy = append(healthy, t)
		}
	}
	shortlinkHealth.mu.Unlock()

	ordered := make([]Target, 0, len(deduped))
	ordered = append(ordered, healthy...)
	ordered = append(ordered, demoted...)
	if !publicMode || len(ordered) <= maxShortlinkAttempts {
		return ordered
	}

	domainTarget := Target{IP: shortlinkDomain, Port: 80}
	out := append([]Target(nil), ordered[:maxShortlinkAttempts]...)
	hasDomain := false
	for _, t := range out {
		if t == domainTarget {
			hasDomain = true
			break
		}
	}
	if !hasDomain {
		shortlinkHealth.mu.Lock()
		domainHealthy := !isShortlinkDemotedLocked(domainTarget, tcpProxy, shortlinkHealth.now())
		shortlinkHealth.mu.Unlock()
		if domainHealthy {
			out[maxShortlinkAttempts-1] = domainTarget
		}
	}
	return out
}

func shouldDemoteShortlinkError(err error) bool {
	if err == nil {
		return false
	}
	var rejected *ShortlinkRejectedError
	var respErr *ShortlinkResponseError
	if errors.As(err, &rejected) || errors.As(err, &respErr) {
		return false
	}
	return true
}

func send0RTT(ctx context.Context, targets []Target, entry pskEntry, recvKey, envelope []byte, timeout time.Duration, tcpProxy string, fallbackDirect, allowReplay bool) ([]byte, []byte, error) {
	if _, hasDeadline := ctx.Deadline(); !hasDeadline && timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	candidates := selectShortlinkTargets(targets, tcpProxy)
	var last error
	for i, t := range candidates {
		if err := ctx.Err(); err != nil {
			if last != nil {
				return nil, nil, fmt.Errorf("ShortLink targets failed before completion (%w); last error: %w", err, last)
			}
			return nil, nil, err
		}
		remaining := timeout
		if deadline, hasDeadline := ctx.Deadline(); hasDeadline {
			remaining = time.Until(deadline)
			if remaining <= 0 {
				err := ctx.Err()
				if err == nil {
					err = context.DeadlineExceeded
				}
				if last != nil {
					return nil, nil, fmt.Errorf("ShortLink targets failed before completion (%w); last error: %w", err, last)
				}
				return nil, nil, err
			}
		}
		left := len(candidates) - i
		attemptBudget := remaining
		if left > 1 && remaining > 0 {
			// Split remaining budget across candidates so one silent IP cannot starve
			// remaining ShortLink endpoints before failover runs.
			attemptBudget = max(remaining/time.Duration(left), time.Millisecond)
		}
		code, resp, err := send0RTTRawAttempt(ctx, t, entry, recvKey, envelope, attemptBudget, remaining, tcpProxy, fallbackDirect, allowReplay)
		if err == nil && (len(code) > 0 || len(resp) > 0) {
			recordShortlinkSuccess(t, tcpProxy)
			return code, resp, nil
		}
		if err != nil {
			last = err
		} else {
			last = fmt.Errorf("ShortLink response did not contain application data")
		}
		var rejected *ShortlinkRejectedError
		if errors.As(err, &rejected) {
			recordShortlinkSuccess(t, tcpProxy)
			return nil, nil, fmt.Errorf("ShortLink rejected by %s:%d: %w", t.IP, t.Port, err)
		}
		if ctx.Err() == nil && shouldDemoteShortlinkError(last) {
			recordShortlinkFailure(t, tcpProxy)
		}
		var dialErr *shortlinkDialError
		if !allowReplay && !errors.As(err, &dialErr) {
			return nil, nil, fmt.Errorf("ShortLink response failed (authorization not replayed): %w", last)
		}
	}
	if last == nil {
		last = fmt.Errorf("no ShortLink targets")
	}
	return nil, nil, fmt.Errorf("all ShortLink targets failed; last error: %w", last)
}

func send0RTTRaw(ctx context.Context, target Target, entry pskEntry, recvKey, envelope []byte, timeout time.Duration, tcpProxy string, fallbackDirect bool) ([]byte, []byte, error) {
	return send0RTTRawAttempt(ctx, target, entry, recvKey, envelope, timeout, timeout, tcpProxy, fallbackDirect, true)
}

func send0RTTRawAttempt(ctx context.Context, target Target, entry pskEntry, recvKey, envelope []byte, dialBudget, totalBudget time.Duration, tcpProxy string, fallbackDirect, boundReadBudget bool) ([]byte, []byte, error) {
	body, psk, pskCH, type8, err := build0RTTRequest(entry, envelope)
	if err != nil {
		return nil, nil, err
	}
	path := fmt.Sprintf("/mmtls/%08x", uint32(time.Now().Unix()))
	req := httpPost(path, "shortcloud.weixin.com", body)

	dialCtx := ctx
	var cancelDial context.CancelFunc
	if dialBudget > 0 {
		dialCtx, cancelDial = context.WithTimeout(ctx, dialBudget)
	}
	conn, err := dialTCP(dialCtx, target.IP, target.Port, dialBudget, tcpProxy, fallbackDirect)
	if cancelDial != nil {
		cancelDial()
	}
	if err != nil {
		return nil, nil, &shortlinkDialError{err}
	}
	defer conn.Close()

	ioCtx := ctx
	readTimeout := totalBudget
	var cancelIO context.CancelFunc
	if boundReadBudget && dialBudget > 0 {
		ioCtx, cancelIO = context.WithTimeout(ctx, dialBudget)
		readTimeout = dialBudget
	}
	if cancelIO != nil {
		defer cancelIO()
	}
	stopCancel := context.AfterFunc(ioCtx, func() { _ = conn.Close() })
	defer stopCancel()
	_ = conn.SetDeadline(ioDeadline(ioCtx, readTimeout))
	if _, err = conn.Write(req); err != nil {
		if ctxErr := ioCtx.Err(); ctxErr != nil && !IsTimeoutError(err) {
			err = ctxErr
		}
		return nil, nil, err
	}
	raw, err := io.ReadAll(conn)
	if err != nil {
		if ctxErr := ioCtx.Err(); ctxErr != nil && !IsTimeoutError(err) {
			err = ctxErr
		}
		return nil, nil, fmt.Errorf("ShortLink response from %s:%d failed: %w", target.IP, target.Port, err)
	}
	if !stopCancel() {
		return nil, nil, ioCtx.Err()
	}
	_, responseBody := splitHTTP(raw)
	if len(responseBody) == 0 {
		return nil, nil, fmt.Errorf("empty ShortLink response")
	}
	return parse0RTTResponse(responseBody, psk, pskCH, type8, recvKey)
}

func parse0RTTResponse(rbody, psk, pskCH, type8, recvKey []byte) ([]byte, []byte, error) {
	recs, _ := parseRecords(rbody)
	var sh []byte
	var encHS [][]byte
	var appdata []byte
	for _, r := range recs {
		if r.ContentType == ctHandshake {
			if sh == nil {
				sh = r.Payload
			}
			encHS = append(encHS, r.Payload)
		}
		if r.ContentType == ctAppData && appdata == nil {
			appdata = r.Payload
		}
	}
	if sh == nil || appdata == nil {
		return nil, nil, &ShortlinkResponseError{Stage: "mmtls_records"}
	}
	candidates := [][]byte{
		bytes.Join([][]byte{pskCH, sh}, nil),
		bytes.Join([][]byte{pskCH, type8, sh}, nil),
		bytes.Join([][]byte{pskCH, sh, type8}, nil),
	}
	authenticated := false
	for _, trans := range candidates {
		h := sha256.Sum256(trans)
		hk := derivePSKOneWayKeys(psk, labelHandshakeKeys, h[:])
		for _, seq := range []uint64{2, 1, 3} {
			ad, err := decryptRecordPayload(hk.Key, hk.IV, seq, ctAppData, appdata)
			if err != nil {
				continue
			}
			authenticated = true
			if sl, err := parseShortlink(ad); err == nil {
				if rejection := shortlinkHeaderRejection(sl.Body); rejection != nil {
					return nil, nil, rejection
				}
				if resp, err := sessionDecrypt(sl.Body, recvKey); err == nil {
					return extractCode(resp), resp, nil
				}
			}
			if rejection := shortlinkHeaderRejection(ad); rejection != nil {
				return nil, nil, rejection
			}
			if resp, err := sessionDecrypt(ad, recvKey); err == nil {
				return extractCode(resp), resp, nil
			}
		}
	}
	stage := "mmtls_decrypt"
	if authenticated {
		stage = "session_payload"
	}
	return nil, nil, &ShortlinkResponseError{Stage: stage}
}

func shortlinkHeaderRejection(body []byte) error {
	fields, _, size, err := decodeWpkgHead(body)
	if err != nil || size != len(body) {
		return nil
	}
	var causes []error
	if code := int64(fields[4]); code != 0 {
		causes = append(causes, &ShortlinkRejectedError{Code: code, Transport: true})
	}
	if code := int64(fields[23]); code != 0 {
		causes = append(causes, &ShortlinkRejectedError{Code: code})
	}
	return errors.Join(causes...)
}
