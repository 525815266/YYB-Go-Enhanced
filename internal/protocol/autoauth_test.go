package protocol

import (
	"bytes"
	"context"
	"crypto/ecdh"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/store"
)

func syntheticNativeSession() WmpfSession {
	return WmpfSession{
		Session: AppSession{UIN: 1234, F9: []byte("synthetic-server-id"), DeviceID: []byte("synthetic-ticket-device"),
			SendKey: []byte("synthetic-send"), RecvKey: []byte("synthetic-recv"), HostAppID: []byte("synthetic-host")},
		AutoAuth: autoAuthState{Device: []byte("synthetic-app-device"), Key: []byte("synthetic-auto-key"), EncKey: bytes.Repeat([]byte{7}, 24)},
		PSK:      pskEntry{PSKType: 1, PreSharedKey: "001122", TicketEntry: "aabbcc", ExpiredAt: time.Now().Add(time.Hour).Unix()},
	}
}

func TestNativeAutoAuthRequestUsesServerIDAndSavedDevice(t *testing.T) {
	st := syntheticNativeSession()
	temp := &manualAuthTemp{}
	packet, err := buildAutoAuth(st, temp)
	if err != nil {
		t.Fatal(err)
	}
	ints, fields, _, err := decodeWpkgHead(packet)
	if err != nil || ints[2] != uint64(st.Session.UIN) || ints[18] != 1 || ints[25] != 17 || ints[7] != 0 || !bytes.Equal(fields[27], st.Session.F9) {
		t.Fatal("native application header differs from verified layout")
	}
	plain, err := lz4Decompress(temp.CompBody)
	if err != nil {
		t.Fatal(err)
	}
	f := pbParse(plain)
	if f[2] != int64(appVersion) || !bytes.Equal(safeBytes(pbParse(safeBytes(f[1]))[1]), st.AutoAuth.Device) || !bytes.Equal(safeBytes(f[3]), st.AutoAuth.Key) || f[4] != nil {
		t.Fatal("native request lost original device or key")
	}
	st.AutoAuth.AttemptedAt = 1
	if _, err := buildAutoAuth(st, &manualAuthTemp{}); err == nil {
		t.Fatal("consumed key replay allowed")
	}
}

func TestNativeSignatureBindsBothCiphertextsWithDoubleHash(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	request, response := []byte("synthetic-request"), []byte("synthetic-response")
	first := sha256.Sum256(append(append([]byte(nil), request...), response...))
	digest := sha256.Sum256(first[:])
	sig, err := ecdsa.SignASN1(rand.Reader, key, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	encoded := append(pbLen(3, response), pbLen(4, sig)...)
	if err := verifyHybridSignatureWithKey(&key.PublicKey, request, encoded); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ req, resp []byte }{
		{[]byte("other-request"), encoded},
		{request, append(pbLen(3, []byte("other-response")), pbLen(4, sig)...)},
		{nil, encoded},
	} {
		if verifyHybridSignatureWithKey(&key.PublicKey, tc.req, tc.resp) == nil {
			t.Fatal("unbound or tampered response accepted")
		}
	}
	single, _ := ecdsa.SignASN1(rand.Reader, key, first[:])
	if verifyHybridSignatureWithKey(&key.PublicKey, request, append(pbLen(3, response), pbLen(4, single)...)) == nil {
		t.Fatal("single-hash signature accepted")
	}
}

// Synthetic peer exercises authenticated parsing without any production keys.
func syntheticAutoResponse(t *testing.T, temp *manualAuthTemp, code int64, uin int64, rotated bool) shortlinkPacket {
	t.Helper()
	sect := append(pbLen(1, []byte("next-send")), pbLen(2, []byte("next-recv"))...)
	sect = append(sect, pbLen(9, []byte("next-server"))...)
	if rotated {
		sect = append(sect, pbLen(3, []byte("next-auto-key"))...)
		sect = append(sect, pbLen(4, bytes.Repeat([]byte{8}, 24))...)
	}
	unified := append(pbLen(2, sect), pbLen(3, pbVar(1, uint64(uin)))...)
	plain := append(pbLen(1, pbVar(1, uint64(code))), pbLen(2, unified)...)
	peer, _ := ecdh.P256().GenerateKey(rand.Reader)
	shared, err := peer.ECDH(temp.Ephemeral.PublicKey())
	if err != nil {
		t.Fatal(err)
	}
	secret := sha256.Sum256(shared)
	pub := peer.PublicKey().Bytes()
	aad := sha256.Sum256(bytes.Join([][]byte{temp.OKM[24:56], temp.CompBody, []byte("415"), pub, []byte("2")}, nil))
	cipher, err := aesGCMEncryptLayout(secret[:24], aad[:], lz4AllLiteral(plain))
	if err != nil {
		t.Fatal(err)
	}
	response := append(pbLen(1, append(pbVar(1, 415), pbLen(2, pub)...)), pbVar(2, 2)...)
	response = append(response, pbLen(3, cipher)...)
	return shortlinkPacket{Cmd: cmdAutoAuth, Body: append(wpkgHead(map[int]uint64{4: 0, 23: 0}, nil), response...)}
}

