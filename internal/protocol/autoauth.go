package protocol

import (
	"context"
	"crypto/ecdsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"log"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

const cmdAutoAuth = 3198

// Independent ILink recovery material, bound to the device from ManualAuth.
// AttemptedAt is persisted before sending a rotating key, so a restart cannot
// replay a request whose server-side result was lost.
type autoAuthState struct {
	Device      []byte
	Key         []byte
	EncKey      []byte
	AttemptedAt int64
}

func (s autoAuthState) usable() bool {
	return len(s.Device) > 0 && len(s.Key) > 0 && len(s.EncKey) == 24 && s.AttemptedAt == 0
}

type AutoAuthRejectedError struct {
	Stage string
	Code  int64
}

func (e *AutoAuthRejectedError) Error() string {
	return fmt.Sprintf("native autoauth %s rejected: code=%d", e.Stage, e.Code)
}

func autoAuthFromResponse(unified, device []byte) autoAuthState {
	sect := pbParse(safeBytes(pbParse(unified)[2]))
	return autoAuthState{Device: append([]byte(nil), device...), Key: safeBytes(sect[3]), EncKey: safeBytes(sect[4])}
}

func (p *Pool) nativeRecoveryState(ctx context.Context, accountID int64, proxy string) (WmpfSession, error) {
	if p.db == nil {
		return WmpfSession{}, ErrCachedSessionUnavailable
	}
	row, err := p.db.GetSessionForRecovery(ctx, accountID, proxy)
	if err != nil {
		return WmpfSession{}, err
	}
	saved, err := sessionFromBlob(row.SessionBlob)
	if err != nil || saved.TCPProxy != proxy || !saved.AutoAuth.usable() || len(saved.Session.F9) == 0 || len(saved.Session.DeviceID) == 0 {
		return WmpfSession{}, ErrCachedSessionUnavailable
	}
	saved.recoveryRow = row
	return saved, nil
}

func (p *Pool) hasNativeRecovery(ctx context.Context, accountID int64, proxy string) bool {
	_, err := p.nativeRecoveryState(ctx, accountID, proxy)
	return err == nil
}

func buildAutoAuth(saved WmpfSession, temp *manualAuthTemp) ([]byte, error) {
	if !saved.AutoAuth.usable() || saved.Session.UIN == 0 || len(saved.Session.F9) == 0 || len(saved.Session.DeviceID) == 0 {
		return nil, errors.New("native autoauth state incomplete or already attempted")
	}
	request := append(pbLen(1, pbLen(1, saved.AutoAuth.Device)), pbVar(2, appVersion)...)
	request = append(request, pbLen(3, saved.AutoAuth.Key)...)
	hybrid, err := hybridECDHEncryptWithKey(request, serverPub, saved.AutoAuth.EncKey, temp)
	if err != nil {
		return nil, err
	}
	ints, byts, _, err := decodeWpkgHead(sessionWpkgHead(saved.Session.UIN, saved.Session.F9, saved.Session.DeviceID))
	if err != nil {
		return nil, err
	}
	ints[25] = 17
	// App instance clears IsAppAccount. Its TDI ServerId (auth field 9),
	// rather than the display username, becomes wpkg bytes field 27.
	return append(wpkgHead(ints, byts), hybrid...), nil
}

const applicationSigningPublicKey = `-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAElsQBff9Ng3kBHy/H+EH4c86qn/iS
0zRmO0XcMQJdxeATvpBKd0BtdhSdPAvYkB/b/ho5Z4o5wwEJW94vlAFCag==
-----END PUBLIC KEY-----`

func verifyHybridSignature(requestCipher, response []byte) error {
	block, _ := pem.Decode([]byte(applicationSigningPublicKey))
	key, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return err
	}
	pub, ok := key.(*ecdsa.PublicKey)
	if !ok {
		return errors.New("invalid native signing public key")
	}
	return verifyHybridSignatureWithKey(pub, requestCipher, response)
}

