// Package eventlog records a fixed event vocabulary, never raw errors, tokens,
// HTTP bodies, query strings, proxy URLs, account names or protocol payloads.
package eventlog

import (
	"context"
	"encoding/json"
	"log"
	"regexp"
	"sync"
	"sync/atomic"
	"time"

	"yyb_go/internal/store"
)

type definition struct{ category, level, message string }

var catalog = map[string]definition{
	"system.started":             {"system", "info", "服务已启动"},
	"system.stopped":             {"system", "info", "服务已停止"},
	"system.dropped":             {"system", "warn", "日志队列已满，部分记录被丢弃"},
	"http.completed":             {"api", "info", "接口请求完成"},
	"http.rejected":              {"api", "warn", "接口请求被拒绝"},
	"http.failed":                {"api", "error", "接口请求失败"},
	"management.completed":       {"management", "info", "管理操作完成"},
	"management.rejected":        {"management", "warn", "管理操作被拒绝"},
	"management.failed":          {"management", "error", "管理操作失败"},
	"auth.completed":             {"auth", "info", "面板登录状态操作完成"},
	"auth.rejected":              {"auth", "warn", "面板登录状态操作被拒绝"},
	"auth.failed":                {"auth", "error", "面板登录状态操作失败"},
	"protocol.succeeded":         {"protocol", "info", "协议调用完成"},
	"protocol.failed":            {"protocol", "error", "协议调用失败"},
	"protocol.recovery_started":  {"protocol", "info", "开始恢复取码会话"},
	"protocol.recovery_deferred": {"protocol", "debug", "取码恢复处于退避期"},
	"protocol.recovery_skipped":  {"protocol", "debug", "取码恢复已跳过：会话刚验证成功"},
	"protocol.circuit_open":      {"protocol", "warn", "已熔断，暂停向上游重复请求"},
	"protocol.circuit_deferred":  {"protocol", "debug", "请求处于熔断期，未发往上游"},
	"protocol.paced":             {"protocol", "info", "取码限速：部分请求已排队或被暂缓"},
	"protocol.appid_denied":      {"protocol", "warn", "取码已拒绝：网站应用 AppID，未发往上游"},
	"fleet.failover":             {"protocol", "info", "编队转移：该成员上游推回，请求交给下一成员"},
	"protocol.login_limited":     {"protocol", "warn", "重新登录次数已达上限，部分请求未再恢复会话"},
	"protocol.ticket_requested":  {"protocol", "info", "请求新的登录票据"},
	"protocol.recovered":         {"protocol", "info", "新会话取码验证通过"},
	"protocol.native_started":    {"protocol", "info", "开始独立会话密钥恢复"},
	"protocol.native_failed":     {"protocol", "error", "独立会话密钥恢复失败"},
	"protocol.native_rotated":    {"protocol", "info", "独立会话密钥已轮换保存"},
	"protocol.native_incomplete": {"protocol", "warn", "密钥已保存，传输状态尚未恢复"},
	"keepalive.failed":           {"keepalive", "error", "后台凭据续期失败"},
	"keepalive.renewed":          {"keepalive", "info", "后台凭据续期完成"},
	"account.expired":            {"account", "warn", "账号授权失效，需重新扫码"},
	"account.scanned":            {"account", "info", "扫码授权已保存"},
	"account.refreshed":          {"account", "info", "凭据已按需或手动刷新"},
	"account.refresh_failed":     {"account", "warn", "凭据按需或手动刷新失败"},
	"account.notified":           {"account", "info", "已推送掉线提醒"},
	"account.notify_failed":      {"account", "warn", "掉线提醒推送失败"},
}

type Rejection struct {
	Stage      string `json:"stage"`
	Code       int64  `json:"code"`
	WeChatCode int    `json:"wechat_code,omitempty"`
}

// Protocol request classes an event or the code breaker can be about.
const (
	OpGetCode        = "get_code"
	OpGetPhoneNumber = "get_phone_number"
	OpOperateWXData  = "operate_wx_data"
	OpH5Code         = "h5_code"
)

type Fields struct {
	AccountID  int64       `json:"-"`
	Channel    string      `json:"-"`
	Route      string      `json:"route,omitempty"`
	Method     string      `json:"method,omitempty"`
	Status     int         `json:"http_status,omitempty"`
	DurationMS int64       `json:"duration_ms,omitempty"`
	AppID      string      `json:"app_id,omitempty"`
	Operation  string      `json:"operation,omitempty"`
	API        string      `json:"api,omitempty"`
	Source     string      `json:"source,omitempty"`
	Rejections []Rejection `json:"rejections,omitempty"`
	Count      uint64      `json:"count,omitempty"`
	// Waited and Refused summarise a burst that pacing queued or turned away.
	Waited  uint64 `json:"waited,omitempty"`
	Refused uint64 `json:"refused,omitempty"`
	// Recent is what the account had just asked for; it is attached to the
	// events that report a death (account.expired, account.refresh_failed).
	Recent []RecentRequest `json:"recent,omitempty"`
}

// Refresh sources outside the background keepalive loop, which reports its own
// keepalive.* events.
const (
	SourceOnDemand = "on_demand"
	SourceManual   = "manual"
)

// WithSource records what asked for a credential refresh.
func (f Fields) WithSource(source string) Fields {
	f.Source = source
	return f
}

