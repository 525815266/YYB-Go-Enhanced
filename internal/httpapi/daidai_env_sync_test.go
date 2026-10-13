package httpapi

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"testing"

	"yyb_go/internal/store"
)

func TestDaidaiSyncKeepsVariableIDAcrossAccounts(t *testing.T) {
	fake, server := newFakeDaidaiPanel(t)
	defer server.Close()
	app, handler, ref := newRunsTestApp(t, server.URL)
	app.qinglong.reconfigure(PanelTypeDaidai, server.URL, fake.appKey, fake.appSecret)
	refs := []string{ref}
	status := "alive"
	for i := 1; i <= 3; i++ {
		nickname := fmt.Sprintf("account-%d", i)
		openid := "test-openid"
		if i > 1 {
			openid = fmt.Sprintf("openid-%d", i)
		}
		acc, err := app.db.UpsertAccount(context.Background(), openid, "buffer", nil, &nickname, nil, nil, nil, &status)
		if err != nil {
			t.Fatal(err)
		}
		if i > 1 {
			refs = append(refs, strconv.FormatInt(acc.ID, 10))
		}
		response := apiRequest(t, handler, http.MethodPost, "/api/qinglong/sync", map[string]any{"ref": refs[i-1]})
		if response.Code != http.StatusOK {
			t.Fatalf("sync %d: %d %s", i, response.Code, response.Body.String())
		}
	}
	response := apiRequest(t, handler, http.MethodPost, "/api/qinglong/sync-all", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("sync all: %d %s", response.Code, response.Body.String())
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.envs) != 1 || fake.envUpserts != 1 || len(fake.envUpdateIDs) != 3 {
		t.Fatalf("expected one creation and three ID updates; rows=%d creates=%d updates=%v", len(fake.envs), fake.envUpserts, fake.envUpdateIDs)
	}
	for _, id := range fake.envUpdateIDs {
		if id != fake.envs[0].ID {
			t.Fatalf("variable ID changed: %v", fake.envUpdateIDs)
		}
	}
	for _, accountRef := range refs {
		if strings.Count(fake.envs[0].Value, "yyb-go:8000@"+accountRef) != 1 {
			t.Fatalf("missing or repeated account: %q", fake.envs[0].Value)
		}
	}
	if fake.envs[0].Remarks != "YYB Go 账号：account-1、account-2、account-3" {
		t.Fatalf("remarks=%q", fake.envs[0].Remarks)
	}
}

func TestDaidaiSyncPrefersEnabledVariable(t *testing.T) {
	for _, path := range []string{"/api/qinglong/sync", "/api/qinglong/sync-all"} {
		t.Run(path, func(t *testing.T) {
			fake, server := newFakeDaidaiPanel(t)
			defer server.Close()
			app, handler, ref := newRunsTestApp(t, server.URL)
			app.qinglong.reconfigure(PanelTypeDaidai, server.URL, fake.appKey, fake.appSecret)
			disabled, enabled := false, true
			fake.envs = []qingLongEnv{
				{ID: 11, Name: "YYB_SERVER", Value: "stale-host@90", Remarks: "old", Enabled: &disabled},
				{ID: 12, Name: "YYB_SERVER", Value: "custom-host@88", Remarks: "custom remarks", Enabled: &enabled},
			}
			response := apiRequest(t, handler, http.MethodPost, path, map[string]any{"ref": ref})
			if response.Code != http.StatusOK {
				t.Fatalf("sync: %d %s", response.Code, response.Body.String())
			}
			fake.mu.Lock()
			defer fake.mu.Unlock()
			if len(fake.envs) != 2 || fake.envUpserts != 0 || len(fake.envUpdateIDs) != 1 || fake.envUpdateIDs[0] != 12 {
				t.Fatalf("unexpected writes: %+v, %v", fake.envs, fake.envUpdateIDs)
			}
			if fake.envs[0].Value != "stale-host@90" || fake.envs[0].enabled() {
				t.Fatal("disabled history was modified")
			}
			if fake.envs[1].Value != "custom-host@88\nyyb-go:8000@"+ref || fake.envs[1].Remarks != "custom remarks" || !fake.envs[1].enabled() {
				t.Fatalf("active variable changed incorrectly: %+v", fake.envs[1])
			}
		})
	}
}

