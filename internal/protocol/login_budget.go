package protocol

import (
	"sync"
	"time"

	"yyb_go/internal/eventlog"
)

// A login budget caps how often business traffic may make the OAuth layer work
// for one account: a new login ticket, a ManualAuth with it, a forced
// credential refresh.
//
// Replaying the shape of a Qinglong burst (120 requests for 120 different
// AppIDs within four minutes) against a healthy authorization killed it in
// both the old and the new code, within about three minutes. The new code did
// it with roughly 45 login tickets in 150 seconds, the old code with repeated
// forced refreshes, and both ended on the same rejection chain
// (login_buffer -101 / 42007, refresh_token -109, check_login -109 / 42007) as
// the production accounts that died in the 00:19 burst. An authorization that
// nothing but the keepalive touched renewed on schedule. What matters is the
// number of OAuth-layer calls per authorization, not the number of requests.
//
// The budget therefore counts those calls per account in a sliding window and
// refuses the next one once the limit is reached, so the request fails with its
// original error instead of provoking the upstream again. Keepalive and manual
// refreshes are not gated: they are scheduled or deliberate and rare.

const (
	// DefaultLoginBudget and DefaultLoginWindow are what the service runs with
	// unless configured otherwise. About one login every three minutes per
	// account is far below the rates that killed authorizations (ten in 18
	// seconds, 45 in 150 seconds) and above the four in eleven minutes that an
	// authorization survived.
	DefaultLoginBudget = 3
	DefaultLoginWindow = 10 * time.Minute

	loginBudgetSummaryEvery = 30 * time.Second
	maxBudgetAccounts       = 4096
)

type budgetAccount struct {
	uses    []time.Time // starts of the calls still inside the window, oldest first
	refused uint64      // refusals since the last summary
	since   time.Time   // start of the current summary window
}

type budgetSummary struct {
	Refused uint64
	Window  time.Duration
}

type loginBudget struct {
	now func() time.Time

	mu       sync.Mutex
	limit    int
	window   time.Duration
	accounts map[int64]*budgetAccount
}

// newLoginBudget returns a budget of limit calls per window. A limit of zero or
// less disables it.
func newLoginBudget(limit int, window time.Duration) *loginBudget {
	b := &loginBudget{now: time.Now, accounts: map[int64]*budgetAccount{}}
	b.set(limit, window)
	return b
}

func (b *loginBudget) set(limit int, window time.Duration) {
	if window <= 0 {
		window = DefaultLoginWindow
	}
	b.mu.Lock()
	b.limit, b.window = limit, window
	b.mu.Unlock()
}

// take claims one call for the account. It reports false when the account has
// already used its budget inside the window. The summary is non-nil when a run
// of refusals has gone on long enough to be worth one log event.
func (b *loginBudget) take(accountID int64) (bool, *budgetSummary) {
	if b == nil {
		return true, nil
	}
	now := b.now()
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.limit <= 0 {
		return true, nil
	}
	st := b.accounts[accountID]
	if st == nil {
		b.pruneLocked(now)
		st = &budgetAccount{}
		b.accounts[accountID] = st
	}
	cutoff := now.Add(-b.window)
	drop := 0
	for drop < len(st.uses) && !st.uses[drop].After(cutoff) {
		drop++
	}
	st.uses = st.uses[drop:]
	if len(st.uses) < b.limit {
		st.uses = append(st.uses, now)
		return true, nil
	}
	st.refused++
	if st.since.IsZero() {
		st.since = now
		return false, nil
	}
	if now.Sub(st.since) < loginBudgetSummaryEvery {
		return false, nil
	}
	summary := &budgetSummary{Refused: st.refused, Window: now.Sub(st.since)}
	st.refused, st.since = 0, time.Time{}
	return false, summary
}

// pruneLocked forgets accounts whose window has emptied, so the table stays
// small even when a script walks many account IDs.
func (b *loginBudget) pruneLocked(now time.Time) {
	if len(b.accounts) < maxBudgetAccounts {
		return
	}
	cutoff := now.Add(-b.window)
	for id, st := range b.accounts {
		if st.refused == 0 && (len(st.uses) == 0 || !st.uses[len(st.uses)-1].After(cutoff)) {
			delete(b.accounts, id)
		}
	}
}

// AllowLogin is asked before traffic makes the OAuth layer do work for an
// account. A false answer means the account has used its budget: the caller
// should fail the request with the error it already has and must not retry.
// The provider only labels the resulting event with the account's channel.
func (p *Pool) AllowLogin(accountID int64, provider string) bool {
	ok, summary := p.budget.take(accountID)
	if summary != nil {
		p.events.Emit("protocol.login_limited", eventlog.Fields{
			AccountID:  accountID,
			Channel:    eventlog.ChannelFor(provider),
			Refused:    summary.Refused,
			DurationMS: summary.Window.Milliseconds(),
		})
	}
	return ok
}

// SetLoginBudget changes the limit and window at run time.
func (p *Pool) SetLoginBudget(limit int, window time.Duration) {
	p.budget.set(limit, window)
}
