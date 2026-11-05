package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/auth"
	"yyb_go/internal/protocol"
	"yyb_go/internal/qr"
	"yyb_go/internal/store"
)

type bindManyReply struct {
	Data struct {
		Primary int64 `json:"primary"`
		Results []struct {
			ID     int64  `json:"id"`
			Bound  bool   `json:"bound"`
			Notice string `json:"notice"`
			Error  string `json:"error"`
		} `json:"results"`
		Members []struct {
			ID int64 `json:"id"`
		} `json:"members"`
	} `json:"data"`
}

func postBindMany(t *testing.T, app *App, body string, user *auth.User) (*httptest.ResponseRecorder, bindManyReply) {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, "/api/fleet/bind-many", strings.NewReader(body))
	if user != nil {
		request = request.WithContext(context.WithValue(request.Context(), authUserKey, user))
	}
	out := httptest.NewRecorder()
	app.handleFleetBindMany(out, request)
	var reply bindManyReply
	_ = json.Unmarshal(out.Body.Bytes(), &reply)
	return out, reply
}

func groupTestAccounts(t *testing.T, unionIDs ...string) (*App, []*store.WechatAccount) {
	t.Helper()
	app := newKeepAliveTestApp(t)
	t.Cleanup(func() { app.Close() })
	// A missing unionid must never reach the real user info endpoint in a test.
	app.fetchUserInfo = func(context.Context, protocol.LoginBufferCredentials) (map[string]any, error) {
		return map[string]any{}, nil
	}
	accounts := make([]*store.WechatAccount, 0, len(unionIDs))
	for i, union := range unionIDs {
		acc := insertKeepAliveTestAccount(t, app, "group-"+string(rune('a'+i)), time.Now().Add(time.Hour))
		if union != "" {
			if err := app.db.SetAccountProfile(context.Background(), acc.ID, nil, nil, map[string]any{"unionid": union}); err != nil {
				t.Fatal(err)
			}
		}
		accounts = append(accounts, acc)
	}
	return app, accounts
}

func TestFleetBindManyReportsEveryMemberOnItsOwn(t *testing.T) {
	app, acc := groupTestAccounts(t, "union-one", "union-one", "union-two")
	body := `{"primary_ref":"` + itoa64(acc[0].ID) + `","member_refs":["` + itoa64(acc[1].ID) + `","` + itoa64(acc[2].ID) + `"]}`
	out, reply := postBindMany(t, app, body, nil)
	if out.Code != http.StatusOK || len(reply.Data.Results) != 2 {
		t.Fatalf("bind-many HTTP %d: %s", out.Code, out.Body.String())
	}
	if !reply.Data.Results[0].Bound || reply.Data.Results[0].Error != "" {
		t.Fatalf("the same WeChat was not bound: %+v", reply.Data.Results[0])
	}
	if reply.Data.Results[1].Bound || !strings.Contains(reply.Data.Results[1].Error, "unionid") {
		t.Fatalf("a different WeChat must be refused with the reason: %+v", reply.Data.Results[1])
	}
	if len(reply.Data.Members) != 2 {
		t.Fatalf("the fleet should hold the primary and the one bound member, got %d", len(reply.Data.Members))
	}
	// Forcing is an explicit decision of the operator.
	out, reply = postBindMany(t, app, `{"primary_ref":"`+itoa64(acc[0].ID)+`","member_refs":["`+itoa64(acc[2].ID)+`"],"force":true}`, nil)
	if out.Code != http.StatusOK || !reply.Data.Results[0].Bound || len(reply.Data.Members) != 3 {
		t.Fatalf("forced bind: %d %s", out.Code, out.Body.String())
	}
	// Dissolving frees every account, the primary included.
	dissolve := httptest.NewRecorder()
	app.handleFleetDissolve(dissolve, httptest.NewRequest(http.MethodPost, "/api/fleet/dissolve", strings.NewReader(`{"ref":"`+itoa64(acc[1].ID)+`"}`)))
	if dissolve.Code != http.StatusOK {
		t.Fatalf("dissolve HTTP %d: %s", dissolve.Code, dissolve.Body.String())
	}
	for _, member := range acc {
		fresh, err := app.db.GetAccount(context.Background(), member.ID)
		if err != nil || fresh.FleetPrimaryID != nil {
			t.Fatalf("account %d is still in a fleet: %v %v", member.ID, fresh.FleetPrimaryID, err)
		}
	}
}

