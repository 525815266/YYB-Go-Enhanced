package httpapi

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

// Fleet management: bind accounts that belong to the same WeChat (verified
// by unionid) into one dispatch group so concurrent traffic rotates across
// channel identities instead of piling onto a single one. Routes live in
// the browser-session-protected section of the router.

type fleetBindRequest struct {
	Ref        string `json:"ref"`
	PrimaryRef string `json:"primary_ref"`
	Force      bool   `json:"force"`
}

type fleetUnbindRequest struct {
	Ref string `json:"ref"`
}

// handleFleetAPI dispatches the /api/fleet route. The router registers it
// inside the browser-session-protected section, so no extra auth check here.
func (a *App) handleFleetAPI(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		a.handleFleetBind(w, r)
	case http.MethodDelete:
		a.handleFleetUnbind(w, r)
	case http.MethodGet:
		a.handleFleetStatus(w, r)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

// fleetBindOutcome is the result of binding one account into a fleet. Status is
// the HTTP status a single bind answers with when Error is set.
type fleetBindOutcome struct {
	Status int
	Notice string
	Error  string
}

// bindFleetMember binds member into the fleet rooted at primary. Both accounts
// must belong to the same owner, the member must not sit in another fleet, and
// the WeChat unionid of the two must not differ unless force is set. A missing
// unionid is let through with a notice, a confirmed mismatch is not.
func (a *App) bindFleetMember(ctx context.Context, member, primary *store.WechatAccount, force bool) fleetBindOutcome {
	if member.ID == primary.ID {
		return fleetBindOutcome{Status: http.StatusUnprocessableEntity, Error: "成员与主账号是同一个账号"}
	}
	if fleetPrimaryOf(member) > 0 && fleetPrimaryOf(member) != primary.ID {
		return fleetBindOutcome{Status: http.StatusUnprocessableEntity, Error: fmt.Sprintf("账号 %d 已绑定其他编队（主账号 %d），请先解绑", member.ID, fleetPrimaryOf(member))}
	}
	if a.auth != nil {
		memberOwner, memberErr := a.auth.AccountOwner(ctx, member.ID)
		primaryOwner, primaryErr := a.auth.AccountOwner(ctx, primary.ID)
		if memberErr != nil && !errors.Is(memberErr, sql.ErrNoRows) || primaryErr != nil && !errors.Is(primaryErr, sql.ErrNoRows) {
			return fleetBindOutcome{Status: http.StatusInternalServerError, Error: "读取账号归属失败"}
		}
		if memberOwner != primaryOwner {
			return fleetBindOutcome{Status: http.StatusConflict, Error: "两个账号归属不同，不能编队；请先核对归属"}
		}
	}
	notice := ""
	if !force {
		match, detail, err := a.fleetUnionIDMatch(ctx, member, primary)
		if err != nil {
			return fleetBindOutcome{Status: http.StatusBadGateway, Error: "读取账号资料失败: " + err.Error()}
		}
		if !match {
			return fleetBindOutcome{Status: http.StatusUnprocessableEntity, Error: detail}
		}
		notice = detail
	}
	// The primary anchors the fleet: it points at itself so member queries
	// and dispatch both resolve through one id.
	if fleetPrimaryOf(primary) == 0 {
		if err := a.db.SetAccountFleet(ctx, primary.ID, primary.ID); err != nil {
			return fleetBindOutcome{Status: http.StatusInternalServerError, Error: "绑定主账号失败: " + err.Error()}
		}
		anchored := primary.ID
		primary.FleetPrimaryID = &anchored
	}
	if err := a.db.SetAccountFleet(ctx, member.ID, primary.ID); err != nil {
		return fleetBindOutcome{Status: http.StatusInternalServerError, Error: "绑定成员失败: " + err.Error()}
	}
	bound := primary.ID
	member.FleetPrimaryID = &bound
	return fleetBindOutcome{Notice: notice}
}

func (a *App) handleFleetBind(w http.ResponseWriter, r *http.Request) {
	var body fleetBindRequest
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	member, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	primary, ok := a.resolveAccountRef(w, r, body.PrimaryRef)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.RequestTimeout)
	defer cancel()
	if outcome := a.bindFleetMember(ctx, member, primary, body.Force); outcome.Error != "" {
		writeError(w, outcome.Status, outcome.Error)
		return
	}
	fresh, err := a.db.FleetMembers(ctx, primary.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"bound": member.ID, "primary": primary.ID, "members": fleetMembersOut(fresh)})
}

type fleetBindManyRequest struct {
	PrimaryRef string   `json:"primary_ref"`
	MemberRefs []string `json:"member_refs"`
	Force      bool     `json:"force"`
}

// maxFleetBindMany bounds one request; a fleet holds one account per channel.
const maxFleetBindMany = 16

// handleFleetBindMany binds several accounts into one fleet and reports every
// member on its own, so the console can show which one was refused and why
// (for example a different WeChat) while the others are bound.
func (a *App) handleFleetBindMany(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body fleetBindManyRequest
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if len(body.MemberRefs) == 0 || len(body.MemberRefs) > maxFleetBindMany {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("member_refs needs 1 to %d accounts", maxFleetBindMany))
		return
	}
	primary, ok := a.resolveAccountRef(w, r, body.PrimaryRef)
	if !ok {
		return
	}
	members := make([]*store.WechatAccount, 0, len(body.MemberRefs))
	seen := map[int64]bool{primary.ID: true}
	for _, ref := range body.MemberRefs {
		member, ok := a.resolveAccountRef(w, r, ref)
		if !ok {
			return
		}
		if seen[member.ID] {
			continue
		}
		seen[member.ID] = true
		members = append(members, member)
	}
	results := make([]map[string]any, 0, len(members))
	for _, member := range members {
		ctx, cancel := context.WithTimeout(r.Context(), a.cfg.RequestTimeout)
		outcome := a.bindFleetMember(ctx, member, primary, body.Force)
		cancel()
		entry := map[string]any{"id": member.ID, "bound": outcome.Error == ""}
		if outcome.Error != "" {
			entry["error"] = outcome.Error
		}
		if outcome.Notice != "" {
			entry["notice"] = outcome.Notice
		}
		results = append(results, entry)
	}
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.RequestTimeout)
	defer cancel()
	fresh, err := a.db.FleetMembers(ctx, primary.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"primary": primary.ID, "results": results, "members": fleetMembersOut(fresh)})
}

