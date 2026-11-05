package httpapi

import (
	"context"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"
)

// A multi-channel scan confirms its accounts one by one and the console merges
// them right after each confirmation. The panel sync waits for that, so a member
// is listed through its primary while an account that stayed independent lists
// itself.
func TestDeferredScanSyncWaitsForTheMerge(t *testing.T) {
	fake, server := newFakeDaidaiPanel(t)
	defer server.Close()
	app, _, primaryRef := newRunsTestApp(t, server.URL)
	app.qinglong.reconfigure(PanelTypeDaidai, server.URL, fake.appKey, fake.appSecret)
	ctx := context.Background()
	status := "alive"
	primaryID, _ := strconv.ParseInt(primaryRef, 10, 64)
	member, err := app.db.UpsertAccount(ctx, "scanned-second", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}
	stranger, err := app.db.UpsertAccount(ctx, "scanned-by-someone-else", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}
	memberRef := strconv.FormatInt(member.ID, 10)
	strangerRef := strconv.FormatInt(stranger.ID, 10)

	// Both scans are confirmed; the sync is postponed. Then the console merges one.
	app.autoSyncAfterScanLater(member, 150*time.Millisecond)
	app.autoSyncAfterScanLater(stranger, 150*time.Millisecond)
	for _, id := range []int64{primaryID, member.ID} {
		if err := app.db.SetAccountFleet(ctx, id, primaryID); err != nil {
			t.Fatal(err)
		}
	}
	entry := func(ref string) string { return "yyb-go:8000@" + ref }
	deadline := time.Now().Add(5 * time.Second)
	for {
		fake.mu.Lock()
		value := ""
		if len(fake.envs) > 0 {
			value = fake.envs[0].Value
		}
		fake.mu.Unlock()
		if strings.Contains(value, entry(primaryRef)) && strings.Contains(value, entry(strangerRef)) {
			if strings.Contains(value, entry(memberRef)) {
				t.Fatalf("the merged account must not be listed on its own: %q", value)
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the panel never listed the primary and the independent account: %q", value)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// The panel variable lists a fleet once, through its primary: requests for the
// primary already rotate across the members, so an entry per member would run
// every script once per channel.
func TestPanelListsAFleetThroughItsPrimary(t *testing.T) {
	fake, server := newFakeDaidaiPanel(t)
	defer server.Close()
	app, handler, primaryRef := newRunsTestApp(t, server.URL)
	app.qinglong.reconfigure(PanelTypeDaidai, server.URL, fake.appKey, fake.appSecret)
	ctx := context.Background()
	status := "alive"
	primaryID, _ := strconv.ParseInt(primaryRef, 10, 64)
	member, err := app.db.UpsertAccount(ctx, "fleet-member", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}
	alone, err := app.db.UpsertAccount(ctx, "stands-alone", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []int64{primaryID, member.ID} {
		if err := app.db.SetAccountFleet(ctx, id, primaryID); err != nil {
			t.Fatal(err)
		}
	}
	memberRef := strconv.FormatInt(member.ID, 10)
	aloneRef := strconv.FormatInt(alone.ID, 10)
	entry := func(ref string) string { return "yyb-go:8000@" + ref }
	panelValue := func() string {
		fake.mu.Lock()
		defer fake.mu.Unlock()
		if len(fake.envs) == 0 {
			return ""
		}
		return fake.envs[0].Value
	}

	// Syncing a member lists its primary and says so.
	response := apiRequest(t, handler, http.MethodPost, "/api/qinglong/sync", map[string]any{"ref": memberRef})
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"synced_id":`+primaryRef) {
		t.Fatalf("member sync: %d %s", response.Code, response.Body.String())
	}
	if value := panelValue(); strings.Count(value, entry(primaryRef)) != 1 || strings.Contains(value, entry(memberRef)) {
		t.Fatalf("a member must be listed through its primary: %q", value)
	}
	// Syncing the primary again changes nothing.
	response = apiRequest(t, handler, http.MethodPost, "/api/qinglong/sync", map[string]any{"ref": primaryRef})
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"added":false`) {
		t.Fatalf("primary sync: %d %s", response.Code, response.Body.String())
	}
	// An account outside every fleet lists itself.
	response = apiRequest(t, handler, http.MethodPost, "/api/qinglong/sync", map[string]any{"ref": aloneRef})
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"synced_id":`+aloneRef) {
		t.Fatalf("standalone sync: %d %s", response.Code, response.Body.String())
	}
	// Syncing everything skips the member as well.
	response = apiRequest(t, handler, http.MethodPost, "/api/qinglong/sync-all", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("sync all: %d %s", response.Code, response.Body.String())
	}
	value := panelValue()
	if strings.Count(value, entry(primaryRef)) != 1 || strings.Count(value, entry(aloneRef)) != 1 || strings.Contains(value, entry(memberRef)) {
		t.Fatalf("sync-all listed a member: %q", value)
	}
}
