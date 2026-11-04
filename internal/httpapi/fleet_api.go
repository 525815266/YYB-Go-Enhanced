package httpapi

import (
	"context"
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
	if member.ID == primary.ID {
		writeError(w, http.StatusUnprocessableEntity, "成员与主账号是同一个账号")
		return
	}
	if fleetPrimaryOf(member) > 0 && fleetPrimaryOf(member) != primary.ID {
		writeError(w, http.StatusUnprocessableEntity, fmt.Sprintf("账号 %d 已绑定其他编队（主账号 %d），请先解绑", member.ID, fleetPrimaryOf(member)))
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.RequestTimeout)
	defer cancel()
	if !body.Force {
		match, detail, err := a.fleetUnionIDMatch(ctx, member, primary)
		if err != nil {
			writeError(w, http.StatusBadGateway, "读取账号资料失败: "+err.Error())
			return
		}
		if !match {
			writeError(w, http.StatusUnprocessableEntity, detail)
			return
		}
	}
	// The primary anchors the fleet: it points at itself so member queries
	// and dispatch both resolve through one id.
	if fleetPrimaryOf(primary) == 0 {
		if err := a.db.SetAccountFleet(ctx, primary.ID, primary.ID); err != nil {
			writeError(w, http.StatusInternalServerError, "绑定主账号失败: "+err.Error())
			return
		}
	}
	if err := a.db.SetAccountFleet(ctx, member.ID, primary.ID); err != nil {
		writeError(w, http.StatusInternalServerError, "绑定成员失败: "+err.Error())
		return
	}
	fresh, err := a.db.FleetMembers(ctx, primary.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"bound": member.ID, "primary": primary.ID, "members": fleetMembersOut(fresh)})
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
