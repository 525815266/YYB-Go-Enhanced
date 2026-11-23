package httpapi

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/pcmgr"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

const (
	fleetFailoverCooldown    = 15 * time.Minute
	fleetFailoverMaxCooldown = 6 * time.Hour
	fleetPenaltyStrikeReset  = 24 * time.Hour
)

type fleetPenalty struct {
	until   time.Time
	strikes int
	last    time.Time
}

// Fleet dispatch: accounts bound to the same WeChat across channels
// (store.FleetPrimaryID) share request traffic so that no single client
// identity absorbs an entire concurrent burst. Every member keeps its own
// session, pacing gap, login budget and circuit breakers; a member whose
// upstream pushes back hands the request to the next member instead of
// hammering the same identity.

// fleetLRU is process-local and resets on restart; it only steers fairness,
// never correctness.
var fleetLRU = struct {
	mu       sync.Mutex
	last     map[int64]int64
	sequence int64
}{last: map[int64]int64{}}

func fleetMarkUsed(id int64) {
	fleetLRU.mu.Lock()
	defer fleetLRU.mu.Unlock()
	// A sequence preserves order when Windows clock readings tie or the wall
	// clock moves backwards. This is process-local scheduling, not a timestamp.
	fleetLRU.sequence++
	fleetLRU.last[id] = fleetLRU.sequence
}

// fleetOrder puts usable members first, then applies least-recently-used
// fairness within the same availability class. A saved session or an
// expired credential is a recovery fallback, not a preferred target when a
// peer is already known alive.
func fleetOrder(members []*store.WechatAccount) []*store.WechatAccount {
	return fleetOrderWithDemotions(members, nil)
}

func fleetOrderWithDemotions(members []*store.WechatAccount, demoted map[int64]bool) []*store.WechatAccount {
	ordered := append([]*store.WechatAccount(nil), members...)
	fleetLRU.mu.Lock()
	defer fleetLRU.mu.Unlock()
	sort.SliceStable(ordered, func(i, j int) bool {
		ri := fleetDispatchRank(ordered[i], demoted[ordered[i].ID])
		rj := fleetDispatchRank(ordered[j], demoted[ordered[j].ID])
		if ri != rj {
			return ri < rj
		}
		li, lj := fleetLRU.last[ordered[i].ID], fleetLRU.last[ordered[j].ID]
		if li != lj {
			return li < lj
		}
		return ordered[i].ID < ordered[j].ID
	})
	return ordered
}

func fleetDispatchRank(account *store.WechatAccount, penalized bool) int {
	base := fleetDispatchAvailabilityRank(account)
	if base == 0 && penalized {
		// Keep a recently failed alive channel behind healthy alive peers,
		// while still preferring it over session-only or expired records.
		return 1
	}
	if base >= 1 {
		return base + 1
	}
	return base
}

func fleetDispatchAvailabilityRank(account *store.WechatAccount) int {
	status := "unknown"
	if account != nil && account.Status != nil {
		status = strings.ToLower(strings.TrimSpace(*account.Status))
	}
	switch status {
	case "alive":
		return 0
	case "", "unknown":
		return 1
	case store.StatusSessionOnly:
		return 2
	case "expired":
		return 3
	default:
		return 3
	}
}

func (a *App) activeFleetDemotions(members []*store.WechatAccount) map[int64]bool {
	return a.activeFleetDemotionsAt(members, time.Now())
}

func (a *App) activeFleetDemotionsAt(members []*store.WechatAccount, now time.Time) map[int64]bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.fleetDemoted) == 0 {
		return nil
	}
	var out map[int64]bool
	for _, m := range members {
		penalty, ok := a.fleetDemoted[m.ID]
		if !ok || !now.Before(penalty.until) {
			// Keep expired entries intact so consecutive failures still escalate
			// until a call succeeds or 24h passes without another failure.
			continue
		}
		if out == nil {
			out = make(map[int64]bool, len(members))
		}
		out[m.ID] = true
	}
	return out
}

func fleetCooldownForStrike(strikes int) time.Duration {
	if strikes <= 1 {
		return fleetFailoverCooldown
	}
	cooldown := fleetFailoverCooldown
	for i := 1; i < strikes && cooldown < fleetFailoverMaxCooldown; i++ {
		if cooldown > fleetFailoverMaxCooldown/2 {
			return fleetFailoverMaxCooldown
		}
		cooldown *= 2
	}
	if cooldown > fleetFailoverMaxCooldown {
		return fleetFailoverMaxCooldown
	}
	return cooldown
}

func (a *App) markFleetMemberFailed(accountID int64) {
	a.markFleetMemberFailedAt(accountID, time.Now())
}

