package store

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestAccountChannelReplacementPreservesRelationsAndRejectsStaleWrites(t *testing.T) {
	ctx := context.Background()
	db, err := Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	before, err := db.UpsertAccount(ctx, "yyb-original", "old-buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = db.SetAccountRemark(ctx, before.ID, "keep remark"); err != nil {
		t.Fatal(err)
	}
	if _, err = db.UpsertAccountProxySetting(ctx, before.ID, "static", "http", "http://proxy.test:8080", "", nil, "", "", "", 300, nil); err != nil {
		t.Fatal(err)
	}
	if _, err = db.UpsertAccountScriptJob(ctx, before.ID, "test.py", 12, "0 1 * * *"); err != nil {
		t.Fatal(err)
	}
	if _, err = db.UpsertAccountPushSetting(ctx, before.ID, "none", "", "topic"); err != nil {
		t.Fatal(err)
	}
	if err = db.PutSessionForAccount(ctx, before, 123, map[string]any{"old": true}, time.Now().Add(time.Hour).Unix(), ""); err != nil {
		t.Fatal(err)
	}
	link, err := db.CreateAccountLinkForProduct(ctx, "link-one", "", "update", before.ID, nil, before.OpenID, time.Now().Add(time.Hour).Unix(), "pcmgr", true)
	if err != nil {
		t.Fatal(err)
	}
	after, err := db.UpdateAccountFromLink(ctx, link, "pcmgr:replacement", "new-buffer", nil, nil, map[string]any{"new": true}, map[string]any{"provider": "pcmgr"})
	if err != nil {
		t.Fatal(err)
	}
	if after.ID != before.ID || after.OpenID != "pcmgr:replacement" || after.UIN != nil || after.CreatedAt != before.CreatedAt || after.Remark == nil || *after.Remark != "keep remark" {
		t.Fatal("replacement changed stable fields", after.ID)
	}
	if _, err = db.GetSessionForRecovery(ctx, before.ID, ""); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("old session survived", err)
	}
	if err = db.PutSessionForAccount(ctx, before, 999, map[string]any{"stale": true}, time.Now().Add(time.Hour).Unix(), ""); !errors.Is(err, ErrAccountAuthorizationChanged) {
		t.Fatal("old login restored session", err)
	}
	unchanged, _ := db.GetAccount(ctx, before.ID)
	if unchanged.UIN != nil {
		t.Fatal("stale login restored UIN")
	}
	proxy, err := db.GetAccountProxySetting(ctx, before.ID)
	if err != nil || proxy.StaticProxy != "http://proxy.test:8080" {
		t.Fatal("proxy lost", err)
	}
	if _, err = db.GetAccountScriptJob(ctx, before.ID, "test.py"); err != nil {
		t.Fatal("job lost", err)
	}
	if _, err = db.GetAccountPushSetting(ctx, before.ID); err != nil {
		t.Fatal("push settings lost", err)
	}
	accounts, _ := db.ListAccounts(ctx)
	if len(accounts) != 1 {
		t.Fatal("allocated another account ID")
	}
	links, _ := db.ListAccountLinks(ctx, nil)
	if len(links) != 0 {
		t.Fatal("consumed link retained")
	}
	refs, _, err := db.PendingAccountReferences(ctx, before.ID)
	if err != nil || len(refs) != 1 || refs[0] != before.OpenID {
		t.Fatal("migration aliases lost", err)
	}
	// Reuse management IDs without permitting an old QR to consume a new link.
	next, err := db.CreateAccountLink(ctx, "link-two", "update", after.ID, nil, after.OpenID, time.Now().Add(time.Hour).Unix())
	if err != nil {
		t.Fatal(err)
	}
	if next.ID != link.ID {
		t.Fatal("consumed management ID not released")
	}
	if _, err = db.UpdateAccountFromLink(ctx, link, "yyb-original", "stale", nil, nil, nil, nil); !errors.Is(err, ErrAccountLinkChanged) {
		t.Fatal("stale link changed authorization", err)
	}
	back, err := db.UpdateAccountFromLink(ctx, next, "yyb-original", "back-buffer", nil, nil, nil, nil)
	if err != nil || back.ID != before.ID {
		t.Fatal("reverse replacement failed", err)
	}
	// Same-channel rescan also invalidates an in-flight login.
	if err = db.PutSessionForAccount(ctx, after, 999, nil, time.Now().Add(time.Hour).Unix(), ""); !errors.Is(err, ErrAccountAuthorizationChanged) {
		t.Fatal(err)
	}
}

