package httpapi

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestRuntimeSettingsRoundTrip(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	// Local mode (no auth store) is administered on the machine; the handler
	// treats it as admin.
	app.auth = nil

	get := httptest.NewRecorder()
	app.handleRuntimeSettings(get, httptest.NewRequest(http.MethodGet, "/api/runtime-settings", nil))
	if get.Code != http.StatusOK {
		t.Fatalf("GET HTTP %d: %s", get.Code, get.Body.String())
	}
	var state struct {
		Data struct {
			Effective   map[string]any `json:"effective"`
			DynamicDeny struct {
				Enabled bool `json:"enabled"`
			} `json:"dynamic_deny"`
		} `json:"data"`
	}
	if err := json.Unmarshal(get.Body.Bytes(), &state); err != nil {
		t.Fatalf("decode: %v body=%s", err, get.Body.String())
	}
	if state.Data.Effective == nil {
		t.Fatalf("GET failed: %s", get.Body.String())
	}
	if state.Data.Effective["login_budget"].(float64) != float64(app.cfg.LoginBudget) {
		t.Fatalf("unexpected default budget: %s", get.Body.String())
	}

	// Pacing knobs land in tuning.json and the pacer applies them within its
	// check window; the login budget rides into the pool via onChange.
	put := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPut, "/api/runtime-settings",
		strings.NewReader(`{"code_min_gap_ms":5000,"login_budget":7,"dynamic_deny_enabled":false,"dynamic_deny_threshold":4}`))
	app.handleRuntimeSettings(put, request)
	if put.Code != http.StatusOK {
		t.Fatalf("PUT HTTP %d: %s", put.Code, put.Body.String())
	}
	time.Sleep(1100 * time.Millisecond) // one pacingFileCheck window
	eff := app.pacer.current()
	if eff.MinGap != 5*time.Second {
		t.Fatalf("min gap not applied: %s", eff.MinGap)
	}
	if eff.LoginBudget != 7 {
		t.Fatalf("login budget not applied: %d", eff.LoginBudget)
	}
	if app.dynamicAppIDs.enabledNow() {
		t.Fatal("dynamic deny should be disabled")
	}
	if app.dynamicAppIDs.currentThreshold() != 4 {
		t.Fatalf("threshold not applied: %d", app.dynamicAppIDs.currentThreshold())
	}

	// A disabled deny list stops learning: strikes are ignored.
	if denied := app.dynamicAppIDs.recordStrike("wx0000000000000001", 9); denied {
		t.Fatal("disabled deny list must not learn")
	}

	// Reset removes the tuning file: pacing returns to env defaults.
	reset := httptest.NewRecorder()
	app.handleRuntimeSettings(reset, httptest.NewRequest(http.MethodPut, "/api/runtime-settings",
		strings.NewReader(`{"reset":true}`)))
	if reset.Code != http.StatusOK {
		t.Fatalf("reset HTTP %d: %s", reset.Code, reset.Body.String())
	}
	if app.tuningFilePresent() {
		t.Fatal("tuning file should be gone after reset")
	}

	// Validation: out-of-range values are refused before anything changes.
	bad := httptest.NewRecorder()
	app.handleRuntimeSettings(bad, httptest.NewRequest(http.MethodPut, "/api/runtime-settings",
		strings.NewReader(`{"login_budget":99999}`)))
	if bad.Code != http.StatusBadRequest {
		t.Fatalf("out-of-range budget HTTP %d: %s", bad.Code, bad.Body.String())
	}
}
