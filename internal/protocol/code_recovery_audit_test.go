//go:build protocolaudit

package protocol

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/pcmgr"
	"yyb_go/internal/store"
)

// Explicit private snapshot only. No production DB, key renewal, restoration
// of expired accounts, or business operation is opened by this audit.
func TestPCMgrCodeRecoveryAudit(t *testing.T) {
	path := os.Getenv("YYB_PCMGR_REPAIR_AUDIT_SNAPSHOT")
	if path == "" {
		t.Skip("requires an explicit current private PCMgr snapshot")
	}
	if !filepath.IsAbs(path) {
		t.Fatal("private snapshot path must be absolute")
	}
	raw, err := os.ReadFile(path)
	var saved struct {
		Schema      int            `json:"schema"`
		Provider    string         `json:"provider"`
		Status      string         `json:"status"`
		Credentials map[string]any `json:"credentials"`
		Buffer      string         `json:"login_buffer"`
		Session     map[string]any `json:"session_blob"`
	}
	if err != nil || json.Unmarshal(raw, &saved) != nil || saved.Schema != 1 || saved.Provider != "pcmgr" || saved.Status != "alive" {
		t.Fatal("requires a current alive PCMgr snapshot")
	}
	creds := CredentialsFromMap(saved.Credentials)
	old, parseErr := sessionFromBlob(saved.Session)
	if creds.Provider != "pcmgr" || creds.PCMgr.Validate() != nil || parseErr != nil || string(old.Session.HostAppID) != pcmgr.HostAppID || old.TCPProxy != "" || old.PSK.ExpiredAt <= time.Now().Unix() {
		t.Fatal("snapshot credentials or cached session invalid")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 75*time.Second)
	defer cancel()
	db, err := store.Open(filepath.Join(t.TempDir(), "isolated-repair.db"))
	if err != nil {
		t.Fatal("isolated database unavailable")
	}
	defer db.Close()
	acc, err := db.UpsertAccount(ctx, "synthetic-private-snapshot", saved.Buffer, nil, nil, nil, nil, saved.Credentials, &saved.Status)
	if err != nil || db.PutSession(ctx, acc.ID, nil, saved.Session, old.PSK.ExpiredAt, "") != nil {
		t.Fatal("isolated snapshot initialization failed")
	}
	pool := NewPool(DefaultConfig(), db)
	const appID = "wx3bb18aff4f2fe45f"
	result, baseline := pool.GetCode(ctx, acc.LoginBuffer, appID, acc.ID, "", false, acc)
	auditOutput("repair_old_cache", map[string]any{"success": baseline == nil, "code_length": len(stringFromMap(result, "code")), "application_minus14": IsApplicationRequestRejection(baseline)})
	if !IsApplicationRequestRejection(baseline) {
		t.Fatal("required cached -14 baseline was not reproduced; no repair attempted")
	}
	fetches := 0
	result, err = pool.RecoverCodeSession(ctx, acc, appID, "", false, baseline, func(ctx context.Context) (string, error) {
		fetches++
		return NewLoginBufferClient(12*time.Second).FetchLoginBuffer(ctx, creds)
	})
	auditOutput("repair_fresh_session", map[string]any{"success": err == nil, "code_length": len(stringFromMap(result, "code")), "ticket_requests": fetches})
	if err != nil || len(stringFromMap(result, "code")) == 0 {
		t.Fatal("isolated repair failed; original production state unchanged")
	}
	result, err = pool.GetCode(ctx, acc.LoginBuffer, appID, acc.ID, "", false, acc)
	current, readErr := db.GetAccount(ctx, acc.ID)
	auditOutput("repair_reused_session", map[string]any{"success": err == nil, "code_length": len(stringFromMap(result, "code")), "authorization_unchanged": readErr == nil && current.SameAuthorization(acc), "production_written": false, "key_renewal_requested": false})
	if err != nil || readErr != nil || !current.SameAuthorization(acc) {
		t.Fatal("repaired cache did not remain usable")
	}
}
