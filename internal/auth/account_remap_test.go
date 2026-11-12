package auth

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
)

func TestRemapAccountIDsRemovesOnlyOrphansAndPreservesOwners(t *testing.T) {
	ctx := context.Background()
	s, err := Open(ctx, "sqlite", filepath.Join(t.TempDir(), "auth.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	first, err := s.CreateUser(ctx, "first", "First", "test-password", "admin")
	if err != nil {
		t.Fatal(err)
	}
	second, err := s.CreateUser(ctx, "second", "Second", "test-password", "user")
	if err != nil {
		t.Fatal(err)
	}
	for id, owner := range map[int64]int64{1: first.ID, 2: second.ID, 3: first.ID, 8: second.ID, 99: first.ID, -2: first.ID} {
		if err = s.ClaimAccount(ctx, id, owner); err != nil {
			t.Fatal(err)
		}
	}
	// 1, 99 and -2 are stale ownership; 7 is a live, unowned account.
	if err = s.RemapAccountIDs(ctx, map[int64]int64{2: 1, 3: 2, 7: 3}, []int64{2, 3, 7, 8}); err != nil {
		t.Fatal(err)
	}
	for id, want := range map[int64]int64{1: second.ID, 2: first.ID, 8: second.ID} {
		if got, err := s.AccountOwner(ctx, id); err != nil || got != want {
			t.Fatalf("owner of %d = %d, %v; want %d", id, got, err, want)
		}
	}
	for _, id := range []int64{-2, -3, -7, 3, 7, 99} {
		if _, err = s.AccountOwner(ctx, id); !errors.Is(err, sql.ErrNoRows) {
			t.Fatalf("unexpected owner of %d: %v", id, err)
		}
	}
	// Also support the inverse map used for rollback, including occupied IDs.
	if err = s.RemapAccountIDs(ctx, map[int64]int64{1: 2, 2: 3, 3: 7}, []int64{1, 2, 3, 8}); err != nil {
		t.Fatal(err)
	}
	if got, err := s.AccountOwner(ctx, 2); err != nil || got != second.ID {
		t.Fatal("inverse map did not restore owner", got, err)
	}
}

func TestRemapAccountIDsRejectsInvalidMappingWithoutChangingOwners(t *testing.T) {
	ctx := context.Background()
	s, err := Open(ctx, "sqlite", filepath.Join(t.TempDir(), "auth.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	u, err := s.CreateUser(ctx, "owner", "Owner", "test-password", "admin")
	if err != nil {
		t.Fatal(err)
	}
	if err = s.ClaimAccount(ctx, 1, u.ID); err != nil {
		t.Fatal(err)
	}
	for _, mapping := range []map[int64]int64{{2: 1}, {2: 0}, {3: 1}, {1: 3, 2: 3}} {
		if err = s.RemapAccountIDs(ctx, mapping, []int64{1, 2}); err == nil {
			t.Fatalf("invalid mapping accepted: %v", mapping)
		}
		if got, err := s.AccountOwner(ctx, 1); err != nil || got != u.ID {
			t.Fatal("rejected mapping changed owner", got, err)
		}
	}
}

func TestRemapAccountIDsFailureRollsBackOrphanCleanup(t *testing.T) {
	ctx := context.Background()
	s, err := Open(ctx, "sqlite", filepath.Join(t.TempDir(), "auth.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	u, err := s.CreateUser(ctx, "owner", "Owner", "test-password", "admin")
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []int64{1, 2} {
		if err = s.ClaimAccount(ctx, id, u.ID); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = s.db.ExecContext(ctx, `CREATE TRIGGER fail_remap BEFORE UPDATE ON account_owners BEGIN SELECT RAISE(ABORT, 'test failure'); END`); err != nil {
		t.Fatal(err)
	}
	if err = s.RemapAccountIDs(ctx, map[int64]int64{2: 1}, []int64{2}); err == nil {
		t.Fatal("expected update failure")
	}
	for _, id := range []int64{1, 2} {
		if got, err := s.AccountOwner(ctx, id); err != nil || got != u.ID {
			t.Fatal("failed transaction changed owner", id, got, err)
		}
	}
}
