package httpapi

import (
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

const (
	defaultCodeBurstLimit      = 10
	defaultCodeBurstWindow     = 10 * time.Minute
	codeBurstLimitSettingKey   = "code_burst_limit"
	codeBurstWindowMSSettingKey = "code_burst_window_ms"
	codeBurstMinWindow         = 10 * time.Second
	codeBurstMaxWindow         = time.Hour
	codeBurstMaxLimit          = 1000
	codeBurstMaxTrackedKeys    = 4096
)

// codeBurstLimiter caps how many wx.login code requests a single (account, AppID)
// pair may trigger within a rolling window, blocking runaway script retry loops
// before they hammer WeChat upstream.
type codeBurstLimiter struct {
	mu     sync.Mutex
	now    func() time.Time
	hits   map[string][]time.Time
	logged map[string]time.Time
	limit  int
	window time.Duration
}

func newCodeBurstLimiter(limit int, window time.Duration) *codeBurstLimiter {
	if limit < 0 {
		limit = 0
	}
	if window <= 0 {
		window = defaultCodeBurstWindow
	}
	return &codeBurstLimiter{
		now:    time.Now,
		hits:   map[string][]time.Time{},
		logged: map[string]time.Time{},
		limit:  limit,
		window: window,
	}
}

func codeBurstKey(accountID int64, appID string) string {
	return fmt.Sprintf("%d|%s", accountID, strings.TrimSpace(appID))
}

func (b *codeBurstLimiter) settings() (int, time.Duration) {
	if b == nil {
		return 0, defaultCodeBurstWindow
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.limit, b.window
}

func (b *codeBurstLimiter) configure(limit int, window time.Duration) {
	if b == nil {
		return
	}
	if limit < 0 {
		limit = 0
	}
	if window <= 0 {
		window = defaultCodeBurstWindow
	}
	b.mu.Lock()
	b.limit = limit
	b.window = window
	if limit == 0 {
		b.hits = map[string][]time.Time{}
		b.logged = map[string]time.Time{}
	}
	b.mu.Unlock()
}

func (b *codeBurstLimiter) allow(accountID int64, appID string, now time.Time) (bool, time.Duration) {
	ok, retryAfter, _, _, _ := b.checkAndRecord(accountID, appID, now)
	return ok, retryAfter
}

func (b *codeBurstLimiter) checkAndRecord(accountID int64, appID string, now time.Time) (ok bool, retryAfter time.Duration, limit int, window time.Duration, shouldLog bool) {
	if b == nil {
		return true, 0, 0, defaultCodeBurstWindow, false
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	limit, window = b.limit, b.window
	if limit <= 0 {
		return true, 0, limit, window, false
	}
	key := codeBurstKey(accountID, appID)
	cutoff := now.Add(-window)
	if len(b.hits) > codeBurstMaxTrackedKeys {
		b.pruneStaleLocked(cutoff)
	}
	kept := b.hits[key][:0]
	for _, ts := range b.hits[key] {
		if ts.After(cutoff) {
			kept = append(kept, ts)
		}
	}
	if len(kept) >= limit {
		b.hits[key] = kept
		oldest := kept[0]
		retryAfter = oldest.Add(window).Sub(now)
		if retryAfter < 0 {
			retryAfter = 0
		}
		lastLog := b.logged[key]
		if lastLog.IsZero() || !now.Before(lastLog.Add(window)) {
			b.logged[key] = now
			shouldLog = true
		}
		return false, retryAfter, limit, window, shouldLog
	}
	b.hits[key] = append(kept, now)
	return true, 0, limit, window, false
}

func (b *codeBurstLimiter) pruneStaleLocked(cutoff time.Time) {
	for k, list := range b.hits {
		if len(list) == 0 || !list[len(list)-1].After(cutoff) {
			delete(b.hits, k)
		}
	}
	for k, ts := range b.logged {
		if !ts.After(cutoff) {
			delete(b.logged, k)
		}
	}
}

func (a *App) allowCodeBurst(w http.ResponseWriter, acc *store.WechatAccount, appID string) bool {
	if a == nil || a.codeBurst == nil || acc == nil {
		return true
	}
	now := a.codeBurst.now()
	ok, retryAfter, limit, window, shouldLog := a.codeBurst.checkAndRecord(acc.ID, appID, now)
	if ok {
		return true
	}
	if shouldLog {
		a.events.Emit("protocol.burst_limited", eventlog.ForAccount(acc).WithTarget(strings.TrimSpace(appID), eventlog.OpGetCode, ""))
	}
	retry := int(retryAfter.Seconds() + 0.999)
	if retry < 1 {
		retry = 1
	}
	windowSeconds := int(window.Seconds())
	if windowSeconds < 1 {
		windowSeconds = 1
	}
	w.Header().Set("Retry-After", strconv.Itoa(retry))
	writeRawJSON(w, http.StatusTooManyRequests, apiEnvelope{
		Code: http.StatusTooManyRequests,
		Msg:  "call deferred: 同一账号同一小程序取码过于频繁，请减少脚本重复取码",
		Data: map[string]any{
			"code_burst_limited": true,
			"limit":              limit,
			"window_seconds":     windowSeconds,
			"retry_after":        retry,
			"request_rejected":   false,
		},
	})
	return false
}