func TestAccountReplacementConflictAndConcurrentConsumption(t *testing.T) {
	ctx := context.Background()
	db, err := Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	first, _ := db.UpsertAccount(ctx, "original", "old", nil, nil, nil, nil, nil, nil)
	second, _ := db.UpsertAccount(ctx, "pcmgr:existing", "other", nil, nil, nil, nil, nil, nil)
	link, err := db.CreateAccountLink(ctx, "unique-link", "update", first.ID, nil, first.OpenID, time.Now().Add(time.Hour).Unix())
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.UpdateAccountFromLink(ctx, link, second.OpenID, "bad", nil, nil, nil, nil); !errors.Is(err, ErrAccountIdentityConflict) {
		t.Fatal(err)
	}
	still, _ := db.GetAccount(ctx, first.ID)
	other, _ := db.GetAccount(ctx, second.ID)
	if still.OpenID != first.OpenID || still.LoginBuffer != "old" || other.LoginBuffer != "other" {
		t.Fatal("conflict changed accounts")
	}
	if _, err = db.GetAccountLink(ctx, link.ID); err != nil {
		t.Fatal("conflict consumed link")
	}
	var wg sync.WaitGroup
	results := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := db.UpdateAccountFromLink(ctx, link, "pcmgr:new", "new", nil, nil, nil, nil)
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	successes, stale := 0, 0
	for err := range results {
		if err == nil {
			successes++
		} else if errors.Is(err, ErrAccountLinkChanged) {
			stale++
		} else {
			t.Fatal(err)
		}
	}
	if successes != 1 || stale != 1 {
		t.Fatal("link consumed more than once", successes, stale)
	}
	accounts, _ := db.ListAccounts(ctx)
	if len(accounts) != 2 {
		t.Fatal("account IDs leaked")
	}
}

func TestAccountLinkChannelColumnsMigrateLegacyDatabase(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "legacy.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	acc, err := db.UpsertAccount(ctx, "legacy", "buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	link, err := db.CreateAccountLink(ctx, "legacy-link", "update", acc.ID, nil, acc.OpenID, time.Now().Add(time.Hour).Unix())
	if err != nil {
		t.Fatal(err)
	}
	for _, column := range []string{"target_product", "switch_confirmed"} {
		if _, err = db.sql.ExecContext(ctx, "ALTER TABLE account_links DROP COLUMN "+column); err != nil {
			t.Fatal(err)
		}
	}
	db.Close()
	db, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	migrated, err := db.GetAccountLink(ctx, link.ID)
	if err != nil {
		t.Fatal(err)
	}
	if migrated.TargetProduct != "" || migrated.SwitchConfirmed || migrated.ExpectedOpenID != acc.OpenID {
		t.Fatal("legacy link unexpectedly opted into channel switching")
	}
}

func TestPendingReferencesFollowCompactionAndDeletion(t *testing.T) {
	ctx := context.Background()
	db, err := Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	first, _ := db.UpsertAccount(ctx, "first", "one", nil, nil, nil, nil, nil, nil)
	second, _ := db.UpsertAccount(ctx, "second", "two", nil, nil, nil, nil, nil, nil)
	link, err := db.CreateAccountLink(ctx, "compact-reference", "update", second.ID, nil, second.OpenID, time.Now().Add(time.Hour).Unix())
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.UpdateAccountFromLink(ctx, link, "pcmgr:second", "new", nil, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	if err = db.DeleteAccount(ctx, first.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = db.CompactAccountIDs(ctx); err != nil {
		t.Fatal(err)
	}
	refs, _, err := db.PendingAccountReferences(ctx, 1)
	if err != nil || len(refs) != 1 || refs[0] != "second" {
		t.Fatal("pending references lost during compaction", err)
	}
	refs, _, _ = db.PendingAccountReferences(ctx, 2)
	if len(refs) != 0 {
		t.Fatal("pending references kept old ID")
	}
	if err = db.DeleteAccount(ctx, 1); err != nil {
		t.Fatal(err)
	}
	refs, _, _ = db.PendingAccountReferences(ctx, 1)
	if len(refs) != 0 {
		t.Fatal("deleted account kept pending references")
	}
}