func verifyHybridSignatureWithKey(pub *ecdsa.PublicKey, requestCipher, response []byte) error {
	fields := pbParse(response)
	cipher, sig := safeBytes(fields[3]), safeBytes(fields[4])
	if len(requestCipher) == 0 || len(cipher) == 0 || len(sig) == 0 {
		return errors.New("native signature inputs incomplete")
	}
	h := sha256.New()
	_, _ = h.Write(requestCipher)
	_, _ = h.Write(cipher)
	// Native EcdsaVerify hashes the HashObj digest a second time.
	digest := sha256.Sum256(h.Sum(nil))
	if !ecdsa.VerifyASN1(pub, digest[:], sig) {
		return errors.New("native response signature mismatch")
	}
	return nil
}

func parseAutoAuthResponse(packet shortlinkPacket, saved WmpfSession, temp *manualAuthTemp) (WmpfSession, error) {
	if temp == nil || temp.Ephemeral == nil || len(temp.OKM) != 56 {
		return WmpfSession{}, errors.New("native autoauth request state incomplete")
	}
	if packet.Cmd != cmdAutoAuth {
		return WmpfSession{}, errors.New("unexpected native autoauth response command")
	}
	ints, _, hlen, err := decodeWpkgHead(packet.Body)
	if err != nil || hlen >= len(packet.Body) {
		if err == nil && int64(ints[4]) != 0 {
			return WmpfSession{}, &AutoAuthRejectedError{"transport", int64(ints[4])}
		}
		if err == nil && int64(ints[23]) != 0 {
			return WmpfSession{}, &AutoAuthRejectedError{"application", int64(ints[23])}
		}
		return WmpfSession{}, errors.New("native autoauth response missing body")
	}
	if int64(ints[4]) != 0 {
		return WmpfSession{}, &AutoAuthRejectedError{"transport", int64(ints[4])}
	}
	if int64(ints[23]) != 0 {
		return WmpfSession{}, &AutoAuthRejectedError{"application", int64(ints[23])}
	}
	response := packet.Body[hlen:]
	credType, _ := pbParse(response)[2].(int64)
	if credType != 1 && credType != 2 {
		return WmpfSession{}, errors.New("unsupported native credential type")
	}
	if credType == 1 {
		if err = verifyHybridSignature(temp.RequestCipher, response); err != nil {
			return WmpfSession{}, err
		}
	}
	decoded, err := hybridECDHDecrypt(response, temp)
	if err != nil {
		return WmpfSession{}, errors.New("native autoauth response authentication failed")
	}
	fields := pbParse(decoded)
	base, unified := safeBytes(fields[1]), safeBytes(fields[2])
	if len(base) == 0 {
		return WmpfSession{}, errors.New("native autoauth response missing result")
	}
	if code, _ := pbParse(base)[1].(int64); code != 0 {
		return WmpfSession{}, &AutoAuthRejectedError{"business", code}
	}
	session, err := extractSession(append(pbLen(1, base), pbLen(3, unified)...))
	if err != nil {
		return WmpfSession{}, errors.New("native autoauth session incomplete")
	}
	next := autoAuthFromResponse(unified, saved.AutoAuth.Device)
	if session.UIN != saved.Session.UIN || !next.usable() || len(session.F9) == 0 {
		return WmpfSession{}, errors.New("native autoauth identity or rotated keys incomplete")
	}
	session.DeviceID, session.HostAppID = saved.Session.DeviceID, saved.Session.HostAppID
	saved.Session, saved.AutoAuth, saved.CreatedAt = session, next, time.Now()
	return saved, nil
}

