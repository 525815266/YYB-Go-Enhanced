package httpapi

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"yyb_go/internal/auth"
	"yyb_go/internal/store"
)

func TestMergeAccidentalAccountAuthorizationPreviewCommitAndReferences(t *testing.T) {
	fake, server := newFakeQingLong(t)
	app, _, _ := newRunsTestApp(t, server.URL)
	ctx := context.Background()
	target, _ := app.db.GetAccount(ctx, 1)
	source, err := app.db.UpsertAccount(ctx, "pcmgr:accidental", "new-login", nil, nil, nil, nil, map[string]any{"provider": "pcmgr"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = app.db.UpsertAccountProxySetting(ctx, target.ID, "static", "http", "http://original:8080", "", nil, "", "", "", 300, nil); err != nil {
		t.Fatal(err)
	}
	if _, err = app.db.UpsertAccountScriptJob(ctx, target.ID, "keep.py", 123, "0 0 * * *"); err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	fake.envs = []qingLongEnv{{ID: 4, Name: "YYB_SERVER", Value: "host@1\nhost@2\nhost@test-openid\nmanual@9", Status: 0}}
	fake.mu.Unlock()
	request := func(confirm bool, key string) *httptest.ResponseRecorder {
		body, _ := json.Marshal(map[string]any{"source_ref": strconv.FormatInt(source.ID, 10), "target_ref": "1", "confirm": confirm, "preview_key": key})
		w := httptest.NewRecorder()
		app.handleAccountMerge(w, httptest.NewRequest("POST", "/accounts/merge", strings.NewReader(string(body))))
		return w
	}
	w := request(true, "")
	if w.Code != 409 {
		t.Fatal("merge skipped preview", w.Code)
	}
	w = request(false, "")
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	var preview struct {
		Data struct {
			Key string `json:"preview_key"`
		}
	}
	if err = json.Unmarshal(w.Body.Bytes(), &preview); err != nil {
		t.Fatal(err)
	}
	if accounts, _ := app.db.ListAccounts(ctx); len(accounts) != 2 {
		t.Fatal("preview mutated accounts")
	}
	w = request(true, preview.Data.Key)
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	kept, _ := app.db.GetAccount(ctx, 1)
	if kept.OpenID != source.OpenID || kept.LoginBuffer != source.LoginBuffer || kept.UIN != nil {
		t.Fatal("authorization not moved")
	}
	accounts, _ := app.db.ListAccounts(ctx)
	if len(accounts) != 1 || accounts[0].ID != 1 {
		t.Fatal("source ID not released")
	}
	proxy, _ := app.db.GetAccountProxySetting(ctx, 1)
	if proxy.StaticProxy != "http://original:8080" {
		t.Fatal("target proxy not kept")
	}
	if _, err = app.db.GetAccountScriptJob(ctx, 1, "keep.py"); err != nil {
		t.Fatal("target task lost", err)
	}
	fake.mu.Lock()
	value := fake.envs[0].Value
	fake.mu.Unlock()
	if strings.Contains(value, "@2") || strings.Contains(value, "@test-openid") || strings.Count(value, "host@1") != 1 || !strings.Contains(value, "manual@9") {
		t.Fatal("source numeric refs not merged", value)
	}
	if w = request(true, preview.Data.Key); w.Code == 200 {
		t.Fatal("merge replay succeeded")
	}
}

func TestMergeRejectsConfiguredSourceAndStalePreview(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	target, _ := app.db.UpsertAccount(ctx, "target", "old", nil, nil, nil, nil, nil, nil)
	source, _ := app.db.UpsertAccount(ctx, "pcmgr:source", "new", nil, nil, nil, nil, map[string]any{"provider": "pcmgr"}, nil)
	key := app.accountMergePreviewKey(source, target)
	if err = app.db.SetAccountCredential(ctx, source.ID, "rescanned", map[string]any{"provider": "pcmgr", "new": true}); err != nil {
		t.Fatal(err)
	}
	body := `{"source_ref":"2","target_ref":"1","confirm":true,"preview_key":"` + key + `"}`
	w := httptest.NewRecorder()
	app.handleAccountMerge(w, httptest.NewRequest("POST", "/accounts/merge", strings.NewReader(body)))
	if w.Code != 409 {
		t.Fatal("stale preview accepted", w.Code)
	}
	if _, err = app.db.UpsertAccountScriptJob(ctx, source.ID, "source.py", 45, "0 1 * * *"); err != nil {
		t.Fatal(err)
	}
	w = httptest.NewRecorder()
	app.handleAccountMerge(w, httptest.NewRequest("POST", "/accounts/merge", strings.NewReader(`{"source_ref":"2","target_ref":"1"}`)))
	if w.Code != 409 {
		t.Fatal("configured source accepted", w.Code)
	}
	if accounts, _ := app.db.ListAccounts(ctx); len(accounts) != 2 {
		t.Fatal("rejected merge changed data")
	}
}

func TestMergeOwnershipPermissionsAndClaimCleanup(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite"})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	ctx := context.Background()
	user, _ := app.auth.CreateUser(ctx, "owner", "Owner", "local-owner-password", "user")
	other, _ := app.auth.CreateUser(ctx, "other", "Other", "local-other-password", "user")
	target, _ := app.db.UpsertAccount(ctx, "target", "old", nil, nil, nil, nil, nil, nil)
	source, _ := app.db.UpsertAccount(ctx, "pcmgr:source", "new", nil, nil, nil, nil, map[string]any{"provider": "pcmgr"}, nil)
	for _, pair := range []struct {
		acc    *store.WechatAccount
		userID int64
	}{{target, user.ID}, {source, other.ID}} {
		if err = app.auth.ClaimAccount(ctx, pair.acc.ID, pair.userID); err != nil {
			t.Fatal(err)
		}
	}
	request := func(role string, id int64, confirm bool) *httptest.ResponseRecorder {
		body, _ := json.Marshal(map[string]any{"source_ref": "2", "target_ref": "1", "confirm": confirm, "preview_key": app.accountMergePreviewKey(source, target)})
		req := httptest.NewRequest("POST", "/accounts/merge", strings.NewReader(string(body)))
		req = req.WithContext(context.WithValue(ctx, authUserKey, &auth.User{ID: id, Role: role}))
		w := httptest.NewRecorder()
		app.handleAccountMerge(w, req)
		return w
	}
	if w := request("user", user.ID, false); w.Code == 200 {
		t.Fatal("user merged another owner")
	}
	if w := request("admin", 99, false); w.Code != 409 {
		t.Fatal("cross-owner admin merge accepted", w.Code)
	}
	if err = app.auth.RemoveAccountOwner(ctx, source.ID, other.ID); err != nil {
		t.Fatal(err)
	}
	if err = app.auth.ClaimAccount(ctx, source.ID, user.ID); err != nil {
		t.Fatal(err)
	}
	if w := request("user", user.ID, true); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	if owner, err := app.auth.AccountOwner(ctx, target.ID); err != nil || owner != user.ID {
		t.Fatal("target ownership lost", err)
	}
	if owner, err := app.auth.AccountOwner(ctx, source.ID); err == nil || owner != 0 {
		t.Fatal("released ID kept an owner")
	}
}

func TestMergeReferenceRetryCannotHijackReusedSourceID(t *testing.T) {
	fake, server := newFakeQingLong(t)
	app, _, _ := newRunsTestApp(t, server.URL)
	ctx := context.Background()
	target, _ := app.db.GetAccount(ctx, 1)
	source, _ := app.db.UpsertAccount(ctx, "pcmgr:source", "new", nil, nil, nil, nil, map[string]any{"provider": "pcmgr"}, nil)
	merged, err := app.db.MergeAccountAuthorization(ctx, source, target)
	if err != nil {
		t.Fatal(err)
	}
	replacement, err := app.db.UpsertAccount(ctx, "unrelated-new-account", "other", nil, nil, nil, nil, nil, nil)
	if err != nil || replacement.ID != source.ID {
		t.Fatal("test ID was not reused", err)
	}
	fake.mu.Lock()
	fake.envs = []qingLongEnv{{ID: 4, Name: "YYB_SERVER", Value: "host@2", Status: 0}}
	fake.mu.Unlock()
	if _, _, err = app.syncAccountToQingLong(ctx, merged); err == nil {
		t.Fatal("reused numeric reference silently redirected")
	}
	fake.mu.Lock()
	value := fake.envs[0].Value
	fake.mu.Unlock()
	if value != "host@2" {
		t.Fatal("unrelated account variable overwritten")
	}
}
