package httpapi

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"sync"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

// Pacing spaces the upstream requests of one account.
//
// Production logs show hundreds of scheduled scripts asking the same few
// accounts for wx.login codes in the same minute: 14 successful codes in 7
// seconds for one account, 2500 requests in 5 minutes across the instance, and
// two fresh authorizations rejected by every layer inside that burst. Nothing
// else touched them. The breaker only reacts to failures, so a burst of
// successful requests passes it untouched. Pacing is the missing part: it keeps
// the request rate an account sees upstream close to what one person could
// produce, and answers the excess with a local 429 that costs the upstream
// nothing.
//
// Every request takes the next free slot of its account. A slot is MinGap after
// the previous one. A request whose slot is more than MaxWait away is refused
// immediately instead of queueing, so the queue stays short and a caller with a
// 30 second timeout is never held past it. Accounts are independent, and an
// idle account always has a free slot.

const (
	pacingMaxGap       = time.Minute
	pacingMaxWait      = 2 * time.Minute
	pacingFileCheck    = time.Second
	pacingSummaryEvery = 30 * time.Second
	maxPacedAccounts   = 4096
)

// pacingSettings are the two knobs. A zero MinGap turns pacing off.
type pacingSettings struct {
	MinGap  time.Duration
	MaxWait time.Duration
}

// pacingFile is the optional runtime override (tuning.json beside the
// database). A missing key keeps the configured default, so the file can change
// one knob without restating the other.
type pacingFile struct {
	MinGapMS  *int64 `json:"code_min_gap_ms"`
	MaxWaitMS *int64 `json:"code_max_wait_ms"`
}

type fileStamp struct {
	mod    time.Time
	size   int64
	exists bool
}

type pacedAccount struct {
	next    time.Time // earliest start of the next request
	waited  uint64    // requests that queued behind an earlier one
	refused uint64    // requests refused because the queue was full
	since   time.Time // start of the current summary window
}

// pacingSummary is handed back now and then so the caller can log one event for
// a burst instead of one per request.
type pacingSummary struct {
	Waited  uint64
	Refused uint64
	Window  time.Duration
}

type accountPacer struct {
	file     string
	defaults pacingSettings
	now      func() time.Time

	mu       sync.Mutex
	accounts map[int64]*pacedAccount
	settings pacingSettings
	checked  time.Time
	stamp    fileStamp
}

// newAccountPacer returns a pacer using defaults until the optional file says
// otherwise. An empty file path means no override.
func newAccountPacer(file string, defaults pacingSettings) *accountPacer {
	return &accountPacer{
		file:     file,
		defaults: defaults,
		settings: defaults,
		now:      time.Now,
		accounts: map[int64]*pacedAccount{},
	}
}

func clampMS(value int64, limit time.Duration) time.Duration {
	if value <= 0 {
		return 0
	}
	d := time.Duration(value) * time.Millisecond
	if d > limit {
		return limit
	}
	return d
}

// settingsLocked returns the active settings, re-reading the override file at
// most once a second and only when it changed. A file that cannot be parsed
// leaves the previous settings in force.
func (p *accountPacer) settingsLocked(now time.Time) pacingSettings {
	if p.file == "" || now.Sub(p.checked) < pacingFileCheck {
		return p.settings
	}
	p.checked = now
	info, err := os.Stat(p.file)
	if err != nil {
		if p.stamp.exists {
			p.stamp = fileStamp{}
			p.settings = p.defaults
			log.Printf("pacing: %s is gone; using the configured defaults (gap %s, wait %s)", p.file, p.defaults.MinGap, p.defaults.MaxWait)
		}
		return p.settings
	}
	stamp := fileStamp{mod: info.ModTime(), size: info.Size(), exists: true}
	if stamp == p.stamp {
		return p.settings
	}
	p.stamp = stamp
	raw, err := os.ReadFile(p.file)
	if err != nil {
		log.Printf("pacing: cannot read %s: %v; keeping the current settings", p.file, err)
		return p.settings
	}
	var parsed pacingFile
	if err := json.Unmarshal(raw, &parsed); err != nil {
		log.Printf("pacing: %s is not valid JSON (%v); keeping the current settings", p.file, err)
		return p.settings
	}
	settings := p.defaults
	if parsed.MinGapMS != nil {
		settings.MinGap = clampMS(*parsed.MinGapMS, pacingMaxGap)
	}
	if parsed.MaxWaitMS != nil {
		settings.MaxWait = clampMS(*parsed.MaxWaitMS, pacingMaxWait)
	}
	p.settings = settings
	log.Printf("pacing: %s applied (gap %s, wait %s)", p.file, settings.MinGap, settings.MaxWait)
	return settings
}

// reserve claims the next slot for the account. It returns how long the caller
// must wait before starting, or a local refusal when the queue is full. The
// summary is non-nil when a burst has just been long enough to report.
func (p *accountPacer) reserve(accountID int64) (time.Duration, *pacingSummary, error) {
	if p == nil {
		return 0, nil, nil
	}
	now := p.now()
	p.mu.Lock()
	defer p.mu.Unlock()
	cfg := p.settingsLocked(now)
	if cfg.MinGap <= 0 {
		return 0, nil, nil
	}
	st := p.accounts[accountID]
	if st == nil {
		p.pruneLocked(now)
		st = &pacedAccount{}
		p.accounts[accountID] = st
	}
	slot := st.next
	if slot.Before(now) {
		slot = now
	}
	wait := slot.Sub(now)
	var refusal error
	if wait > cfg.MaxWait {
		retry := wait - cfg.MaxWait
		if retry < cfg.MinGap {
			retry = cfg.MinGap
		}
		st.refused++
		refusal = &circuitOpenError{Scope: "pacing", RetryAfter: retry}
		wait = 0
	} else {
		st.next = slot.Add(cfg.MinGap)
		if wait > 0 {
			st.waited++
		}
	}
	var summary *pacingSummary
	if st.waited+st.refused > 0 {
		if st.since.IsZero() {
			st.since = now
		} else if now.Sub(st.since) >= pacingSummaryEvery {
			summary = &pacingSummary{Waited: st.waited, Refused: st.refused, Window: now.Sub(st.since)}
			st.waited, st.refused, st.since = 0, 0, time.Time{}
		}
	}
	return wait, summary, refusal
}

// pruneLocked bounds memory when the account table is large: accounts whose
// queue has drained and that have nothing left to report are forgotten.
func (p *accountPacer) pruneLocked(now time.Time) {
	if len(p.accounts) < maxPacedAccounts {
		return
	}
	for id, st := range p.accounts {
		if !now.Before(st.next) && st.waited+st.refused == 0 {
			delete(p.accounts, id)
		}
	}
}

// paceWXApp waits for the account's slot, or refuses the request when the queue
// is full. A refusal is a *circuitOpenError, so every entry point maps it to the
// same local 429 it already uses for the breaker.
func (a *App) paceWXApp(ctx context.Context, acc *store.WechatAccount) error {
	wait, summary, err := a.pacer.reserve(acc.ID)
	if summary != nil {
		fields := eventlog.ForAccount(acc)
		fields.Waited, fields.Refused = summary.Waited, summary.Refused
		fields.DurationMS = summary.Window.Milliseconds()
		a.events.Emit("protocol.paced", fields)
	}
	if err != nil {
		return err
	}
	if wait <= 0 {
		return nil
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
