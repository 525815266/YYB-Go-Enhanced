package httpapi

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"reflect"
	"testing"

	"yyb_go/internal/auth"
	"yyb_go/internal/store"
)

func TestAccountEnumerationSelectsOneUsableAuthorizationPerFleet(t *testing.T) {
	root := int64(2)
	account := func(id int64, status string) *store.WechatAccount {
		return &store.WechatAccount{ID: id, FleetPrimaryID: &root, Status: &status}
	}
	for _, tc := range []struct {
		name    string
		members []*store.WechatAccount
		want    int64
	}{
		{"healthy primary preferred", []*store.WechatAccount{account(1, "alive"), account(2, "alive"), account(3, "alive")}, 2},
		{"expired primary falls back", []*store.WechatAccount{account(2, "expired"), account(3, "alive")}, 3},
		{"saved session before unverified", []*store.WechatAccount{account(2, "unknown"), account(3, "session_only")}, 3},
		{"alive before saved session", []*store.WechatAccount{account(2, "session_only"), account(3, "alive")}, 3},
		{"all offline retains primary", []*store.WechatAccount{account(1, "expired"), account(2, "expired")}, 2},
		{"unseen primary not fetched", []*store.WechatAccount{account(4, "expired"), account(3, "alive")}, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := accountRepresentatives(tc.members)
			if len(got) != 1 || got[0].ID != tc.want {
				t.Fatalf("representatives=%v, want ID %d", got, tc.want)
			}
			if got[0].FleetPrimaryID == nil || *got[0].FleetPrimaryID != root {
				t.Fatal("returned authorization lost its original primary reference")
			}
		})
	}
	// Unbound records remain independent even when their profile unionid matches.
	one, two := account(5, "alive"), account(6, "alive")
	one.FleetPrimaryID, two.FleetPrimaryID = nil, nil
	one.UserInfo, two.UserInfo = map[string]any{"unionid": "same"}, map[string]any{"unionid": "same"}
	if got := accountRepresentatives([]*store.WechatAccount{one, two}); len(got) != 2 {
		t.Fatal("list inferred an unapproved binding")
	}
}

func TestAccountEnumerationOptInAndOwnerFiltering(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	owner, err := app.auth.CreateUser(ctx, "list-owner", "Owner", "password-for-test", "user")
	if err != nil {
		t.Fatal(err)
	}
	other, err := app.auth.CreateUser(ctx, "list-other", "Other", "password-for-test", "user")
	if err != nil {
		t.Fatal(err)
	}
	var ids []int64
	for i, status := range []string{"expired", "alive", "alive"} {
		acc, err := app.db.UpsertAccount(ctx, "list-"+string(rune('a'+i)), "buffer", nil, nil, nil, nil, nil, &status)
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, acc.ID)
		userID := owner.ID
		if i == 2 {
			userID = other.ID
		}
		if err = app.auth.ClaimAccount(ctx, acc.ID, userID); err != nil {
			t.Fatal(err)
		}
		if err = app.db.SetAccountFleet(ctx, acc.ID, ids[0]); err != nil {
			t.Fatal(err)
		}
	}
	for _, tc := range []struct {
		query  string
		user   *auth.User
		want   []int64
		status int
	}{
		{"", owner, []int64{ids[1]}, 200},
		{"?include_members=1", owner, ids[:2], 200},
		{"?include_members=false", owner, []int64{ids[1]}, 200},
		{"?include_members=true", other, ids[2:], 200},
		{"?include_members=invalid", owner, nil, 400},
	} {
		r := httptest.NewRequest("GET", "/accounts"+tc.query, nil)
		r = r.WithContext(context.WithValue(r.Context(), authUserKey, tc.user))
		w := httptest.NewRecorder()
		app.handleAccountsRoot(w, r)
		if w.Code != tc.status {
			t.Fatalf("%s: %d %s", tc.query, w.Code, w.Body.String())
		}
		if w.Code != 200 {
			continue
		}
		var reply struct{ Data []store.AccountPublic }
		if err = json.Unmarshal(w.Body.Bytes(), &reply); err != nil {
			t.Fatal(err)
		}
		got := make([]int64, 0, len(reply.Data))
		for _, a := range reply.Data {
			got = append(got, a.ID)
		}
		if !reflect.DeepEqual(got, tc.want) {
			t.Fatalf("%s: got %v want %v", tc.query, got, tc.want)
		}
	}
}

func TestIntegrationAccountEnumerationCount(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), IntegrationToken: "list-test-token"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	var root int64
	for i, status := range []string{"expired", "alive"} {
		acc, err := app.db.UpsertAccount(ctx, "integration-list-"+string(rune('a'+i)), "buffer", nil, nil, nil, nil, nil, &status)
		if err != nil {
			t.Fatal(err)
		}
		if i == 0 {
			root = acc.ID
		}
		if err = app.db.SetAccountFleet(ctx, acc.ID, root); err != nil {
			t.Fatal(err)
		}
	}
	for _, tc := range []struct {
		query string
		want  int
	}{
		{"", 1},
		{"?include_members=1", 2},
	} {
		r := httptest.NewRequest("GET", "/integration/accounts"+tc.query, nil)
		r.Header.Set("Authorization", "Bearer list-test-token")
		w := httptest.NewRecorder()
		app.handleIntegrationAccounts(w, r)
		if w.Code != 200 {
			t.Fatalf("%s: %d %s", tc.query, w.Code, w.Body.String())
		}
		var reply struct {
			Data struct {
				Items []struct {
					ID     int64
					Status string
				}
				Count int
			}
		}
		if err = json.Unmarshal(w.Body.Bytes(), &reply); err != nil {
			t.Fatal(err)
		}
		if len(reply.Data.Items) != tc.want || reply.Data.Count != tc.want {
			t.Fatalf("%s: items=%d count=%d want %d", tc.query, len(reply.Data.Items), reply.Data.Count, tc.want)
		}
		if tc.want == 1 && (reply.Data.Items[0].ID == root || reply.Data.Items[0].Status != "alive") {
			t.Fatal("integration did not return the usable member's real ID")
		}
	}
}
