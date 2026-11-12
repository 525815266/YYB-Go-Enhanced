package httpapi

import (
	"context"
	"database/sql"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAccountCompactWithLegacyOrphanOwner(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	admin, err := app.auth.CreateUser(ctx, "admin", "Admin", "test-password", "admin")
	if err != nil {
		t.Fatal(err)
	}
	member, err := app.auth.CreateUser(ctx, "member", "Member", "test-password", "user")
	if err != nil {
		t.Fatal(err)
	}
	for _, openID := range []string{"deleted", "member-account", "admin-account"} {
		acc, err := app.db.UpsertAccount(ctx, openID, "test-buffer", nil, nil, nil, nil, nil, nil)
		if err != nil {
			t.Fatal(err)
		}
		owner := admin.ID
		if acc.ID == 2 {
			owner = member.ID
		}
		if err = app.auth.ClaimAccount(ctx, acc.ID, owner); err != nil {
			t.Fatal(err)
		}
	}
	// Reproduce historical deletion that left account_owners.account_id=1.
	if err = app.db.DeleteAccount(ctx, 1); err != nil {
		t.Fatal(err)
	}
	request := func(body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "/accounts/compact", strings.NewReader(body))
		r = r.WithContext(context.WithValue(ctx, authUserKey, admin))
		w := httptest.NewRecorder()
		app.handleAccountCompact(w, r)
		return w
	}
	if w := request(`{"confirm":false}`); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	if owner, err := app.auth.AccountOwner(ctx, 1); err != nil || owner != admin.ID {
		t.Fatal("preview changed ownership", owner, err)
	}
	if w := request(`{"confirm":true}`); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	for id, want := range map[int64]struct {
		openID string
		owner  int64
	}{1: {"member-account", member.ID}, 2: {"admin-account", admin.ID}} {
		acc, err := app.db.GetAccount(ctx, id)
		if err != nil || acc.OpenID != want.openID {
			t.Fatal("account identity changed", id, err)
		}
		if owner, err := app.auth.AccountOwner(ctx, id); err != nil || owner != want.owner {
			t.Fatal("account owner changed", id, owner, err)
		}
	}
	if _, err = app.auth.AccountOwner(ctx, 3); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("old owner remains", err)
	}
}

func TestDeleteAccountCleansOwnerBeforeIDReuse(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	u, err := app.auth.CreateUser(ctx, "owner", "Owner", "test-password", "user")
	if err != nil {
		t.Fatal(err)
	}
	acc, err := app.db.UpsertAccount(ctx, "old-account", "test-buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = app.auth.ClaimAccount(ctx, acc.ID, u.ID); err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest("DELETE", "/accounts?ref=1", nil)
	r = r.WithContext(context.WithValue(ctx, authUserKey, u))
	w := httptest.NewRecorder()
	app.handleAccountsRoot(w, r)
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	if _, err = app.auth.AccountOwner(ctx, acc.ID); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("deleted account retained owner", err)
	}
	if _, err = app.db.UpsertAccount(ctx, "new-account", "new-buffer", nil, nil, nil, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	ids, err := app.auth.AccountIDs(ctx, u.ID)
	if err != nil || len(ids) != 0 {
		t.Fatal("new account inherited deleted ownership", ids, err)
	}
}

func TestDeleteAccountRestoresOwnerWhenAccountDatabaseFails(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	u, err := app.auth.CreateUser(ctx, "owner", "Owner", "test-password", "user")
	if err != nil {
		t.Fatal(err)
	}
	if err = app.auth.ClaimAccount(ctx, 1, u.ID); err != nil {
		t.Fatal(err)
	}
	if err = app.db.Close(); err != nil {
		t.Fatal(err)
	}
	if err = app.deleteAccountWithOwner(ctx, 1); err == nil {
		t.Fatal("expected database failure")
	}
	if owner, err := app.auth.AccountOwner(ctx, 1); err != nil || owner != u.ID {
		t.Fatal("failed deletion lost owner", owner, err)
	}
}
