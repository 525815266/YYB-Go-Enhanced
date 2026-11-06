//go:build protocolaudit

package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"yyb_go/internal/protocol"
)

func TestNativeLabRetainsSafeRejectionDetails(t *testing.T) {
	err := fmt.Errorf("synthetic-private-material: %w", &protocol.AutoAuthRejectedError{Stage: "business", Code: -13})
	raw, _ := json.Marshal(failureFields(err))
	if strings.Contains(string(raw), "synthetic-private-material") || !strings.Contains(string(raw), `"native_autoauth_code":-13`) || !strings.Contains(string(raw), `"native_autoauth_stage":"business"`) {
		t.Fatal("safe diagnostics lost rejection or disclosed raw error")
	}
	raw, _ = json.Marshal(failureFields(errors.New("PCMgr transport: synthetic-private-material")))
	if strings.Contains(string(raw), "synthetic-private-material") {
		t.Fatal("provider error prefix bypassed safe diagnostics")
	}
}

func TestNativeLabIntentAndFirstOutcomeCannotBeOverwritten(t *testing.T) {
	dir := t.TempDir()
	if err := recordNativeIntent(dir); err != nil {
		t.Fatal(err)
	}
	if recordNativeIntent(dir) == nil {
		t.Fatal("duplicate rotating-key intent accepted")
	}
	first := failureFields(errors.New("synthetic-private-error"))
	if err := recordNativeOutcome(dir, first); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "native-autoauth-result.json")
	before, _ := os.ReadFile(path)
	if recordNativeOutcome(dir, map[string]any{"success": true}) == nil {
		t.Fatal("first outcome overwritten")
	}
	after, _ := os.ReadFile(path)
	if string(before) != string(after) || strings.Contains(string(after), "synthetic-private-error") {
		t.Fatal("first outcome changed or raw error leaked")
	}
}