func (a *App) markFleetMemberFailedAt(accountID int64, now time.Time) fleetPenalty {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.fleetDemoted == nil {
		a.fleetDemoted = map[int64]fleetPenalty{}
	}
	penalty := a.fleetDemoted[accountID]
	if !penalty.last.IsZero() && now.Sub(penalty.last) > fleetPenaltyStrikeReset {
		penalty.strikes = 0
	}
	penalty.strikes++
	penalty.until = now.Add(fleetCooldownForStrike(penalty.strikes))
	penalty.last = now
	a.fleetDemoted[accountID] = penalty
	return penalty
}

func (a *App) clearFleetMemberPenalty(accountID int64) {
	a.mu.Lock()
	delete(a.fleetDemoted, accountID)
	a.mu.Unlock()
}

// fleetFailover reports whether another channel identity could plausibly
// answer after this error. Business-shaped answers (the upstream judged the
// request itself) and globally blocked AppIDs are not retried across
// members: every member would hit the same wall.
func fleetFailover(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.Canceled) {
		return false
	}
	var h5Attempt *protocol.H5AuthorizationAttemptError
	if errors.As(err, &h5Attempt) {
		return false
	}
	if protocol.IsBusinessResponseRejection(err) {
		// The upstream judged the request itself (appid/redirect pairing,
		// scope...). Every member would receive the same verdict.
		return false
	}
	var expired accountExpiredError
	if errors.As(err, &expired) {
		return true
	}
	var circuitOpen *circuitOpenError
	if errors.As(err, &circuitOpen) {
		// The member's (account, appid) breaker is holding; its fleet peers
		// have independent breakers.
		return true
	}
	if protocol.IsApplicationRequestRejection(err) {
		return true
	}
	if errors.Is(err, errLoginBudget) {
		return true
	}
	if errors.Is(err, protocol.ErrCachedSessionUnavailable) {
		return true
	}
	var manualRejected *protocol.ManualAuthRejectedError
	if errors.As(err, &manualRejected) {
		// Saved ticket rejected on this channel (e.g. session-only or
		// channel credential failure); sibling channels hold independent sessions.
		return true
	}
	var bufferRejected *protocol.LoginBufferRejectedError
	if errors.As(err, &bufferRejected) {
		// The member's own credential issuance was refused; its fleet peers
		// hold independent credentials.
		return true
	}
	var refreshRejected *protocol.RefreshRejectedError
	if errors.As(err, &refreshRejected) {
		return true
	}
	var bufferErr *pcmgr.BufferRejectedError
	if errors.As(err, &bufferErr) {
		return true
	}
	var rpcErr *pcmgr.RPCError
	if errors.As(err, &rpcErr) {
		return true
	}
	return false
}

// invokeWithFleet resolves the account's fleet and dispatches. Accounts
// without a fleet (the default) go straight through. Fleet dispatch applies
// at this level so every member resolves its own proxy, pacing and session
// machinery; the caller's `call` stays identical for each member.
func (a *App) invokeWithFleet(ctx context.Context, acc *store.WechatAccount, appID string, payload map[string]any, call wxappCall) (map[string]any, error) {
	primary := fleetPrimaryOf(acc)
	if primary > 0 && primary != acc.ID {
		return nil, &fleetMemberReferenceError{PrimaryID: primary}
	}
	if primary <= 0 {
		return a.invokeWXApp(ctx, acc, appID, payload, call)
	}
	members, err := a.db.FleetMembers(ctx, primary)
	if err != nil || len(members) <= 1 {
		// A broken binding never blocks the request: the requested account
		// still serves it standalone.
		return a.invokeWXApp(ctx, acc, appID, payload, call)
	}
	var lastErr error
	for _, member := range fleetOrderWithDemotions(members, a.activeFleetDemotions(members)) {

		fleetMarkUsed(member.ID)
		result, callErr := a.invokeWXApp(ctx, member, appID, payload, call)
		if callErr == nil {
			a.clearFleetMemberPenalty(member.ID)
			if member.ID != acc.ID {
				result["fleet_member"] = member.ID
			}
			return result, nil
		}
		if !fleetFailover(callErr) {
			return nil, callErr
		}
		a.markFleetMemberFailed(member.ID)
		lastErr = callErr
		a.events.Emit("fleet.failover", eventlog.Fields{
			AccountID: member.ID,
			Channel:   eventlog.ChannelFor(protocol.ProviderOf(member)),
			AppID:     appID,
			Source:    "next-member",
		})
	}
	return nil, lastErr
}

type fleetMemberReferenceError struct{ PrimaryID int64 }

func (e *fleetMemberReferenceError) Error() string {
	return fmt.Sprintf("channel member cannot be called directly; use primary account ref=%d", e.PrimaryID)
}

func fleetPrimaryOf(acc *store.WechatAccount) int64 {
	if acc == nil || acc.FleetPrimaryID == nil {
		return 0
	}
	return *acc.FleetPrimaryID
}
