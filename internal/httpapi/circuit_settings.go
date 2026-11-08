package httpapi

import (
	"context"
	"net/http"
	"strconv"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

// Global is a master gate: account switches cannot override a disabled master.
// The env flag supplies the default only until a global choice is saved.
func (a *App) handleCircuitSettings(w http.ResponseWriter, r *http.Request) {
	var accountID int64
	global := r.URL.Path == "/api/circuit-settings"
	if global {
		if a.auth != nil && !requireAdmin(w, r) {
			return
		}
	} else {
		acc, ok := a.resolveAccountFromQuery(w, r)
		if !ok {
			return
		}
		accountID = acc.ID
	}
	switch r.Method {
	case http.MethodGet:
	case http.MethodPut:
		var body struct {
			Enabled *bool `json:"enabled"`
		}
		if err := decodeOptionalJSON(r, &body); err != nil || body.Enabled == nil {
			writeError(w, http.StatusBadRequest, "enabled 必须是布尔值")
			return
		}
		var err error
		if global {
			err = a.db.SetSetting(r.Context(), store.CircuitEnabledKey, strconv.FormatBool(*body.Enabled))
		} else {
			err = a.db.SetAccountCircuitEnabled(r.Context(), accountID, *body.Enabled)
		}
		if err != nil {
			writeError(w, http.StatusInternalServerError, "保存熔断设置失败")
			return
		}
		// Reset cooldowns on any policy change. Re-enabling starts clean; the
		// disabled interval must not accumulate failures for a later lockout.
		if global {
			a.circuit.reset()
		} else {
			a.circuit.forget(accountID)
		}
		a.events.Emit("management.completed", eventlog.Fields{Route: r.URL.Path, Method: r.Method})
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	settings, err := a.db.CircuitSettings(r.Context(), accountID, !a.cfg.DisableCircuitBreaker)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "读取熔断设置失败")
		return
	}
	writeJSON(w, http.StatusOK, settings)
}

func (a *App) accountCircuitEnabled(ctx context.Context, accountID int64) (bool, error) {
	settings, err := a.db.CircuitSettings(ctx, accountID, !a.cfg.DisableCircuitBreaker)
	if err != nil {
		return false, err
	}
	if !settings.Effective {
		a.circuit.forget(accountID)
	}
	return settings.Effective, nil
}
