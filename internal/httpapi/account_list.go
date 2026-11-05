package httpapi

import (
	"net/http"
	"strconv"
	"strings"

	"yyb_go/internal/store"
)

// Account enumeration is for scripts by default: one saved authorization per
// explicit fleet. The console opts into the full list for channel management.
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
	groups := make(map[int64]int)
	for _, account := range accounts {
		root := fleetPrimaryOf(account)
		if root <= 0 {
			out = append(out, account)
			continue
		}
		if index, exists := groups[root]; exists {
			current := out[index]
			rank, currentRank := accountAvailabilityRank(account), accountAvailabilityRank(current)
			if rank < currentRank || rank == currentRank && (account.ID == root || current.ID != root && account.ID < current.ID) {
				out[index] = account
			}
		} else {
			groups[root] = len(out)
			out = append(out, account)
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
