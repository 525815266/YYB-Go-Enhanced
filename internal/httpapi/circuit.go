package httpapi

import (
	"errors"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"sync"
	"time"

	"yyb_go/internal/eventlog"
)

// writeIfCircuitOpen renders a held-off request as HTTP 429 with the retry
// delay, distinct from an upstream 502 or a re-scan 409. It reports whether it
// handled the error.
func writeIfCircuitOpen(w http.ResponseWriter, err error) bool {
	var open *circuitOpenError
	if !errors.As(err, &open) {
		return false
	}
	retry := int(open.RetryAfter.Seconds() + 0.999)
	if retry < 1 {
		retry = 1
	}
	w.Header().Set("Retry-After", strconv.Itoa(retry))
	writeRawJSON(w, http.StatusTooManyRequests, apiEnvelope{
		Code: http.StatusTooManyRequests,
		Msg:  "call deferred: " + open.Error(),
		Data: map[string]any{"circuit_open": open.Scope, "retry_after": retry, "request_rejected": false},
	})
	return true
}

// The breaker is traffic control only. A short-circuited request never changes
// account status, clears credentials or touches the database; it returns a
// local 429 so a caller backs off instead of provoking the upstream again.
//
// Two layers, matching the observed offline amplification:
//   - target: one (account, AppID) that the application layer rejects (-14) or
//     that returns no code keeps being retried on every script run. After a few
//     such results the target is held off for a cooldown.
//   - session: when several distinct AppIDs on one account are all rejected
//     within a short window, the whole protocol session is being refused
//     upstream (the account-level lockout in #112/#121). Business requests for
//     that account are held off until a longer cooldown passes.
//
// A single success clears both layers for that account: the session works again.

type circuitOutcome int

const (
	circuitOK        circuitOutcome = iota // a usable response
	circuitAppReject                       // application layer -14 for this AppID
	circuitEmptyCode                       // authenticated but no wx.login code
	circuitOther                           // network, -10, OAuth, cancellation: not ours
)

type circuitConfig struct {
	Enabled                bool
	TargetAppRejects       int           // consecutive -14 before a target opens
	TargetEmptyCodes       int           // consecutive empty codes before a target opens
	TargetCooldown         time.Duration // how long a target stays held off
	TargetWindow           time.Duration // rejections count as consecutive only within this gap
	SessionDistinctTargets int           // distinct -14 AppIDs that open the session
	SessionWindow          time.Duration // window those distinct AppIDs are counted in
	SessionCooldown        time.Duration // how long the session stays held off
}

func circuitConfigFor(disabled bool) circuitConfig {
	cfg := defaultCircuitConfig()
	if disabled {
		cfg.Enabled = false
	}
	return cfg
}

func defaultCircuitConfig() circuitConfig {
	return circuitConfig{
		Enabled:                true,
		TargetAppRejects:       2,
		TargetEmptyCodes:       3,
		TargetCooldown:         10 * time.Minute,
		TargetWindow:           30 * time.Minute,
		SessionDistinctTargets: 3,
		SessionWindow:          5 * time.Minute,
		SessionCooldown:        15 * time.Minute,
	}
}

// circuitOpenError is returned while a target or session is held off. It is a
// local decision, never an upstream rejection, so it carries no WeChat code and
// must not be mapped to account expiry.
type circuitOpenError struct {
	Scope      string // "target" or "session"
	RetryAfter time.Duration
}

func (e *circuitOpenError) Error() string {
	if e.Scope == "pacing" {
		return fmt.Sprintf("该账号的请求过于密集，已排满，%ds 后再试；避免短时间内大量请求触发风控", int(e.RetryAfter.Seconds()+0.999))
	}
	scope := "目标"
	if e.Scope == "session" {
		scope = "账号会话"
	}
	return fmt.Sprintf("%s暂时熔断，%ds 内不再请求上游；避免重复触发风控", scope, int(e.RetryAfter.Seconds()+0.999))
}

type targetBreaker struct {
	appRejects int
	emptyCodes int
	last       time.Time
	openUntil  time.Time
}

// targetKey is one (account, AppID, request class) the breaker tracks. A phone
// or operateWxData request that the host refuses for its own reasons must not
// hold off wx.login for the same AppID, so the class is part of the key.
type targetKey struct {
	account int64
	appID   string
	op      string
}

// maxBreakerTargets bounds memory: AppIDs come from callers, so a script that
// walks arbitrary AppIDs must not grow the map without limit.
const maxBreakerTargets = 4096

type sessionBreaker struct {
	recent    map[string]time.Time // AppID -> last -14 within the window
	openUntil time.Time
}

type circuitBreaker struct {
	cfg circuitConfig
	now func() time.Time

	mu       sync.Mutex
	targets  map[targetKey]*targetBreaker
	sessions map[int64]*sessionBreaker
	// overflowLogged rate-limits the saturation warning.
	overflowLogged time.Time
}

func newCircuitBreaker(cfg circuitConfig) *circuitBreaker {
	return &circuitBreaker{
		cfg:      cfg,
		now:      time.Now,
		targets:  map[targetKey]*targetBreaker{},
		sessions: map[int64]*sessionBreaker{},
	}
}

