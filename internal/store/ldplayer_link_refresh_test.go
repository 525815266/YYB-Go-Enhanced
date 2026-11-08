package store

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestLDPlayerConfirmedRescanReusesExpiredMemberID(t *testing.T) {
	ctx := context.Background()
	db, err := Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	base, _ := db.UpsertAccount(ctx, "root", "root-ticket", nil, nil, nil, nil, nil, nil)
	expired := "expired"
	member, _ := db.UpsertAccount(ctx, "ldplayer:old-ticket", "", nil, nil, nil, nil, map[string]any{"provider": "ldplayer"}, &expired)
	if err = db.SetAccountFleet(ctx, base.ID, base.ID); err != nil {
		t.Fatal(err)
	}
	if err = db.SetAccountFleet(ctx, member.ID, base.ID); err != nil {
		t.Fatal(err)
	}
	base, _ = db.GetAccount(ctx, base.ID)
	member, _ = db.GetAccount(ctx, member.ID)
	if err = db.SetAccountRemark(ctx, member.ID, "preserve-remark"); err != nil {
		t.Fatal(err)
	}
	link, _ := db.CreateAccountLinkWithChannels(ctx, "test", "", "update", base.ID, nil, base.OpenID, time.Now().Add(time.Hour).Unix(), "appstore", false, true, &base.ID)
	save := func(confirm bool) (*WechatAccount, error) {
		return db.SaveAccountLinkScan(ctx, link, base, member, "ldplayer", "ldplayer:new-ticket", "fresh", nil, nil, nil, map[string]any{"provider": "ldplayer"}, nil, confirm)
	}
	if _, err = save(false); !errors.Is(err, ErrAccountIdentityConflict) {
		t.Fatal("changed ticket replaced without confirmation", err)
	}
	next, err := save(true)
	if err != nil || next.ID != member.ID || next.FleetPrimaryID == nil || *next.FleetPrimaryID != base.ID || next.Remark == nil || *next.Remark != "preserve-remark" || next.OpenID != "ldplayer:new-ticket" {
		t.Fatal("rescan changed member ID or anchor", err)
	}
	rows, _ := db.ListAccounts(ctx)
	if len(rows) != 2 {
		t.Fatal("rescan allocated another member")
	}
}
