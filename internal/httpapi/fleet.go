package httpapi

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/pcmgr"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

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

// fleetOrder puts the least recently used member first (ties by id) so that
// concurrent callers naturally rotate through the fleet.
func fleetOrder(members []*store.WechatAccount) []*store.WechatAccount {
	ordered := append([]*store.WechatAccount(nil), members...)
	fleetLRU.mu.Lock()
	defer fleetLRU.mu.Unlock()
	sort.SliceStable(ordered, func(i, j int) bool {
		li, lj := fleetLRU.last[ordered[i].ID], fleetLRU.last[ordered[j].ID]
		if li != lj {
			return li < lj
		}
		return ordered[i].ID < ordered[j].ID
	})
	return ordered
}

// fleetFailover reports whether another channel identity could plausibly
// answer after this error. Business-shaped answers (the upstream judged the
// request itself) and globally blocked AppIDs are not retried across
// members: every member would hit the same wall.
func fleetFailover(err error) bool {
	if err == nil {
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
	for _, member := range fleetOrder(members) {

		fleetMarkUsed(member.ID)
		result, callErr := a.invokeWXApp(ctx, member, appID, payload, call)
		if callErr == nil {
			if member.ID != acc.ID {
				result["fleet_member"] = member.ID
			}
			return result, nil
		}
		if !fleetFailover(callErr) {
			return nil, callErr
		}
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