// check reports whether a request for this target may reach the upstream. The
// session layer is checked first: a wholesale-rejected account holds off every
// target. A disabled breaker always allows.
func (c *circuitBreaker) check(accountID int64, appID, op string) error {
	if c == nil || !c.cfg.Enabled {
		return nil
	}
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	if s := c.sessions[accountID]; s != nil && now.Before(s.openUntil) {
		return &circuitOpenError{Scope: "session", RetryAfter: s.openUntil.Sub(now)}
	}
	if t := c.targets[targetKey{accountID, appID, op}]; t != nil && now.Before(t.openUntil) {
		return &circuitOpenError{Scope: "target", RetryAfter: t.openUntil.Sub(now)}
	}
	return nil
}

// record folds one call outcome into both layers and reports whether it opened
// a breaker (for logging). A success clears the account's session and this
// target; unrelated failures (circuitOther) are ignored.
func (c *circuitBreaker) record(accountID int64, appID, op string, outcome circuitOutcome) (openedScope string) {
	if c == nil || !c.cfg.Enabled {
		return ""
	}
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	key := targetKey{accountID, appID, op}

	if outcome == circuitOK {
		delete(c.targets, key)
		delete(c.sessions, accountID)
		return ""
	}
	if outcome == circuitOther {
		return ""
	}

	if t := c.targetLocked(key, now); t != nil {
		if !t.last.IsZero() && !now.Before(t.openUntil) && now.Sub(t.last) > c.cfg.TargetWindow {
			t.appRejects, t.emptyCodes = 0, 0
		}
		t.last = now
		switch outcome {
		case circuitAppReject:
			t.appRejects++
		case circuitEmptyCode:
			t.emptyCodes++
		}
		if !now.Before(t.openUntil) &&
			(t.appRejects >= c.cfg.TargetAppRejects || t.emptyCodes >= c.cfg.TargetEmptyCodes) {
			t.openUntil = now.Add(c.cfg.TargetCooldown)
			t.appRejects, t.emptyCodes = 0, 0
			openedScope = "target"
		}
	}

	// Only an application-layer -14 on a code request counts toward a wholesale
	// session lockout. An empty code means "check this AppID", not that the
	// whole session died, and phone or operate requests are refused for
	// ordinary reasons too, so they only feed their own target.
	if outcome == circuitAppReject && countsTowardSession(op) {
		s := c.sessions[accountID]
		if s == nil {
			s = &sessionBreaker{recent: map[string]time.Time{}}
			c.sessions[accountID] = s
		}
		cutoff := now.Add(-c.cfg.SessionWindow)
		s.recent[appID] = now
		for id, at := range s.recent {
			if at.Before(cutoff) {
				delete(s.recent, id)
			}
		}
		if !now.Before(s.openUntil) && len(s.recent) >= c.cfg.SessionDistinctTargets {
			s.openUntil = now.Add(c.cfg.SessionCooldown)
			s.recent = map[string]time.Time{}
			openedScope = "session"
		}
	}
	return openedScope
}

// targetLocked returns the tracker for key, creating it unless the map is full
// of recently active targets. The caller holds c.mu.
func (c *circuitBreaker) targetLocked(key targetKey, now time.Time) *targetBreaker {
	if t := c.targets[key]; t != nil {
		return t
	}
	if len(c.targets) >= maxBreakerTargets {
		for k, t := range c.targets {
			if !now.Before(t.openUntil) && now.Sub(t.last) >= time.Hour {
				delete(c.targets, k)
			}
		}
	}
	if len(c.targets) >= maxBreakerTargets {
		// Still full of recent targets: make room for the new one by dropping the
		// least recently active target that is not currently holding requests
		// off, so a new target is never silently left unprotected.
		var oldestKey targetKey
		var oldest time.Time
		found := false
		for k, t := range c.targets {
			if !now.Before(t.openUntil) && (!found || t.last.Before(oldest)) {
				oldestKey, oldest, found = k, t.last, true
			}
		}
		if !found {
			// Every slot is holding requests off, which needs thousands of distinct
			// refused targets within one cooldown. New targets pass untracked (the
			// breaker is best effort and must not block healthy traffic), but the
			// saturation is reported instead of staying silent.
			if now.Sub(c.overflowLogged) >= time.Minute {
				c.overflowLogged = now
				log.Printf("protocol: code breaker table is full of open targets (%d); new targets are not tracked", maxBreakerTargets)
			}
			return nil
		}
		delete(c.targets, oldestKey)
	}
	t := &targetBreaker{}
	c.targets[key] = t
	return t
}

// countsTowardSession reports whether a -14 on this request class is evidence
// that the whole protocol session is refused. An unspecified class counts.
func countsTowardSession(op string) bool {
	return op == "" || op == eventlog.OpGetCode
}

// forget drops all breaker state for an account, used after a re-scan so a new
// authorization starts clean.
func (c *circuitBreaker) forget(accountID int64) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.sessions, accountID)
	for key := range c.targets {
		if key.account == accountID {
			delete(c.targets, key)
		}
	}
}
