package httpapi

import (
	"net/http"
	"strconv"
	"strings"

	"yyb_go/internal/store"
)

// Account enumeration is for scripts by default: one callable primary per
// explicit fleet. Its status summarizes the best visible member. Identity and
// profile remain the primary's; the console opts into the real member records.
// Selection uses stored status only; listing never refreshes or probes a login.
func accountListForRequest(w http.ResponseWriter, r *http.Request, accounts []*store.WechatAccount) ([]*store.WechatAccount, bool) {
	if value := r.URL.Query().Get("include_members"); value != "" {
		include, err := strconv.ParseBool(value)
		if err != nil {
			writeError(w, http.StatusBadRequest, "include_members must be a boolean")
			return nil, false
		}
		if include {
			return accounts, true
		}
	}
	return accountRepresentatives(accounts), true
}

func accountRepresentatives(accounts []*store.WechatAccount) []*store.WechatAccount {
	out := make([]*store.WechatAccount, 0, len(accounts))
	best := make(map[int64]*store.WechatAccount)
	for _, account := range accounts {
		root := fleetPrimaryOf(account)
		if root <= 0 {
			continue
		}
		if current := best[root]; current == nil || accountAvailabilityRank(account) < accountAvailabilityRank(current) {
			best[root] = account
		}
	}
	for _, account := range accounts {
		root := fleetPrimaryOf(account)
		if root <= 0 {
			out = append(out, account)
		} else if account.ID == root {
			// Do not mutate the real primary's status or substitute a member's
			// identity. If the primary is not visible, omit this group entirely.
			publicView := *account
			publicView.Status = best[root].Status
			out = append(out, &publicView)
		}
	}
	return out
}

func accountAvailabilityRank(account *store.WechatAccount) int {
	status := ""
	if account.Status != nil {
		status = strings.ToLower(strings.TrimSpace(*account.Status))
	}
	switch status {
	case "alive":
		return 0
	case store.StatusSessionOnly:
		return 1
	case "", "unknown":
		return 2
	default:
		return 3
	}
}
