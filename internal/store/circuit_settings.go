package store

import (
	"context"
	"database/sql"
	"fmt"
	"strconv"
)

const CircuitEnabledKey = "circuit_enabled"

type CircuitSettings struct {
	GlobalEnabled  bool `json:"global_enabled"`
	AccountEnabled bool `json:"account_enabled"`
	Effective      bool `json:"effective"`
}

// Read both gates in one SQLite snapshot. Account policies follow the account
// through ID compaction and are deleted with it, rather than surviving in an
// ID-shaped app_settings key. Zero reads only the global gate.
func (db *DB) CircuitSettings(ctx context.Context, accountID int64, defaultEnabled bool) (CircuitSettings, error) {
	var global string
	var account bool
	query := `SELECT COALESCE((SELECT value FROM app_settings WHERE key=?),?), 1`
	args := []any{CircuitEnabledKey, strconv.FormatBool(defaultEnabled)}
	if accountID > 0 {
		query = `SELECT COALESCE((SELECT value FROM app_settings WHERE key=?),?), COALESCE(c.enabled,1)
FROM wechat_accounts a LEFT JOIN account_circuit_settings c ON c.account_id=a.id WHERE a.id=?`
		args = append(args, accountID)
	}
	if err := db.sql.QueryRowContext(ctx, query, args...).Scan(&global, &account); err != nil {
		return CircuitSettings{}, err
	}
	enabled, err := strconv.ParseBool(global)
	if err != nil {
		return CircuitSettings{}, fmt.Errorf("invalid circuit setting")
	}
	return CircuitSettings{GlobalEnabled: enabled, AccountEnabled: account, Effective: enabled && account}, nil
}

func (db *DB) SetAccountCircuitEnabled(ctx context.Context, accountID int64, enabled bool) error {
	result, err := db.sql.ExecContext(ctx, `INSERT INTO account_circuit_settings(account_id,enabled)
SELECT id,? FROM wechat_accounts WHERE id=? ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled`, enabled, accountID)
	if err != nil {
		return err
	}
	if n, err := result.RowsAffected(); err != nil {
		return err
	} else if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}
