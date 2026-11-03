package eventlog

import (
	"strings"
	"sync"
	"time"
)

// RecentRequest is one protocol request an account made shortly before an event
// that needs context, such as the account being refused by the upstream. It
// carries identifiers only: which AppID, which class of request, how it ended and
// how long before the event it happened.
type RecentRequest struct {
	AppID string  `json:"app_id"`
	Op    string  `json:"op,omitempty"`
	Event string  `json:"event"`
	AgeS  float64 `json:"age_s"`
	Code  int64   `json:"code,omitempty"`
}

const (
	recentPerAccount    = 40
	recentKeep          = 10 * time.Minute
	recentMaxAccounts   = 4096
	recentMaxInEvent    = 12
	recentMaxAgeInEvent = 3600.0
)

type recentEntry struct {
	at    time.Time
	appID string
	op    string
	event string
	code  int64
}

// recentBook remembers the last requests per account in memory. It is for the
// next time an account dies: the death event can then say which AppIDs the
// account had just asked for, without anyone having to reconstruct it from logs.
type recentBook struct {
	mu        sync.Mutex
	now       func() time.Time
	byAccount map[int64][]recentEntry
}

func newRecentBook() *recentBook {
	return &recentBook{now: time.Now, byAccount: map[int64][]recentEntry{}}
}

// observe notes a protocol event that names an AppID. Other events and events
// without an account are ignored.
func (b *recentBook) observe(event string, f Fields) {
	if b == nil || f.AccountID <= 0 || f.AppID == "" || !strings.HasPrefix(event, "protocol.") {
		return
	}
	e := recentEntry{at: b.now(), appID: f.AppID, op: f.Operation, event: strings.TrimPrefix(event, "protocol.")}
	if len(f.Rejections) > 0 {
		e.code = f.Rejections[0].Code
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.byAccount) >= recentMaxAccounts {
		b.byAccount = map[int64][]recentEntry{}
	}
	list := b.byAccount[f.AccountID]
	cut := 0
	for cut < len(list) && e.at.Sub(list[cut].at) > recentKeep {
		cut++
	}
	list = append(list[cut:], e)
	if len(list) > recentPerAccount {
		list = list[len(list)-recentPerAccount:]
	}
	b.byAccount[f.AccountID] = list
}

// snapshot returns up to max of the newest entries within window, oldest first.
func (b *recentBook) snapshot(accountID int64, window time.Duration, max int) []RecentRequest {
	if b == nil || accountID <= 0 || max <= 0 {
		return nil
	}
	now := b.now()
	b.mu.Lock()
	list := append([]recentEntry(nil), b.byAccount[accountID]...)
	b.mu.Unlock()
	var out []RecentRequest
	for _, e := range list {
		age := now.Sub(e.at)
		if age < 0 || age > window {
			continue
		}
		out = append(out, RecentRequest{AppID: e.appID, Op: e.op, Event: e.event, AgeS: float64(int(age.Seconds()*10)) / 10, Code: e.code})
	}
	if len(out) > max {
		out = out[len(out)-max:]
	}
	return out
}

// Recent returns what the account asked for in the last window, oldest first, for
// events that explain a death. It is safe on a nil Logger.
func (l *Logger) Recent(accountID int64, window time.Duration, max int) []RecentRequest {
	if l == nil {
		return nil
	}
	return l.recent.snapshot(accountID, window, max)
}

// cleanRecent keeps only entries that carry safe identifiers.
func cleanRecent(in []RecentRequest) []RecentRequest {
	var out []RecentRequest
	for _, r := range in {
		if len(out) >= recentMaxInEvent {
			break
		}
		if !safeAppID.MatchString(r.AppID) || !safeOperationOrEmpty(r.Op) || !safeRecentEvent(r.Event) {
			continue
		}
		if r.AgeS < 0 || r.AgeS > recentMaxAgeInEvent {
			continue
		}
		out = append(out, r)
	}
	return out
}

func safeOperationOrEmpty(op string) bool { return op == "" || safeOperation(op) }

func safeRecentEvent(name string) bool {
	_, ok := catalog["protocol."+name]
	return ok
}