// handleFleetDissolve frees every member of the fleet the account belongs to,
// the primary last. Unbinding the primary first would leave its members
// pointing at it.
func (a *App) handleFleetDissolve(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body fleetUnbindRequest
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.RequestTimeout)
	defer cancel()
	primary := fleetPrimaryOf(acc)
	if primary <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"dissolved": []int64{}})
		return
	}
	members, err := a.db.FleetMembers(ctx, primary)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	// A fleet bound before accounts of different owners were refused may hold
	// accounts the caller cannot see; splitting it is then up to an administrator.
	for _, member := range members {
		if !a.accountVisible(r, member.ID) {
			writeError(w, http.StatusForbidden, "多渠道账号里有不属于你的账号，请联系管理员拆分")
			return
		}
	}
	freed := make([]int64, 0, len(members))
	for _, member := range members {
		if member.ID == primary {
			continue
		}
		if err := a.db.SetAccountFleet(ctx, member.ID, 0); err != nil {
			writeError(w, http.StatusInternalServerError, "解散失败: "+err.Error())
			return
		}
		freed = append(freed, member.ID)
	}
	if err := a.db.SetAccountFleet(ctx, primary, 0); err != nil {
		writeError(w, http.StatusInternalServerError, "解散失败: "+err.Error())
		return
	}
	freed = append(freed, primary)
	writeJSON(w, http.StatusOK, map[string]any{"dissolved": freed})
}

func (a *App) handleFleetUnbind(w http.ResponseWriter, r *http.Request) {
	var body fleetUnbindRequest
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.RequestTimeout)
	defer cancel()
	if err := a.db.SetAccountFleet(ctx, acc.ID, 0); err != nil {
		writeError(w, http.StatusInternalServerError, "解绑失败: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"unbound": acc.ID})
}

func (a *App) handleFleetStatus(w http.ResponseWriter, r *http.Request) {
	acc, ok := a.resolveAccountRef(w, r, r.URL.Query().Get("ref"))
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.RequestTimeout)
	defer cancel()
	primary := fleetPrimaryOf(acc)
	if primary <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"fleet": false, "account_id": acc.ID})
		return
	}
	members, err := a.db.FleetMembers(ctx, primary)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"fleet": true, "primary": primary, "members": fleetMembersOut(members)})
}

func fleetMembersOut(members []*store.WechatAccount) []map[string]any {
	out := make([]map[string]any, 0, len(members))
	for _, member := range members {
		public := member.Public()
		out = append(out, map[string]any{
			"id":           member.ID,
			"alias":        member.Alias,
			"nickname":     member.Nickname,
			"scan_product": public.ScanProduct,
			"status":       member.Status,
		})
	}
	return out
}

// fleetUnionIDMatch compares the two accounts' WeChat unionid. Missing
// unionids are allowed through with a notice; a confirmed mismatch is not.
func (a *App) fleetUnionIDMatch(ctx context.Context, member, primary *store.WechatAccount) (bool, string, error) {
	memberUID, err := a.fleetUnionIDOf(ctx, member)
	if err != nil {
		return false, "", fmt.Errorf("账号 %d: %w", member.ID, err)
	}
	primaryUID, err := a.fleetUnionIDOf(ctx, primary)
	if err != nil {
		return false, "", fmt.Errorf("账号 %d: %w", primary.ID, err)
	}
	switch {
	case memberUID == "" && primaryUID == "":
		return true, "双方 unionid 均不可得，请自行确认是同一微信", nil
	case memberUID == "" || primaryUID == "":
		return true, fmt.Sprintf("一方 unionid 缺失（%q vs %q），请自行确认是同一微信", memberUID, primaryUID), nil
	case memberUID == primaryUID:
		return true, "", nil
	default:
		return false, fmt.Sprintf("unionid 不一致，不是同一微信（%s… vs %s…）", clipUID(memberUID), clipUID(primaryUID)), nil
	}
}

func clipUID(uid string) string {
	if len(uid) > 8 {
		return uid[:8]
	}
	return uid
}

func (a *App) fleetUnionIDOf(ctx context.Context, acc *store.WechatAccount) (string, error) {
	if cached := fleetUnionIDFromMap(acc.UserInfo); cached != "" {
		return cached, nil
	}
	creds := protocol.CredentialsFromMap(acc.Credentials)
	if creds.AccessToken == "" && creds.PCMgr.LoginKey == "" {
		return "", nil
	}
	info, err := a.fetchUserInfo(ctx, creds)
	if err != nil {
		return "", err
	}
	return fleetUnionIDFromMap(info), nil
}

func fleetUnionIDFromMap(info map[string]any) string {
	if info == nil {
		return ""
	}
	for _, key := range []string{"unionid", "unionId", "union_id"} {
		if s, ok := info[key].(string); ok && strings.TrimSpace(s) != "" {
			return strings.TrimSpace(s)
		}
	}
	if nested, ok := info["user_info"].(map[string]any); ok {
		return fleetUnionIDFromMap(nested)
	}
	return ""
}
