package store

import (
	"context"
	"path/filepath"
	"testing"
)

func fleetTestAccounts(t *testing.T, count int) (*DB, []*WechatAccount) {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "accounts.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	status := "alive"
	accounts := make([]*WechatAccount, 0, count)
	for i := 0; i < count; i++ {
		acc, err := db.UpsertAccount(context.Background(), "fleet-account-"+string(rune('a'+i)), "buffer", nil, nil, nil, nil, nil, &status)
		if err != nil {
			t.Fatal(err)
		}
		accounts = append(accounts, acc)
	}
	return db, accounts
}

func fleetPointer(t *testing.T, db *DB, id int64) int64 {
	t.Helper()
	acc, err := db.GetAccount(context.Background(), id)
	if err != nil {
		t.Fatalf("account %d: %v", id, err)
	}
	if acc.FleetPrimaryID == nil {
		return 0
	}
	return *acc.FleetPrimaryID
}

func bindFleet(t *testing.T, db *DB, primary int64, members ...int64) {
	t.Helper()
	ctx := context.Background()
	if err := db.SetAccountFleet(ctx, primary, primary); err != nil {
		t.Fatal(err)
	}
	for _, id := range members {
		if err := db.SetAccountFleet(ctx, id, primary); err != nil {
			t.Fatal(err)
		}
	}
}

// Compacting account IDs renumbers the rows, so the fleet pointers must move
// with them or a fleet would silently fall apart.
func TestCompactAccountIDsRemapsFleetPointers(t *testing.T) {
	db, acc := fleetTestAccounts(t, 5)
	ctx := context.Background()
	// Leave IDs 1, 3 and 5 so that compaction renumbers the fleet.
	for _, gone := range []int64{acc[1].ID, acc[3].ID} {
		if err := db.DeleteAccount(ctx, gone); err != nil {
			t.Fatal(err)
		}
	}
	bindFleet(t, db, acc[2].ID, acc[0].ID, acc[4].ID)
	mapping, err := db.CompactAccountIDs(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if mapping[acc[2].ID] != 2 || mapping[acc[4].ID] != 3 {
		t.Fatalf("unexpected mapping %v", mapping)
	}
	for id, want := range map[int64]int64{1: 2, 2: 2, 3: 2} {
		if got := fleetPointer(t, db, id); got != want {
			t.Fatalf("account %d points at %d after compaction, want %d", id, got, want)
		}
	}
	members, err := db.FleetMembers(ctx, 2)
	if err != nil || len(members) != 3 {
		t.Fatalf("fleet after compaction: %d members, %v", len(members), err)
	}
}

// Account IDs are reused after a delete. Members of a removed primary must not
// keep pointing at an ID that an unrelated new account may take.
func TestDeletingFleetPrimaryMovesItsMembers(t *testing.T) {
	db, acc := fleetTestAccounts(t, 3)
	ctx := context.Background()
	bindFleet(t, db, acc[0].ID, acc[1].ID, acc[2].ID)
	if err := db.DeleteAccount(ctx, acc[0].ID); err != nil {
		t.Fatal(err)
	}
	if fleetPointer(t, db, acc[1].ID) != acc[1].ID || fleetPointer(t, db, acc[2].ID) != acc[1].ID {
		t.Fatalf("members were not moved to the lowest remaining ID: %d, %d", fleetPointer(t, db, acc[1].ID), fleetPointer(t, db, acc[2].ID))
	}
	// One account is not a fleet.
	if err := db.DeleteAccount(ctx, acc[1].ID); err != nil {
		t.Fatal(err)
	}
	if got := fleetPointer(t, db, acc[2].ID); got != 0 {
		t.Fatalf("the last account is still bound to %d", got)
	}
}

func TestDeletingTheOnlyMemberDissolvesTheFleet(t *testing.T) {
	db, acc := fleetTestAccounts(t, 2)
	ctx := context.Background()
	bindFleet(t, db, acc[0].ID, acc[1].ID)
	if err := db.DeleteAccount(ctx, acc[1].ID); err != nil {
		t.Fatal(err)
	}
	if got := fleetPointer(t, db, acc[0].ID); got != 0 {
		t.Fatalf("the primary is still anchored to %d", got)
	}
}

// Removing a member of a larger fleet leaves the rest of it alone.
func TestDeletingOneMemberKeepsTheOthersTogether(t *testing.T) {
	db, acc := fleetTestAccounts(t, 3)
	bindFleet(t, db, acc[0].ID, acc[1].ID, acc[2].ID)
	if err := db.DeleteAccount(context.Background(), acc[1].ID); err != nil {
		t.Fatal(err)
	}
	if fleetPointer(t, db, acc[0].ID) != acc[0].ID || fleetPointer(t, db, acc[2].ID) != acc[0].ID {
		t.Fatal("the remaining members were split")
	}
}
