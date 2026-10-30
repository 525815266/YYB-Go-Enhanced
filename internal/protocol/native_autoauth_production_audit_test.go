//go:build protocolaudit

package protocol

import (
	"bytes"
	"context"
	"crypto/ecdh"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/pcmgr"
	"yyb_go/internal/store"
)

// Explicit private input only. Uses production persistence, request construction,
// verification and transport; never accesses or changes the production database.
func TestNativeAutoAuthProductionAudit(t *testing.T) {
	source, output := os.Getenv("YYB_NATIVE_PRODUCTION_SOURCE"), os.Getenv("YYB_NATIVE_PRODUCTION_OUTPUT")
	snapshot := os.Getenv("YYB_NATIVE_PRODUCTION_SNAPSHOT")
	if source == "" && snapshot == "" || output == "" {
		t.Skip("explicit private source and new output directory required")
	}
	if source != "" && !filepath.IsAbs(source) || snapshot != "" && !filepath.IsAbs(snapshot) || !filepath.IsAbs(output) || source != "" && snapshot != "" {
		t.Fatal("absolute private paths required")
	}
	if err := os.Mkdir(output, 0700); err != nil {
		t.Fatal("unused private output directory required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	db, err := store.Open(filepath.Join(output, "yyb.db"))
	if err != nil {
		t.Fatal("private DB open failed")
	}
	defer db.Close()
	pool := NewPool(DefaultConfig(), db)
	var st WmpfSession
	if snapshot != "" {
		raw, err := os.ReadFile(snapshot)
		var profile struct {
			Credentials map[string]any `json:"credentials"`
			Provider    string         `json:"provider"`
			Status      string         `json:"status"`
		}
		if err != nil || json.Unmarshal(raw, &profile) != nil || profile.Status != "alive" || profile.Provider != "pcmgr" {
			t.Fatal("current private profile required")
		}
		buffer, err := NewLoginBufferClient(12*time.Second).FetchLoginBuffer(ctx, CredentialsFromMap(profile.Credentials))
		if err != nil {
			var rejection *pcmgr.BufferRejectedError
			if errors.As(err, &rejection) {
				auditOutput("production_ticket_failure", map[string]any{"command": 20138, "code": rejection.Code})
			}
			t.Fatal("fresh ticket unavailable; OAuth unchanged")
		}
		st, err = pool.loginAndSession(ctx, buffer, "", false)
		if err != nil {
			t.Fatal("production ManualAuth baseline failed")
		}
		result, err := pool.requestLoginCode(ctx, st, "wx3bb18aff4f2fe45f")
		code, _ := result["code"].(string)
		auditOutput("production_manual_baseline", map[string]any{"success": err == nil && code != "", "code_length": len(code), "native_keys_saved": st.AutoAuth.usable()})
		if err != nil || code == "" || !st.AutoAuth.usable() {
			t.Fatal("production ManualAuth baseline failed")
		}
	} else {
		raw, err := os.ReadFile(source)
		var captured nativeAuditState
		if err != nil || json.Unmarshal(raw, &captured) != nil {
			t.Fatal("source unavailable or malformed")
		}
		st = WmpfSession{Session: captured.Session, PSK: captured.PSK, AutoAuth: autoAuthState{Device: captured.AppDevice, Key: captured.AutoKey, EncKey: captured.EncKey}}
		if !st.AutoAuth.usable() {
			t.Fatal("source keys incomplete")
		}
		marker, err := os.OpenFile(source+".used", os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			t.Fatal("source already used; refusing replay")
		}
		_ = marker.Close()
	}
	status := "alive"
	acc, err := db.UpsertAccount(ctx, "synthetic-native-audit", "synthetic-buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal("private account setup failed")
	}
	if err := db.PutSessionForAccount(ctx, acc, st.Session.UIN, st.toBlob(), st.PSK.ExpiredAt, ""); err != nil {
		t.Fatal("private cache setup failed")
	}
	if _, err := db.ApplyCredentialRefresh(ctx, acc, "", nil, store.StatusSessionOnly); err != nil {
		t.Fatal("limited mode setup failed")
	}
	acc, _ = db.GetAccount(ctx, acc.ID)
	saved, err := pool.nativeRecoveryState(ctx, acc.ID, "")
	if err != nil {
		t.Fatal("source state unavailable")
	}
	next, err := pool.restoreNativeSession(ctx, acc, saved)
	if err != nil {
		auditOutput("production_native_failure", map[string]any{"error_type": formatAuditError(err)})
		t.Fatal("production native recovery failed; private DB preserves consumed-key state")
	}
	// The new rotating state is already committed before the first code probe.
	newState := nativeAuditState{AppDevice: next.AutoAuth.Device, Device: next.Session.DeviceID, AutoKey: next.AutoAuth.Key, EncKey: next.AutoAuth.EncKey, Session: next.Session, PSK: next.PSK}
	encoded, _ := json.Marshal(newState)
	if err := os.WriteFile(filepath.Join(output, "next.json"), encoded, 0600); err != nil {
		t.Fatal("private next-state save failed")
	}
	result, err := pool.requestLoginCode(ctx, next, "wx3bb18aff4f2fe45f")
	code, _ := result["code"].(string)
	current, _ := db.GetAccount(ctx, acc.ID)
	reopened := NewPool(DefaultConfig(), db)
	recovered, stateErr := reopened.nativeRecoveryState(ctx, acc.ID, "")
	auditOutput("production_native_recovery", map[string]any{
		"success": err == nil && len(code) > 0, "code_length": len(code),
		"auto_key_rotated":           !bytes.Equal(saved.AutoAuth.Key, next.AutoAuth.Key),
		"enc_key_rotated":            !bytes.Equal(saved.AutoAuth.EncKey, next.AutoAuth.EncKey),
		"persisted_before_probe":     next.nativePersisted,
		"rotation_survives_new_pool": stateErr == nil && bytes.Equal(recovered.AutoAuth.Key, next.AutoAuth.Key),
		"limited_status_preserved":   current.SessionOnly(), "oauth_unchanged": current.SameAuthorization(acc),
		"production_database_written": false})
	if err != nil || len(code) == 0 || !current.SessionOnly() || !current.SameAuthorization(acc) || stateErr != nil {
		t.Fatal("production native acceptance failed")
	}
}

func TestNativeAutoAuthProductionOfflineAudit(t *testing.T) {
	path := os.Getenv("YYB_NATIVE_PRODUCTION_OFFLINE")
	if path == "" {
		t.Skip("explicit private response required")
	}
	if !filepath.IsAbs(path) {
		t.Fatal("absolute private response required")
	}
	raw, err := os.ReadFile(path)
	var saved nativeAuditState
	if err != nil || json.Unmarshal(raw, &saved) != nil {
		t.Fatal("private response unavailable")
	}
	key, err := ecdh.P256().NewPrivateKey(saved.EphemeralPrivate)
	if err != nil {
		t.Fatal("private ephemeral state unavailable")
	}
	temp := &manualAuthTemp{Ephemeral: key, OKM: saved.OKM, CompBody: saved.Compressed, RequestCipher: saved.RequestCipher}
	old := WmpfSession{Session: saved.Session, AutoAuth: autoAuthState{Device: saved.AppDevice, Key: saved.AutoKey, EncKey: saved.EncKey}}
	_, err = parseAutoAuthResponse(shortlinkPacket{Cmd: saved.WireCommand, Body: saved.WireResponse}, old, temp)
	if err != nil {
		t.Fatal("production parser rejected authenticated native success")
	}
	auditOutput("production_offline_response", map[string]any{"signature_verified": true, "authenticated_response": true, "business_success": true, "network_used": false})
}

func formatAuditError(err error) string {
	// No arbitrary upstream error text or sensitive data in the test output.
	if e, ok := err.(*AutoAuthRejectedError); ok {
		return e.Error()
	}
	return "native recovery or persistence failure"
}
