package store

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// System logs intentionally have no account foreign key: a deleted account's
// diagnostic timeline must survive deletion. They never store account identity.
const systemLogSchema = `CREATE TABLE IF NOT EXISTS system_logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 created_at INTEGER NOT NULL,
 level TEXT NOT NULL,
 category TEXT NOT NULL,
 event TEXT NOT NULL,
 message TEXT NOT NULL,
 account_id INTEGER NOT NULL DEFAULT 0,
 channel TEXT NOT NULL DEFAULT '',
 details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_system_logs_category ON system_logs(category,id);
CREATE INDEX IF NOT EXISTS idx_system_logs_level ON system_logs(level,id);
CREATE INDEX IF NOT EXISTS idx_system_logs_account ON system_logs(account_id,id);
CREATE INDEX IF NOT EXISTS idx_system_logs_created ON system_logs(created_at);`

type SystemLog struct {
	ID        int64           `json:"id"`
	CreatedAt int64           `json:"created_at"`
	Level     string          `json:"level"`
	Category  string          `json:"category"`
	Event     string          `json:"event"`
	Message   string          `json:"message"`
	AccountID int64           `json:"account_id,omitempty"`
	Channel   string          `json:"channel,omitempty"`
	Details   json.RawMessage `json:"details"`
}

type SystemLogFilter struct {
	Category, Level, Channel string
	AccountID, Before, Since int64
	Limit                    int
}

func (db *DB) InitSystemLogs(ctx context.Context) error {
	_, err := db.sql.ExecContext(ctx, systemLogSchema)
	return err
}

func (db *DB) AppendSystemLogs(ctx context.Context, entries []SystemLog) error {
	tx, err := db.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, item := range entries {
		if _, err = tx.ExecContext(ctx, `INSERT INTO system_logs(created_at,level,category,event,message,account_id,channel,details) VALUES(?,?,?,?,?,?,?,?)`,
			item.CreatedAt, item.Level, item.Category, item.Event, item.Message, item.AccountID, item.Channel, string(item.Details)); err != nil {
			return err
		}
	}
	// Both bounds apply across restarts. This table never grows with account IDs.
	if _, err = tx.ExecContext(ctx, `DELETE FROM system_logs WHERE created_at < ? OR id <= COALESCE((SELECT id FROM system_logs ORDER BY id DESC LIMIT 1 OFFSET 10000),0)`, time.Now().Add(-7*24*time.Hour).UnixMilli()); err != nil {
		return err
	}
	return tx.Commit()
}

func (db *DB) ListSystemLogs(ctx context.Context, f SystemLogFilter) ([]SystemLog, error) {
	where, args := []string{"created_at>=?"}, []any{time.Now().Add(-7 * 24 * time.Hour).UnixMilli()}
	for _, pair := range [][2]string{{"category", f.Category}, {"level", f.Level}, {"channel", f.Channel}} {
		if pair[1] != "" {
			where = append(where, pair[0]+"=?")
			args = append(args, pair[1])
		}
	}
	for _, part := range []struct {
		sql   string
		value int64
	}{{"account_id=?", f.AccountID}, {"id<?", f.Before}, {"created_at>=?", f.Since}} {
		if part.value > 0 {
			where = append(where, part.sql)
			args = append(args, part.value)
		}
	}
	if f.Limit < 1 || f.Limit > 200 {
		return nil, fmt.Errorf("invalid log limit")
	}
	args = append(args, f.Limit)
	rows, err := db.sql.QueryContext(ctx, `SELECT id,created_at,level,category,event,message,account_id,channel,details FROM system_logs WHERE `+strings.Join(where, " AND ")+` ORDER BY id DESC LIMIT ?`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]SystemLog, 0)
	for rows.Next() {
		var item SystemLog
		var details string
		if err := rows.Scan(&item.ID, &item.CreatedAt, &item.Level, &item.Category, &item.Event, &item.Message, &item.AccountID, &item.Channel, &details); err != nil {
			return nil, err
		}
		item.Details = json.RawMessage(details)
		out = append(out, item)
	}
	return out, rows.Err()
}