// WithTarget records which AppID and request class an event is about; the
// investigation of repeated -14 rejections needs to see which ones were refused.
func (f Fields) WithTarget(appID, operation, api string) Fields {
	f.AppID, f.Operation, f.API = appID, operation, api
	return f
}

// ChannelFor maps an account credential provider to its log channel. The
// empty provider is the default YYB (appstore) channel.
func ChannelFor(provider string) string {
	switch provider {
	case "pcmgr":
		return "pcmgr"
	case "gameassist":
		return "gameassist"
	case "qbbrowser":
		return "qbbrowser"
	case "ldplayer":
		return "ldplayer"
	default:
		return "yyb"
	}
}

func ForAccount(acc *store.WechatAccount) Fields {
	if acc == nil {
		return Fields{}
	}
	provider, _ := acc.Credentials["provider"].(string)
	return Fields{AccountID: acc.ID, Channel: ChannelFor(provider)}
}

// Routes are router templates, never the requested URL or account-link token.
var safeRoute = regexp.MustCompile(`^/[a-zA-Z0-9_/*:-]{0,100}$`)

// AppIDs and mini-program API names are public identifiers, but they arrive in
// request bodies, so only plain identifier text is ever recorded.
var (
	safeAppID   = regexp.MustCompile(`^[A-Za-z0-9_-]{1,40}$`)
	safeAPIName = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_]{0,39}$`)
)

func safeSource(source string) bool {
	return source == SourceOnDemand || source == SourceManual
}

func safeOperation(op string) bool {
	switch op {
	case OpGetCode, OpGetPhoneNumber, OpOperateWXData, OpH5Code:
		return true
	}
	return false
}

func safeStage(stage string) bool {
	switch stage {
	case "refresh_token", "check_login", "login_buffer", "manual_auth", "native_autoauth_transport", "native_autoauth_application", "native_autoauth_business", "shortlink_transport", "shortlink_application", "pcmgr_login_buffer", "pcmgr_key_renewal", "circuit_target", "circuit_session":
		return true
	}
	return false
}

type Logger struct {
	db      *store.DB
	mu      sync.RWMutex
	closed  bool
	queue   chan store.SystemLog
	done    chan struct{}
	dropped atomic.Uint64
	recent  *recentBook
}

func New(db *store.DB) (*Logger, error) {
	if err := db.InitSystemLogs(context.Background()); err != nil {
		return nil, err
	}
	l := &Logger{db: db, queue: make(chan store.SystemLog, 256), done: make(chan struct{}), recent: newRecentBook()}
	go l.run()
	return l, nil
}

func (l *Logger) Emit(event string, f Fields) {
	if l == nil {
		return
	}
	d, ok := catalog[event]
	if !ok {
		return
	}
	if f.AccountID < 0 {
		f.AccountID = 0
	}
	if f.Channel != "yyb" && f.Channel != "pcmgr" && f.Channel != "gameassist" {
		f.Channel = ""
	}
	if !safeRoute.MatchString(f.Route) {
		f.Route = ""
	}
	switch f.Method {
	case "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS":
	default:
		f.Method = ""
	}
	if !safeAppID.MatchString(f.AppID) {
		f.AppID = ""
	}
	if !safeOperation(f.Operation) {
		f.Operation = ""
	}
	if !safeAPIName.MatchString(f.API) {
		f.API = ""
	}
	if !safeSource(f.Source) {
		f.Source = ""
	}
	clean := make([]Rejection, 0, 8)
	for _, r := range f.Rejections {
		if safeStage(r.Stage) && len(clean) < 8 {
			clean = append(clean, r)
		}
	}
	f.Rejections = clean
	l.recent.observe(event, f)
	f.Recent = cleanRecent(f.Recent)
	raw, _ := json.Marshal(f)
	item := store.SystemLog{CreatedAt: time.Now().UnixMilli(), Level: d.level, Category: d.category, Event: event, Message: d.message, AccountID: f.AccountID, Channel: f.Channel, Details: raw}
	l.mu.RLock()
	defer l.mu.RUnlock()
	if l.closed {
		return
	}
	select {
	case l.queue <- item:
	default:
		l.dropped.Add(1)
	}
}

func (l *Logger) run() {
	defer close(l.done)
	for first := range l.queue {
		batch := []store.SystemLog{first}
		for len(batch) < 50 {
			select {
			case next, ok := <-l.queue:
				if !ok {
					goto flush
				}
				batch = append(batch, next)
			default:
				goto flush
			}
		}
	flush:
		if n := l.dropped.Swap(0); n > 0 {
			raw, _ := json.Marshal(Fields{Count: n})
			d := catalog["system.dropped"]
			batch = append(batch, store.SystemLog{CreatedAt: time.Now().UnixMilli(), Level: d.level, Category: d.category, Event: "system.dropped", Message: d.message, Details: raw})
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		err := l.db.AppendSystemLogs(ctx, batch)
		cancel()
		if err != nil {
			log.Print("system-log: storage write failed; event details withheld")
		}
	}
}

func (l *Logger) Close() {
	if l == nil {
		return
	}
	l.mu.Lock()
	if !l.closed {
		l.closed = true
		close(l.queue)
	}
	l.mu.Unlock()
	<-l.done
}
