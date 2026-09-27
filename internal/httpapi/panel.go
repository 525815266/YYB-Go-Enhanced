package httpapi

import (
	"context"
	"strings"
	"time"
)

const (
	PanelTypeQingLong = "qinglong"
	PanelTypeDaidai   = "daidai"
	PanelTypeArcadia  = "arcadia"
)

type qingLongEnv struct {
	ID      int64  `json:"id"`
	Name    string `json:"name"`
	Value   string `json:"value"`
	Remarks string `json:"remarks"`
	Status  int    `json:"status"`
	Enabled *bool  `json:"enabled"`
}

func (e qingLongEnv) enabled() bool {
	if e.Enabled != nil {
		return *e.Enabled
	}
	return e.Status == 0
}

type qingLongCron struct {
	ID                 int64   `json:"id"`
	Name               string  `json:"name"`
	Command            string  `json:"command"`
	Schedule           string  `json:"schedule"`
	CronExpression     string  `json:"cron_expression"`
	TaskBefore         string  `json:"task_before"`
	LogName            string  `json:"log_name"`
	LogPath            string  `json:"log_path"`
	Status             any     `json:"status"`
	LastExecutionTime  any     `json:"last_execution_time"`
	LastRunAt          string  `json:"last_run_at"`
	LastRunningTime    any     `json:"last_running_time"`
	IsDisabled         *int    `json:"isDisabled"`
	Enabled            *bool   `json:"enabled"`
	IsRunning          *int    `json:"is_running"`
	IsRunningAlt       *int    `json:"isRunning"`
	PID                any     `json:"pid"`
	ExecutionStatusAlt float64 `json:"execution_status"`
}

func (c qingLongCron) getSchedule() string {
	if c.Schedule != "" {
		return c.Schedule
	}
	return c.CronExpression
}

func (c qingLongCron) enabled() bool {
	// QingLong exposes isDisabled and uses status for the runtime state.
	// Daidai exposes enabled (or status=1 for enabled) without isDisabled.
	if c.IsDisabled != nil {
		return *c.IsDisabled == 0
	}
	if c.Enabled != nil {
		return *c.Enabled
	}
	switch v := c.Status.(type) {
	case float64:
		return v != 0
	case int:
		return v != 0
	case bool:
		return v
	}
	return true
}

func (c qingLongCron) running() bool {
	if status, ok := c.Status.(string); ok {
		return status == "running"
	}
	if c.IsDisabled != nil {
		switch v := c.Status.(type) {
		case float64:
			return v >= 0 && v < 1
		case int:
			return v == 0
		case int64:
			return v == 0
		}
		return false
	}
	if (c.IsRunning != nil && *c.IsRunning != 0) || (c.IsRunningAlt != nil && *c.IsRunningAlt != 0) || c.ExecutionStatusAlt == 2 {
		return true
	}
	if v, ok := c.Status.(float64); ok && v == 2 {
		return true
	}
	if c.PID != nil {
		switch v := c.PID.(type) {
		case int:
			return v != 0
		case int64:
			return v != 0
		case float64:
			return v != 0
		}
	}
	return false
}

func (c qingLongCron) getLastExecutionAt() int64 {
	if parsed, err := time.Parse(time.RFC3339Nano, c.LastRunAt); err == nil {
		return parsed.Unix()
	}
	switch v := c.LastExecutionTime.(type) {
	case int64:
		return v
	case float64:
		return int64(v)
	case int:
		return int64(v)
	case string:
		if parsed, err := time.Parse(time.RFC3339Nano, v); err == nil {
			return parsed.Unix()
		}
	}
	return 0
}

func (c qingLongCron) getLastRunningTime() int64 {
	switch v := c.LastRunningTime.(type) {
	case int64:
		return v
	case float64:
		return int64(v)
	case int:
		return int64(v)
	}
	return 0
}

type qingLongLogEntry struct {
	Title      string             `json:"title"`
	Key        string             `json:"key"`
	Type       string             `json:"type"`
	Parent     string             `json:"parent"`
	Size       int64              `json:"size"`
	CreateTime int64              `json:"createTime"`
	Children   []qingLongLogEntry `json:"children"`
}

// PanelDriver is the decoupled interface implemented by specific panel drivers (Qinglong, Daidai, etc.)
type PanelDriver interface {
	PanelType() string
	Status(ctx context.Context) error
	ListEnvs(ctx context.Context, searchValue string) ([]qingLongEnv, error)
	UpsertEnv(ctx context.Context, name, value, remarks string) error
	UpdateEnv(ctx context.Context, id int64, name, value, remarks string) error
	UpdateEnvEntry(ctx context.Context, env qingLongEnv, newValue string) error
	SetEnvsEnabled(ctx context.Context, ids []int64, enabled bool) error
	SetNamedEnvsEnabled(ctx context.Context, names []string, enabled bool) error
	ListCrons(ctx context.Context, search string) ([]qingLongCron, error)
	CreateCron(ctx context.Context, name, command, schedule, taskBefore, logName string) (*qingLongCron, error)
	UpdateCron(ctx context.Context, id int64, name, command, schedule, taskBefore, logName string) error
	SetCronsEnabled(ctx context.Context, ids []int64, enabled bool) error
	RunCrons(ctx context.Context, ids []int64) error
	DeleteCrons(ctx context.Context, ids []int64) error
	CronLog(ctx context.Context, id int64) (string, error)
	ListLogs(ctx context.Context) ([]qingLongLogEntry, error)
	LogDetail(ctx context.Context, dir, filename string) (string, error)
}

func normalizePanelType(pType string) string {
	switch strings.ToLower(strings.TrimSpace(pType)) {
	case PanelTypeDaidai:
		return PanelTypeDaidai
	case PanelTypeArcadia:
		return PanelTypeArcadia
	default:
		return PanelTypeQingLong
	}
}