// Connect once, send once. In particular, never retry a rotating AutoKey on
// another node after an ambiguous send/receive failure.
func (p *Pool) autoAuthSession(ctx context.Context, saved WmpfSession, consume func() error) (WmpfSession, error) {
	temp := &manualAuthTemp{}
	body, err := buildAutoAuth(saved, temp)
	if err != nil {
		return WmpfSession{}, err
	}
	targets, err := getLonglinkTargets(ctx, min(p.cfg.ShortlinkTimeout, p.cfg.LoginTimeout/3), p.cfg.DNSCacheTTL)
	if err != nil || len(targets) == 0 {
		return WmpfSession{}, errors.New("native autoauth target unavailable")
	}
	mc, err := connectMmtls(ctx, targets[0], p.cfg.LoginTimeout, saved.TCPProxy, false)
	if err != nil {
		return WmpfSession{}, err
	}
	defer mc.close()
	if err = consume(); err != nil {
		return WmpfSession{}, err
	}
	if err = mc.sendApp(cmdAutoAuth, body); err != nil {
		return WmpfSession{}, err
	}
	packet, err := mc.recvApp()
	if err != nil {
		return WmpfSession{}, err
	}
	next, err := parseAutoAuthResponse(packet, saved, temp)
	if err != nil {
		return WmpfSession{}, err
	}
	keys, err := mc.extractPSKs()
	if err != nil {
		next.PSK = pskEntry{ExpiredAt: time.Now().Add(-time.Second).Unix()}
		return next, err
	}
	psk, ok := pickAccessPSK(keys)
	if !ok {
		next.PSK = pskEntry{ExpiredAt: time.Now().Add(-time.Second).Unix()}
		return next, errors.New("native autoauth returned no access PSK")
	}
	next.PSK = psk
	return next, nil
}

// Caller holds the account/route state lock. Save consumption before sending,
// and save rotation before any application probe can fail or be cancelled.
func (p *Pool) restoreNativeSession(ctx context.Context, acc *store.WechatAccount, saved WmpfSession) (WmpfSession, error) {
	if acc == nil || acc.Status != nil && *acc.Status == "expired" || !saved.AutoAuth.usable() || saved.recoveryRow == nil {
		return WmpfSession{}, ErrCachedSessionUnavailable
	}
	return p.rotateNativeSession(ctx, acc, saved, p.autoAuthSession)
}

func (p *Pool) rotateNativeSession(ctx context.Context, acc *store.WechatAccount, saved WmpfSession, exchange func(context.Context, WmpfSession, func() error) (WmpfSession, error)) (WmpfSession, error) {
	p.events.Emit("protocol.native_started", eventlog.ForAccount(acc))
	var markedRow *store.SessionRow
	next, err := exchange(ctx, saved, func() error {
		marked := saved
		marked.AutoAuth.AttemptedAt = time.Now().Unix()
		var err error
		markedRow, err = p.db.ReplaceNativeSessionForAccount(ctx, acc, saved.recoveryRow, saved.Session.UIN, marked.toBlob(), sessionExpiresAt(saved, p.cfg.SessionTTL))
		return err
	})
	if err != nil && !next.AutoAuth.usable() {
		fields := eventlog.ForAccount(acc)
		var rejected *AutoAuthRejectedError
		if errors.As(err, &rejected) {
			fields.Rejections = []eventlog.Rejection{{Stage: "native_autoauth_" + rejected.Stage, Code: rejected.Code}}
		}
		p.events.Emit("protocol.native_failed", fields)
		return WmpfSession{}, err
	}
	exchangeErr := err
	saveCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	if next.recoveryRow, err = p.db.ReplaceNativeSessionForAccount(saveCtx, acc, markedRow, next.Session.UIN, next.toBlob(), sessionExpiresAt(next, p.cfg.SessionTTL)); err != nil {
		return WmpfSession{}, err
	}
	next.nativePersisted = true
	if exchangeErr != nil {
		p.events.Emit("protocol.native_incomplete", eventlog.ForAccount(acc))
		log.Printf("protocol: account id=%d native keys rotated; transport state incomplete", acc.ID)
	} else {
		p.events.Emit("protocol.native_rotated", eventlog.ForAccount(acc))
		log.Printf("protocol: account id=%d native session renewed", acc.ID)
	}
	return next, exchangeErr
}
