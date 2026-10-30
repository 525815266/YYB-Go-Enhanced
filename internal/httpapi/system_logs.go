package httpapi

import (
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

func eventRejections(err error) []eventlog.Rejection {
	out := make([]eventlog.Rejection, 0)
	for _, item := range credentialDiagnostics(err) {
		out = append(out, eventlog.Rejection{Stage: item.Stage, Code: item.UpstreamCode, WeChatCode: item.WeChatCode})
	}
	return out
}

func (a *App) logRequests() gin.HandlerFunc {
	return func(c *gin.Context) {
		started := time.Now()
		c.Next()
		route := c.FullPath()
		if route == "" {
			return
		}
		protocol := strings.HasPrefix(route, "/wx/") || strings.HasPrefix(route, "/wxapp/")
		auth := route == "/login" || route == "/logout" || route == "/register"
		management := strings.HasPrefix(route, "/accounts") || strings.HasPrefix(route, "/api/") || strings.HasPrefix(route, "/qr") || strings.HasPrefix(route, "/account-link/") || strings.HasPrefix(route, "/quick-login")
		if !protocol && (c.Request.Method == http.MethodGet || c.Request.Method == http.MethodHead || !auth && !management) {
			return
		}
		category := "management"
		if protocol {
			category = "http"
		} else if auth {
			category = "auth"
		}
		outcome := "completed"
		status := c.Writer.Status()
		if status >= 500 {
			outcome = "failed"
		} else if status >= 400 {
			outcome = "rejected"
		}
		a.events.Emit(category+"."+outcome, eventlog.Fields{Route: route, Method: c.Request.Method, Status: status, DurationMS: time.Since(started).Milliseconds()})
	}
}

func (a *App) handleSystemLogsPage(w http.ResponseWriter, r *http.Request) {
	if !requireAdmin(w, r) {
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	serveFileOrText(w, r, filepath.Join(a.resources.Templates, "logs.html"), "System logs page missing")
}

func (a *App) handleSystemLogs(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if !requireAdmin(w, r) {
		return
	}
	q := r.URL.Query()
	f := store.SystemLogFilter{Category: q.Get("category"), Level: q.Get("level"), Channel: q.Get("channel"), Limit: 50}
	allowed := func(value string, values ...string) bool {
		if value == "" {
			return true
		}
		for _, v := range values {
			if value == v {
				return true
			}
		}
		return false
	}
	if !allowed(f.Category, "system", "api", "protocol", "keepalive", "account", "management", "auth") || !allowed(f.Level, "debug", "info", "warn", "error") || !allowed(f.Channel, "yyb", "pcmgr") {
		writeError(w, 400, "日志筛选条件无效")
		return
	}
	for key, target := range map[string]*int64{"account_id": &f.AccountID, "before": &f.Before, "since": &f.Since} {
		if value := q.Get(key); value != "" {
			n, err := strconv.ParseInt(value, 10, 64)
			if err != nil || n < 0 {
				writeError(w, 400, "日志筛选参数无效")
				return
			}
			*target = n
		}
	}
	if value := q.Get("limit"); value != "" {
		n, err := strconv.Atoi(value)
		if err != nil || n < 1 || n > 100 {
			writeError(w, 400, "每页条数应为1至100")
			return
		}
		f.Limit = n
	}
	limit := f.Limit
	f.Limit++
	entries, err := a.db.ListSystemLogs(r.Context(), f)
	if err != nil {
		writeError(w, 500, "读取运行日志失败")
		return
	}
	more := len(entries) > limit
	if more {
		entries = entries[:limit]
	}
	var cursor int64
	if more {
		cursor = entries[len(entries)-1].ID
	}
	writeJSON(w, 200, map[string]any{"entries": entries, "has_more": more, "next_before": cursor, "retention_days": 7, "max_entries": 10000})
}
