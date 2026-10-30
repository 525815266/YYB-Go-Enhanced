//go:build protocolaudit

package protocol

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Explicit private snapshot only. Compare the cached request with the saved
// ticket ManualAuth path used by older releases. No OAuth, AutoAuth, new ticket
// API or production DB access. The snapshot marker prevents accidental repeats.
func TestSavedTicketRebuildAudit(t *testing.T) {
	input, output := os.Getenv("YYB_SAVED_TICKET_SNAPSHOT"), os.Getenv("YYB_SAVED_TICKET_OUTPUT")
	if input == "" || output == "" {
		t.Skip("explicit private snapshot and unused output required")
	}
	if !filepath.IsAbs(input) || !filepath.IsAbs(output) {
		t.Fatal("absolute private paths required")
	}
	raw, err := os.ReadFile(input)
	var saved struct {
		Schema   int            `json:"schema"`
		Provider string         `json:"provider"`
		Status   string         `json:"status"`
		Buffer   string         `json:"login_buffer"`
		Session  map[string]any `json:"session_blob"`
	}
	if err != nil || json.Unmarshal(raw, &saved) != nil || saved.Schema != 1 || saved.Status != "alive" || saved.Buffer == "" || (saved.Provider != "pcmgr" && saved.Provider != "appstore") {
		t.Fatal("current private profile required")
	}
	st, err := sessionFromBlob(saved.Session)
	if err != nil || st.TCPProxy != "" || st.PSK.ExpiredAt <= time.Now().Unix() {
		t.Fatal("valid direct cached snapshot required")
	}
	if err := os.Mkdir(output, 0700); err != nil {
		t.Fatal("unused output directory required")
	}
	marker, err := os.OpenFile(input+".saved-ticket-used", os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		t.Fatal("snapshot already attempted; refusing replay")
	}
	_ = marker.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	pool := NewPool(DefaultConfig(), nil)
	const appID = "wx3bb18aff4f2fe45f"
	result, cachedErr := pool.requestLoginCode(ctx, st, appID)
	report := map[string]any{
		"provider": saved.Provider, "cached": savedTicketAuditResult(result, cachedErr),
		"production_db_written": false, "oauth_requested": false, "autoauth_requested": false, "new_ticket_requested": false,
	}
	if IsApplicationRequestRejection(cachedErr) {
		fresh, loginErr := pool.loginAndSession(ctx, saved.Buffer, "", false)
		if loginErr != nil {
			report["saved_ticket_manualauth"] = savedTicketAuditResult(nil, loginErr)
		} else {
			result, codeErr := pool.requestLoginCode(ctx, fresh, appID)
			report["saved_ticket_manualauth"] = savedTicketAuditResult(result, codeErr)
			if codeErr == nil && result["code"] != "" {
				encoded, err := json.Marshal(fresh.toBlob())
				if err != nil || os.WriteFile(filepath.Join(output, "candidate-session.json"), encoded, 0600) != nil {
					t.Fatal("candidate session could not be saved privately")
				}
				result, reuseErr := pool.requestLoginCode(ctx, fresh, appID)
				report["candidate_reuse"] = savedTicketAuditResult(result, reuseErr)
			}
		}
	} else {
		report["saved_ticket_skipped_without_minus14"] = true
	}
	encoded, err := json.Marshal(report)
	if err != nil || os.WriteFile(filepath.Join(output, "safe-result.json"), encoded, 0600) != nil {
		t.Fatal("safe report could not be saved")
	}
	t.Log(string(encoded))
}

func savedTicketAuditResult(result map[string]any, err error) map[string]any {
	code, _ := result["code"].(string)
	out := map[string]any{"success": err == nil && code != "", "code_length": len(code)}
	var manual *ManualAuthRejectedError
	if errors.As(err, &manual) {
		out["manual_auth_code"] = manual.Code
	}
	var short *ShortlinkRejectedError
	if errors.As(err, &short) {
		out["shortlink_code"] = short.Code
		out["transport_rejection"] = short.Transport
	}
	if err != nil {
		out["timed_out"] = errors.Is(err, context.DeadlineExceeded)
	}
	return out
}