func TestFleetBindManyNoticesWhenTheUnionIDCannotBeChecked(t *testing.T) {
	app, acc := groupTestAccounts(t, "", "")
	out, reply := postBindMany(t, app, `{"primary_ref":"`+itoa64(acc[0].ID)+`","member_refs":["`+itoa64(acc[1].ID)+`"]}`, nil)
	if out.Code != http.StatusOK || !reply.Data.Results[0].Bound || reply.Data.Results[0].Notice == "" {
		t.Fatalf("a missing unionid should bind with a notice asking the operator to confirm: %d %s", out.Code, out.Body.String())
	}
}

func TestFleetBindManyRejectsBadRequests(t *testing.T) {
	app, acc := groupTestAccounts(t, "u", "u")
	for name, body := range map[string]string{
		"no members":     `{"primary_ref":"` + itoa64(acc[0].ID) + `","member_refs":[]}`,
		"unknown member": `{"primary_ref":"` + itoa64(acc[0].ID) + `","member_refs":["99999"]}`,
		"unknown prime":  `{"primary_ref":"99999","member_refs":["` + itoa64(acc[1].ID) + `"]}`,
	} {
		if out, _ := postBindMany(t, app, body, nil); out.Code == http.StatusOK {
			t.Fatalf("%s was accepted: %s", name, out.Body.String())
		}
	}
	// The primary is never its own member.
	out, reply := postBindMany(t, app, `{"primary_ref":"`+itoa64(acc[0].ID)+`","member_refs":["`+itoa64(acc[0].ID)+`","`+itoa64(acc[1].ID)+`"]}`, nil)
	if out.Code != http.StatusOK || len(reply.Data.Results) != 1 {
		t.Fatalf("the primary listed as a member should be skipped: %d %s", out.Code, out.Body.String())
	}
	get := httptest.NewRecorder()
	app.handleFleetBindMany(get, httptest.NewRequest(http.MethodGet, "/api/fleet/bind-many", nil))
	if get.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET answered %d", get.Code)
	}
}

// Accounts of two owners must never end up in one fleet: dispatch would hand
// one user's requests to the other user's account.
func TestFleetBindingRefusesAccountsOfAnotherOwner(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite", RequestTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	app.fetchUserInfo = func(context.Context, protocol.LoginBufferCredentials) (map[string]any, error) {
		return map[string]any{}, nil
	}
	alice, _ := app.auth.CreateUser(ctx, "alice", "Alice", "local-alice-password", "user")
	bob, _ := app.auth.CreateUser(ctx, "bob", "Bob", "local-bob-password", "user")
	first, _ := app.db.UpsertAccount(ctx, "owned-by-alice", "buffer", nil, nil, nil, nil, nil, nil)
	second, _ := app.db.UpsertAccount(ctx, "also-alice", "buffer", nil, nil, nil, nil, nil, nil)
	third, _ := app.db.UpsertAccount(ctx, "owned-by-bob", "buffer", nil, nil, nil, nil, nil, nil)
	for _, claim := range []struct {
		acc  *store.WechatAccount
		user int64
	}{{first, alice.ID}, {second, alice.ID}, {third, bob.ID}} {
		if err := app.auth.ClaimAccount(ctx, claim.acc.ID, claim.user); err != nil {
			t.Fatal(err)
		}
	}
	admin := &auth.User{ID: 99, Role: "admin"}
	body := `{"primary_ref":"` + itoa64(first.ID) + `","member_refs":["` + itoa64(second.ID) + `","` + itoa64(third.ID) + `"]}`
	out, reply := postBindMany(t, app, body, admin)
	if out.Code != http.StatusOK || len(reply.Data.Results) != 2 {
		t.Fatalf("bind-many HTTP %d: %s", out.Code, out.Body.String())
	}
	if !reply.Data.Results[0].Bound {
		t.Fatalf("an account of the same owner should bind: %+v", reply.Data.Results[0])
	}
	if reply.Data.Results[1].Bound || !strings.Contains(reply.Data.Results[1].Error, "归属") {
		t.Fatalf("an account of another owner must be refused: %+v", reply.Data.Results[1])
	}
	// The single bind endpoint applies the same rule.
	single := httptest.NewRequest(http.MethodPost, "/api/fleet", strings.NewReader(`{"ref":"`+itoa64(third.ID)+`","primary_ref":"`+itoa64(first.ID)+`","force":true}`))
	single = single.WithContext(context.WithValue(single.Context(), authUserKey, admin))
	rec := httptest.NewRecorder()
	app.handleFleetAPI(rec, single)
	if rec.Code != http.StatusConflict {
		t.Fatalf("single bind across owners answered %d: %s", rec.Code, rec.Body.String())
	}
}