func TestNativeAutoAuthResponseRequiresAuthenticatedRotationAndIdentity(t *testing.T) {
	saved := syntheticNativeSession()
	for _, outcome := range []string{"success", "business_rejected", "other_identity", "missing_keys", "tamper", "wrong_command", "missing_request"} {
		t.Run(outcome, func(t *testing.T) {
			temp := &manualAuthTemp{}
			if _, err := buildAutoAuth(saved, temp); err != nil {
				t.Fatal(err)
			}
			code, uin, rotated := int64(0), saved.Session.UIN, true
			if outcome == "business_rejected" {
				code = -13
			}
			if outcome == "other_identity" {
				uin++
			}
			if outcome == "missing_keys" {
				rotated = false
			}
			packet := syntheticAutoResponse(t, temp, code, uin, rotated)
			if outcome == "tamper" {
				packet.Body[len(packet.Body)-1] ^= 1
			}
			if outcome == "wrong_command" {
				packet.Cmd++
			}
			if outcome == "missing_request" {
				temp = nil
			}
			next, err := parseAutoAuthResponse(packet, saved, temp)
			if outcome != "success" {
				if err == nil {
					t.Fatal("invalid native response accepted")
				}
				return
			}
			if err != nil || !next.AutoAuth.usable() || bytes.Equal(next.AutoAuth.Key, saved.AutoAuth.Key) || !bytes.Equal(next.Session.DeviceID, saved.Session.DeviceID) {
				t.Fatal("rotation failed", err)
			}
		})
	}
}

func TestNativeRotationPersistsConsumptionAndSurvivesCancelledProbe(t *testing.T) {
	for _, outcome := range []string{"success", "lost_response", "lost_psk", "cancelled", "rescan", "manual_expiry"} {
		t.Run(outcome, func(t *testing.T) {
			ctx := context.Background()
			db, err := store.Open(filepath.Join(t.TempDir(), "test.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			status := "alive"
			acc, err := db.UpsertAccount(ctx, "synthetic-native", "buffer", nil, nil, nil, nil, map[string]any{"last_scan_at": 123}, &status)
			if err != nil {
				t.Fatal(err)
			}
			st := syntheticNativeSession()
			if err := db.PutSessionForAccount(ctx, acc, st.Session.UIN, st.toBlob(), st.PSK.ExpiredAt, ""); err != nil {
				t.Fatal(err)
			}
			if _, err := db.ApplyCredentialRefresh(ctx, acc, "", nil, store.StatusSessionOnly); err != nil {
				t.Fatal(err)
			}
			acc, _ = db.GetAccount(ctx, acc.ID)
			pool := NewPool(DefaultConfig(), db)
			saved, err := pool.nativeRecoveryState(ctx, acc.ID, "")
			if err != nil {
				t.Fatal(err)
			}
			callCtx, cancel := context.WithCancel(ctx)
			defer cancel()
			exchange := func(ctx context.Context, old WmpfSession, consume func() error) (WmpfSession, error) {
				if err := consume(); err != nil {
					return WmpfSession{}, err
				}
				// Recreate the pool to prove that the consumption marker survives restarts.
				if NewPool(DefaultConfig(), db).hasNativeRecovery(ctx, acc.ID, "") {
					t.Fatal("spent key survived restart as usable")
				}
				if outcome == "lost_response" {
					return WmpfSession{}, errors.New("synthetic lost response")
				}
				if outcome == "rescan" {
					_ = db.SetAccountCredential(ctx, acc.ID, "new-scan", map[string]any{"scan": 456})
				}
				if outcome == "manual_expiry" {
					_ = db.SetAccountStatus(ctx, acc.ID, "expired")
				}
				if outcome == "cancelled" {
					cancel()
				}
				old.AutoAuth.Key = []byte("rotated-auto-key")
				if outcome == "lost_psk" {
					old.PSK = pskEntry{ExpiredAt: time.Now().Add(-time.Second).Unix()}
					return old, errors.New("synthetic PSK extraction failure")
				}
				return old, nil
			}
			next, err := pool.rotateNativeSession(callCtx, acc, saved, exchange)
			if outcome == "success" || outcome == "cancelled" || outcome == "lost_psk" {
				if outcome != "lost_psk" && err != nil || outcome == "lost_psk" && err == nil || !next.nativePersisted {
					t.Fatal("rotation was lost", err)
				}
				restarted := NewPool(DefaultConfig(), db)
				got, err := restarted.nativeRecoveryState(ctx, acc.ID, "")
				if err != nil || string(got.AutoAuth.Key) != "rotated-auto-key" {
					t.Fatal("new key missing after restart")
				}
				if outcome == "lost_psk" {
					if _, err := restarted.cachedSession(ctx, acc.ID, ""); err == nil {
						t.Fatal("old PSK survived session key rotation")
					}
				}
				current, _ := db.GetAccount(ctx, acc.ID)
				if !current.SameAuthorization(acc) || !current.SessionOnly() || current.LoginBuffer != acc.LoginBuffer {
					t.Fatal("native recovery changed OAuth or revived account")
				}
			} else if err == nil {
				t.Fatal("failed or stale rotation committed")
			}
		})
	}
}

func TestPayloadInvalidationKeepsIndependentNativeRecovery(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	acc, _ := db.UpsertAccount(ctx, "synthetic-payload", "buffer", nil, nil, nil, nil, nil, nil)
	st := syntheticNativeSession()
	if err := db.PutSessionForAccount(ctx, acc, st.Session.UIN, st.toBlob(), st.PSK.ExpiredAt, ""); err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)
	if err := pool.invalidateExpected(ctx, acc.ID, "", acc); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.cachedSession(ctx, acc.ID, ""); err == nil {
		t.Fatal("rejected PSK stayed eligible")
	}
	if !pool.hasNativeRecovery(ctx, acc.ID, "") {
		t.Fatal("payload error erased independent AutoKey")
	}
	if pool.hasNativeRecovery(ctx, acc.ID, "socks5://example.invalid:1080") {
		t.Fatal("native recovery crossed proxy routes")
	}
}
