package auth

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSQLiteAuthenticationLifecycle(t *testing.T) {
	ctx := context.Background()
	store, err := Open(ctx, "sqlite", filepath.Join(t.TempDir(), "auth.db"))
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	defer store.Close()

	if err := store.BootstrapAdmin(ctx, "", ""); err != nil {
		t.Fatalf("BootstrapAdmin() without credentials error = %v", err)
	}
	if err := store.BootstrapAdmin(ctx, "admin", ""); err != nil {
		t.Fatalf("BootstrapAdmin() with blank password error = %v", err)
	}
	admin, err := store.RegisterUser(ctx, "owner", "Owner", "owner-password")
	if err != nil {
		t.Fatalf("RegisterUser(first) error = %v", err)
	}
	if admin.Role != "admin" {
		t.Fatalf("first registered role = %q, want admin", admin.Role)
	}
	user, err := store.RegisterUser(ctx, "member", "Member", "member-password")
	if err != nil {
		t.Fatalf("RegisterUser(second) error = %v", err)
	}
	if user.Role != "user" {
		t.Fatalf("second registered role = %q, want user", user.Role)
	}
	if _, err := store.RegisterUser(ctx, "member", "Duplicate", "another-password"); err == nil || !strings.Contains(err.Error(), "用户名已存在") {
		t.Fatalf("duplicate RegisterUser() error = %v", err)
	}

	authenticated, err := store.Authenticate(ctx, "OWNER", "owner-password")
	if err != nil {
		t.Fatalf("Authenticate() error = %v", err)
	}
	token, session, err := store.CreateSession(ctx, authenticated.ID, "test-agent", "127.0.0.1", time.Hour)
	if err != nil {
		t.Fatalf("CreateSession() error = %v", err)
	}
	gotUser, gotSession, err := store.UserBySession(ctx, token)
	if err != nil {
		t.Fatalf("UserBySession() error = %v", err)
	}
	if gotUser.ID != admin.ID || gotSession.ID != session.ID {
		t.Fatalf("UserBySession() = user %d session %d, want user %d session %d", gotUser.ID, gotSession.ID, admin.ID, session.ID)
	}
	if err := store.DeleteSession(ctx, token); err != nil {
		t.Fatalf("DeleteSession() error = %v", err)
	}
	if _, _, err := store.UserBySession(ctx, token); err == nil {
		t.Fatal("deleted session remains usable")
	}

	if err := store.SetRegistrationEnabled(ctx, false); err != nil {
		t.Fatalf("SetRegistrationEnabled(false) error = %v", err)
	}
	enabled, err := store.RegistrationEnabled(ctx)
	if err != nil || enabled {
		t.Fatalf("RegistrationEnabled() = %v, %v; want false, nil", enabled, err)
	}
	if err := store.SetRegistrationEnabled(ctx, true); err != nil {
		t.Fatalf("SetRegistrationEnabled(true) error = %v", err)
	}
	enabled, err = store.RegistrationEnabled(ctx)
	if err != nil || !enabled {
		t.Fatalf("RegistrationEnabled() = %v, %v; want true, nil", enabled, err)
	}
}

func TestSQLiteBootstrapAdmin(t *testing.T) {
	ctx := context.Background()
	store, err := Open(ctx, "sqlite", filepath.Join(t.TempDir(), "auth.db"))
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	defer store.Close()

	if err := store.BootstrapAdmin(ctx, "admin", "bootstrap-password"); err != nil {
		t.Fatalf("BootstrapAdmin() error = %v", err)
	}
	user, err := store.Authenticate(ctx, "admin", "bootstrap-password")
	if err != nil {
		t.Fatalf("Authenticate() error = %v", err)
	}
	if user.Role != "admin" {
		t.Fatalf("bootstrap role = %q, want admin", user.Role)
	}
}

func TestRecoverAdminPasswordPreservesOtherDataAndRevokesOnlyTargetSessions(t *testing.T) {
	ctx := context.Background()
	store, err := Open(ctx, "sqlite", filepath.Join(t.TempDir(), "auth.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	admin, err := store.RegisterUser(ctx, "owner", "Owner", "old-password")
	if err != nil {
		t.Fatal(err)
	}
	member, err := store.RegisterUser(ctx, "member", "Member", "member-password")
	if err != nil {
		t.Fatal(err)
	}
	adminToken, _, err := store.CreateSession(ctx, admin.ID, "test", "127.0.0.1", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	memberToken, _, err := store.CreateSession(ctx, member.ID, "test", "127.0.0.1", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.ClaimAccount(ctx, 9, member.ID); err != nil {
		t.Fatal(err)
	}
	if err := store.SetRegistrationEnabled(ctx, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RecoverAdminPassword(ctx, "OWNER", "new-password"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Authenticate(ctx, "owner", "old-password"); err == nil {
		t.Fatal("old password still works")
	}
	if _, err := store.Authenticate(ctx, "owner", "new-password"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := store.UserBySession(ctx, adminToken); err == nil {
		t.Fatal("old administrator session still works")
	}
	if _, _, err := store.UserBySession(ctx, memberToken); err != nil {
		t.Fatalf("member session lost: %v", err)
	}
	if owner, err := store.AccountOwner(ctx, 9); err != nil || owner != member.ID {
		t.Fatalf("account ownership changed: %d, %v", owner, err)
	}
	if enabled, err := store.RegistrationEnabled(ctx); err != nil || enabled {
		t.Fatalf("registration setting changed: %v, %v", enabled, err)
	}
	for _, username := range []string{"member", "missing"} {
		if err := store.RecoverAdminPassword(ctx, username, "another-password"); err == nil {
			t.Fatalf("recovered non-admin %q", username)
		}
	}
	if _, err := store.Authenticate(ctx, "member", "member-password"); err != nil {
		t.Fatal("member password was modified")
	}
	if _, err := store.db.ExecContext(ctx, "UPDATE users SET enabled=FALSE WHERE id=?", admin.ID); err != nil {
		t.Fatal(err)
	}
	if err := store.RecoverAdminPassword(ctx, "owner", "another-password"); err == nil {
		t.Fatal("disabled admin was recovered")
	}
}

func TestRecoverAdminPasswordRollsBackWhenSessionDeletionFails(t *testing.T) {
	ctx := context.Background()
	store, err := Open(ctx, "sqlite", filepath.Join(t.TempDir(), "auth.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.BootstrapAdmin(ctx, "owner", "old-password"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.ExecContext(ctx, "DROP TABLE user_sessions"); err != nil {
		t.Fatal(err)
	}
	if err := store.RecoverAdminPassword(ctx, "owner", "new-password"); err == nil {
		t.Fatal("recovery succeeded without revoking sessions")
	}
	if _, err := store.Authenticate(ctx, "owner", "old-password"); err != nil {
		t.Fatalf("password was changed despite failed transaction: %v", err)
	}
}
