package store

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestMultiLinkAtomicRollbackRefreshAndStaleLink(t *testing.T) {
	ctx := context.Background()
	db, err := Open(filepath.Join(t.TempDir(), "multi.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	base, err := db.UpsertAccount(ctx, "base", "old", nil, nil, nil, map[string]any{"unionid": "u"}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.UpsertAccountProxySetting(ctx, base.ID, "static", "http", "http://proxy.test:8080", "", nil, "", "", "", 300, nil)
	if err != nil {
		t.Fatal(err)
	}
	link, err := db.CreateAccountLinkWithChannels(ctx, "test", "", "update", base.ID, nil, base.OpenID, time.Now().Add(time.Hour).Unix(), "appstore", false, true, &base.ID)
	if err != nil {
		t.Fatal(err)
	}
	save := func(target *WechatAccount, product, openid string, claim func(int64, bool) error) (*WechatAccount, error) {
		return db.SaveAccountLinkScan(ctx, link, base, target, product, openid, "new", nil, nil, map[string]any{"unionid": "u"}, map[string]any{"provider": product}, claim)
	}
	// Ownership failure rolls back the newly allocated account, copied proxy,
	// root anchoring and progress. No half-built account leaks out.
	if _, err = save(nil, "pcmgr", "pcmgr:new", func(int64, bool) error { return errors.New("claim failed") }); err == nil {
		t.Fatal("claim failure accepted")
	}
	accounts, _ := db.ListAccounts(ctx)
	if len(accounts) != 1 {
		t.Fatal("rollback leaked account")
	}
	root, _ := db.GetAccount(ctx, base.ID)
	if root.FleetPrimaryID != nil {
		t.Fatal("rollback changed root")
	}
	scans, _ := db.AccountLinkScans(ctx, link.ID)
	if len(scans) != 0 {
		t.Fatal("rollback leaked progress")
	}
	member, err := save(nil, "pcmgr", "pcmgr:new", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = save(nil, "pcmgr", "pcmgr:another", nil); !errors.Is(err, ErrAccountIdentityConflict) {
		t.Fatal("second identity for the same fleet channel accepted", err)
	}
	proxy, err := db.GetAccountProxySetting(ctx, member.ID)
	if err != nil || proxy.StaticProxy != "http://proxy.test:8080" {
		t.Fatal("proxy not inherited", err)
	}
	// Refreshing an existing channel keeps its ID and remark, clears its session.
	if err = db.SetAccountRemark(ctx, member.ID, "keep"); err != nil {
		t.Fatal(err)
	}
	if err = db.PutSessionForAccount(ctx, member, 123, map[string]any{}, time.Now().Add(time.Hour).Unix(), ""); err != nil {
		t.Fatal(err)
	}
	updated, err := save(member, "pcmgr", member.OpenID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if updated.ID != member.ID || updated.Remark == nil || *updated.Remark != "keep" {
		t.Fatal("refresh changed stable fields")
	}
	if _, err = db.GetSessionForRecovery(ctx, member.ID, ""); err == nil {
		t.Fatal("stale session survived")
	}
	// Removing the capability while a QR is in flight prevents the write.
	if err = db.RevokeAccountLink(ctx, link.ID, nil); err != nil {
		t.Fatal(err)
	}
	if _, err = save(nil, "gameassist", "ga:new", nil); !errors.Is(err, ErrAccountLinkChanged) {
		t.Fatal("revoked link saved", err)
	}
	accounts, _ = db.ListAccounts(ctx)
	if len(accounts) != 2 {
		t.Fatal("revoked link allocated account")
	}
}

func TestMultiLinkRejectsChangedRootAndExpiredCapability(t *testing.T) {
	ctx := context.Background()
	db, err := Open(filepath.Join(t.TempDir(), "multi.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	base, err := db.UpsertAccount(ctx, "base", "old", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	link, err := db.CreateAccountLinkWithChannels(ctx, "test", "", "update", base.ID, nil, base.OpenID, time.Now().Add(time.Hour).Unix(), "appstore", false, true, &base.ID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.UpsertAccount(ctx, "base", "different", nil, nil, nil, nil, map[string]any{"new": true}, nil); err != nil {
		t.Fatal(err)
	}
	if _, err = db.SaveAccountLinkScan(ctx, link, base, nil, "pcmgr", "pcmgr:new", "new", nil, nil, nil, nil, nil); !errors.Is(err, ErrAccountLinkChanged) {
		t.Fatal("stale verified snapshot accepted", err)
	}
	base, _ = db.GetAccount(ctx, base.ID)
	if _, err = db.sql.ExecContext(ctx, "UPDATE account_links SET expires_at=? WHERE id=?", time.Now().Add(-time.Second).Unix(), link.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = db.SaveAccountLinkScan(ctx, link, base, nil, "pcmgr", "pcmgr:new", "new", nil, nil, nil, nil, nil); !errors.Is(err, ErrAccountLinkChanged) {
		t.Fatal("expired link accepted", err)
	}
}

func TestMultiLinkReferencesFollowCompactionAndDeletion(t *testing.T) {
	ctx := context.Background()
	db, err := Open(filepath.Join(t.TempDir(), "multi.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	spare, err := db.UpsertAccount(ctx, "spare", "b", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	base, err := db.UpsertAccount(ctx, "base", "b", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	link, err := db.CreateAccountLinkWithChannels(ctx, "test", "", "update", base.ID, nil, base.OpenID, time.Now().Add(time.Hour).Unix(), "appstore", false, true, &base.ID)
	if err != nil {
		t.Fatal(err)
	}
	member, err := db.SaveAccountLinkScan(ctx, link, base, nil, "pcmgr", "pcmgr:new", "b", nil, nil, nil, map[string]any{"provider": "pcmgr"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = db.DeleteAccount(ctx, spare.ID); err != nil {
		t.Fatal(err)
	}
	mapping, err := db.CompactAccountIDs(ctx)
	if err != nil {
		t.Fatal(err)
	}
	fresh, err := db.GetAccountLink(ctx, link.ID)
	if err != nil {
		t.Fatal(err)
	}
	if fresh.AccountID != mapping[base.ID] || fresh.MultiPrimaryID == nil || *fresh.MultiPrimaryID != mapping[base.ID] {
		t.Fatal("link root not remapped")
	}
	scans, err := db.AccountLinkScans(ctx, link.ID)
	if err != nil || len(scans) != 1 || scans[0].AccountID != mapping[member.ID] {
		t.Fatal("progress not remapped", err)
	}
	if err = db.DeleteAccount(ctx, fresh.AccountID); err != nil {
		t.Fatal(err)
	}
	if _, err = db.GetAccountLink(ctx, link.ID); err == nil {
		t.Fatal("deleted root left live capability")
	}
	scans, err = db.AccountLinkScans(ctx, link.ID)
	if err != nil || len(scans) != 0 {
		t.Fatal("deleted link left progress", err)
	}
}