func TestDaidaiSyncRejectsAmbiguousVariablesWithoutWrites(t *testing.T) {
	for _, enabled := range []bool{true, false} {
		for _, path := range []string{"/api/qinglong/sync", "/api/qinglong/sync-all"} {
			t.Run(fmt.Sprintf("%t-%s", enabled, path), func(t *testing.T) {
				fake, server := newFakeDaidaiPanel(t)
				defer server.Close()
				app, handler, ref := newRunsTestApp(t, server.URL)
				app.qinglong.reconfigure(PanelTypeDaidai, server.URL, fake.appKey, fake.appSecret)
				fake.envs = []qingLongEnv{{ID: 11, Name: "YYB_SERVER", Value: "host@8", Enabled: &enabled}, {ID: 12, Name: "YYB_SERVER", Value: "host@9", Enabled: &enabled}}
				response := apiRequest(t, handler, http.MethodPost, path, map[string]any{"ref": ref})
				if response.Code != http.StatusBadGateway || !strings.Contains(response.Body.String(), "已有变量未被修改") {
					t.Fatalf("expected conflict: %d %s", response.Code, response.Body.String())
				}
				fake.mu.Lock()
				defer fake.mu.Unlock()
				if fake.envUpserts != 0 || len(fake.envUpdateIDs) != 0 || fake.envs[0].Value != "host@8" || fake.envs[1].Value != "host@9" {
					t.Fatal("ambiguous variables modified")
				}
			})
		}
	}
}

func TestDaidaiUpdateEnvEntryUsesIDAndPreservesDisabledState(t *testing.T) {
	fake, server := newFakeDaidaiPanel(t)
	defer server.Close()
	disabled := false
	fake.envs = []qingLongEnv{{ID: 7, Name: "YYB_SERVER", Value: "old", Remarks: "custom", Enabled: &disabled}}
	driver := newDaidaiDriver(server.URL, fake.appKey, fake.appSecret, 0)
	if err := driver.UpdateEnvEntry(context.Background(), fake.envs[0], "new"); err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.envUpserts != 0 || len(fake.envUpdateIDs) != 1 || fake.envUpdateIDs[0] != 7 || fake.envs[0].Value != "new" || fake.envs[0].Remarks != "custom" || fake.envs[0].enabled() {
		t.Fatalf("unexpected update: %+v", fake.envs)
	}
}

func TestDaidaiSyncUpdateFailureDoesNotCreateReplacement(t *testing.T) {
	fake, server := newFakeDaidaiPanel(t)
	defer server.Close()
	app, handler, ref := newRunsTestApp(t, server.URL)
	app.qinglong.reconfigure(PanelTypeDaidai, server.URL, fake.appKey, fake.appSecret)
	fake.envs = []qingLongEnv{{ID: 11, Name: "YYB_SERVER", Value: "host@8"}}
	fake.failEnvUpdate = true
	response := apiRequest(t, handler, http.MethodPost, "/api/qinglong/sync", map[string]any{"ref": ref})
	if response.Code != http.StatusBadGateway {
		t.Fatalf("expected failure: %d %s", response.Code, response.Body.String())
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.envs) != 1 || fake.envUpserts != 0 || fake.envs[0].Value != "host@8" {
		t.Fatal("update failure created or changed an environment")
	}
}

func TestDaidaiConcurrentSyncKeepsAllAccounts(t *testing.T) {
	fake, server := newFakeDaidaiPanel(t)
	defer server.Close()
	app, _, _ := newRunsTestApp(t, server.URL)
	app.qinglong.reconfigure(PanelTypeDaidai, server.URL, fake.appKey, fake.appSecret)
	accounts := []*store.WechatAccount{}
	status := "alive"
	for i := 0; i < 8; i++ {
		acc, err := app.db.UpsertAccount(context.Background(), fmt.Sprintf("concurrent-%d", i), "buffer", nil, nil, nil, nil, nil, &status)
		if err != nil {
			t.Fatal(err)
		}
		accounts = append(accounts, acc)
	}
	var group sync.WaitGroup
	errors := make(chan error, len(accounts))
	for _, acc := range accounts {
		group.Add(1)
		go func(account *store.WechatAccount) {
			defer group.Done()
			_, _, err := app.syncAccountToQingLong(context.Background(), account)
			errors <- err
		}(acc)
	}
	group.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.envs) != 1 || fake.envUpserts != 1 || len(strings.Split(fake.envs[0].Value, "\n")) != len(accounts) {
		t.Fatalf("concurrent sync lost entries or created copies: %+v", fake.envs)
	}
}