// A fleet that mixes owners (possible before binding checked them) is only split
// by an administrator: an ordinary user must not free someone else's account.
func TestDissolveLeavesAFleetWithForeignAccountsToAnAdministrator(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite", RequestTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	alice, _ := app.auth.CreateUser(ctx, "alice", "Alice", "local-alice-password", "user")
	bob, _ := app.auth.CreateUser(ctx, "bob", "Bob", "local-bob-password", "user")
	mine, _ := app.db.UpsertAccount(ctx, "owned-by-bob", "buffer", nil, nil, nil, nil, nil, nil)
	theirs, _ := app.db.UpsertAccount(ctx, "owned-by-alice", "buffer", nil, nil, nil, nil, nil, nil)
	if err := app.auth.ClaimAccount(ctx, mine.ID, bob.ID); err != nil {
		t.Fatal(err)
	}
	if err := app.auth.ClaimAccount(ctx, theirs.ID, alice.ID); err != nil {
		t.Fatal(err)
	}
	for _, id := range []int64{mine.ID, theirs.ID} {
		if err := app.db.SetAccountFleet(ctx, id, mine.ID); err != nil {
			t.Fatal(err)
		}
	}
	dissolve := func(user *auth.User) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/api/fleet/dissolve", strings.NewReader(`{"ref":"`+itoa64(mine.ID)+`"}`))
		request = request.WithContext(context.WithValue(request.Context(), authUserKey, user))
		out := httptest.NewRecorder()
		app.handleFleetDissolve(out, request)
		return out
	}
	if out := dissolve(&auth.User{ID: bob.ID, Role: "user"}); out.Code != http.StatusForbidden {
		t.Fatalf("an ordinary user dissolved a mixed fleet: %d %s", out.Code, out.Body.String())
	}
	for _, id := range []int64{mine.ID, theirs.ID} {
		fresh, err := app.db.GetAccount(ctx, id)
		if err != nil || fresh.FleetPrimaryID == nil {
			t.Fatalf("account %d left its fleet: %v %v", id, fresh.FleetPrimaryID, err)
		}
	}
	if out := dissolve(&auth.User{ID: 99, Role: "admin"}); out.Code != http.StatusOK {
		t.Fatalf("the administrator could not dissolve it: %d %s", out.Code, out.Body.String())
	}
	for _, id := range []int64{mine.ID, theirs.ID} {
		fresh, err := app.db.GetAccount(ctx, id)
		if err != nil || fresh.FleetPrimaryID != nil {
			t.Fatalf("account %d is still in a fleet: %v %v", id, fresh.FleetPrimaryID, err)
		}
	}
}

func TestScanChannelsEndpointFollowsTheRegistry(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	out := httptest.NewRecorder()
	app.handleScanChannels(out, httptest.NewRequest(http.MethodGet, "/api/scan-channels", nil))
	if out.Code != http.StatusOK {
		t.Fatalf("HTTP %d: %s", out.Code, out.Body.String())
	}
	var reply struct {
		Data struct {
			Channels []qr.ScanChannel `json:"channels"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &reply); err != nil {
		t.Fatal(err)
	}
	want := qr.ScanChannels()
	if len(reply.Data.Channels) != len(want) {
		t.Fatalf("got %d channels, want %d", len(reply.Data.Channels), len(want))
	}
	for i, channel := range reply.Data.Channels {
		if channel != want[i] {
			t.Fatalf("channel %d = %+v, want %+v", i, channel, want[i])
		}
	}
	post := httptest.NewRecorder()
	app.handleScanChannels(post, httptest.NewRequest(http.MethodPost, "/api/scan-channels", nil))
	if post.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST answered %d", post.Code)
	}
}

// The scan endpoints accept exactly the channels the registry lists.
func TestLoginProductFollowsTheRegistry(t *testing.T) {
	for _, channel := range qr.ScanChannels() {
		got, err := normalizeLoginProduct(strings.ToUpper(" " + channel.ID + " "))
		if err != nil || got != channel.ID {
			t.Fatalf("channel %q: got %q, %v", channel.ID, got, err)
		}
	}
	if got, err := normalizeLoginProduct(""); err != nil || got != "appstore" {
		t.Fatalf("the empty product should mean appstore: %q, %v", got, err)
	}
	if _, err := normalizeLoginProduct("wechat-pad"); err == nil {
		t.Fatal("an unlisted channel was accepted")
	}
}
