package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestDynamicAppIDCooldownOnlyOneConcurrentCheckAndSuccessClears(t *testing.T) {
	db, err := store.Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	d := newDynamicAppIDDeny(db, 2, nil, false)
	now := time.Now().Truncate(time.Second)
	d.now = func() time.Time { return now }
	id := "wx1111111111111111"
	d.recordStrike(id, 1)
	d.recordStrike(id, 2)
	if _, denied := d.begin(id); denied == nil || denied.RetryAfter != dynamicAppIDCooldown {
		t.Fatalf("missing temporary cooldown: %+v", denied)
	}
	now = now.Add(dynamicAppIDCooldown)
	var wg sync.WaitGroup
	var admitted atomic.Int32
	var finish func(bool)
	for range 24 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			cleanup, denied := d.begin(id)
			if denied == nil {
				admitted.Add(1)
				finish = cleanup
			}
		}()
	}
	wg.Wait()
	if admitted.Load() != 1 {
		t.Fatalf("admitted %d checks", admitted.Load())
	}
	d.recordSuccess(id)
	finish(false)
	if len(d.list()) != 0 || len(newDynamicAppIDDeny(db, 2, nil, false).list()) != 0 {
		t.Fatal("successful code did not clear persisted hold")
	}
}

func TestDynamicAppIDFailedCheckRearmsButLocalFailureDoesNot(t *testing.T) {
	d := newDynamicAppIDDeny(nil, 2, nil, false)
	now := time.Now().Truncate(time.Second)
	d.now = func() time.Time { return now }
	id := "wx2222222222222222"
	d.recordStrike(id, 1)
	d.recordStrike(id, 2)
	now = now.Add(dynamicAppIDCooldown)
	finish, denied := d.begin(id)
	if denied != nil {
		t.Fatal(denied)
	}
	finish(false) // request never reached upstream, or typed non-empty failure
	finish, denied = d.begin(id)
	if denied != nil {
		t.Fatalf("local failure extended global hold: %v", denied)
	}
	finish(true) // upstream returned another empty code
	if _, denied = d.begin(id); denied == nil || denied.RetryAfter != dynamicAppIDCooldown {
		t.Fatalf("empty result did not rearm bounded cooldown: %v", denied)
	}
	now = now.Add(dynamicAppIDCooldown)
	finish, denied = d.begin(id)
	if denied != nil {
		t.Fatal("temporary hold became permanent")
	}
	finish(false)
}

func TestDynamicAppIDLegacyMigrationKeepsFiniteDeadlineAcrossRestart(t *testing.T) {
	db, err := store.Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	id := "wx3333333333333333"
	if err := db.SetSetting(context.Background(), dynamicAppIDDenySettingKey, `{"`+id+`":"legacy deny"}`); err != nil {
		t.Fatal(err)
	}
	d := newDynamicAppIDDeny(db, 2, nil, false)
	deadline := d.reviewState()[id]["retry_at"].(int64)
	if deadline <= time.Now().Unix() || deadline > time.Now().Add(dynamicAppIDCooldown+time.Second).Unix() {
		t.Fatal("legacy entry not migrated to a finite cooldown")
	}
	restarted := newDynamicAppIDDeny(db, 2, nil, false)
	if restarted.reviewState()[id]["retry_at"] != deadline {
		t.Fatal("restart extended cooldown")
	}
	restarted.now = func() time.Time { return time.Unix(deadline+1, 0) }
	finish, denied := restarted.begin(id)
	if denied != nil {
		t.Fatal("legacy entry cannot be reviewed")
	}
	finish(false)
}

func TestDynamicAppIDOldFailuresExpireAndFleetIsOneGroup(t *testing.T) {
	d := newDynamicAppIDDeny(nil, 2, nil, false)
	now := time.Now()
	d.now = func() time.Time { return now }
	d.recordStrike("wx4444444444444444", 1)
	now = now.Add(dynamicAppIDStrikeWindow)
	d.recordStrike("wx4444444444444444", 2)
	if len(d.list()) != 0 {
		t.Fatal("old failure counted forever")
	}
	app := newKeepAliveTestApp(t)
	defer app.Close()
	primary := int64(7)
	for _, id := range []int64{7, 19} {
		app.learnDeniedAppID(&store.WechatAccount{ID: id, FleetPrimaryID: &primary}, "wx5555555555555555", protocol.ErrLoginCodeUnavailable)
	}
	if len(app.dynamicAppIDs.list()) != 0 {
		t.Fatal("one WeChat fleet counted as independent accounts")
	}
	app.learnDeniedAppID(&store.WechatAccount{ID: 8}, "wx5555555555555555", protocol.ErrLoginCodeUnavailable)
	if len(app.dynamicAppIDs.list()) != 1 {
		t.Fatal("independent primary group not counted")
	}
}

func TestDynamicAppIDStrikeWindowDoesNotSlideWithEachFailure(t *testing.T) {
	d := newDynamicAppIDDeny(nil, 2, nil, false)
	now := time.Now()
	d.now = func() time.Time { return now }
	id := "wx9999999999999999"
	for range 4 {
		d.recordStrike(id, 1)
		now = now.Add(6 * time.Minute)
	}
	if len(d.list()) != 0 {
		t.Fatal("failures outside the 15-minute window accumulated into a hold")
	}
}

func TestAppIDAllowOverridesLearnedAtRuntimeAndRestart(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	app.auth = nil
	id := "wx6666666666666666"
	app.dynamicAppIDs.recordStrike(id, 1)
	app.dynamicAppIDs.recordStrike(id, 2)
	request := httptest.NewRequest(http.MethodPost, "/api/appid-deny", strings.NewReader("action=add_allow&app_id="+id))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	out := httptest.NewRecorder()
	app.handleAppIDDeny(out, request)
	if out.Code != http.StatusOK || !app.appIDs.isAllowed(id) || len(app.dynamicAppIDs.list()) != 0 {
		t.Fatalf("panel allowance still blocked: %s", out.Body.String())
	}
	acc := insertKeepAliveTestAccount(t, app, "synthetic-allowed", time.Now().Add(time.Hour))
	for range 5 {
		app.learnDeniedAppID(acc, id, protocol.ErrLoginCodeUnavailable)
	}
	if len(app.dynamicAppIDs.list()) != 0 {
		t.Fatal("explicit allowance relearned")
	}
	restarted := newAppIDPolicy(nil, nil, app.db)
	if !restarted.isAllowed(id) {
		t.Fatal("persisted allowance missing on restart")
	}
	app.dynamicAppIDs.recordStrike(id, 1)
	app.dynamicAppIDs.recordStrike(id, 2)
	calls := 0
	_, err := app.invokeWXApp(withWXOperationContext(context.Background(), eventlog.OpGetCode), acc, id, nil,
		func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
			calls++
			return map[string]any{"code": "synthetic-code"}, nil
		})
	if err != nil || calls != 1 {
		t.Fatalf("allowed AppID denied by stale learned entry: %v", err)
	}
}

func TestAppIDEnvAllowAndDenyRestoreAfterPanelRemoval(t *testing.T) {
	db, err := store.Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	id := "wx7777777777777777"
	p := newAppIDPolicy([]string{id}, []string{id}, db)
	if err := p.AddDeny(id, "manual"); err != nil {
		t.Fatal(err)
	}
	if p.check(id) != nil || !p.isAllowed(id) {
		t.Fatal("panel deny defeated explicit env allow")
	}
	p = newAppIDPolicy([]string{id}, nil, db)
	if err := p.AddAllow(id); err != nil {
		t.Fatal(err)
	}
	if err := p.RemoveAllow(id); err != nil {
		t.Fatal(err)
	}
	if p.check(id) == nil || p.isAllowed(id) {
		t.Fatal("removing panel allow lost env deny")
	}
	if err := p.AddDeny(id, "manual"); err != nil {
		t.Fatal(err)
	}
	if err := p.RemoveDeny(id); err != nil {
		t.Fatal(err)
	}
	if p.check(id) == nil {
		t.Fatal("removing panel deny lost env deny")
	}
}

func TestTemporaryAppIDResponseCarriesRetryAfter(t *testing.T) {
	out := httptest.NewRecorder()
	if !writeIfAppIDDenied(out, &appIDDeniedError{AppID: "wx8888888888888888", Why: "temporary", RetryAfter: 1500 * time.Millisecond}) {
		t.Fatal("temporary error not rendered")
	}
	var body struct {
		Data struct {
			Temporary bool `json:"temporary"`
			Retry     int  `json:"retry_after"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if out.Code != 403 || out.Header().Get("Retry-After") != "2" || !body.Data.Temporary || body.Data.Retry != 2 {
		t.Fatalf("missing retry contract: %s", out.Body.String())
	}
	if writeIfAppIDDenied(httptest.NewRecorder(), errors.New("ordinary error")) {
		t.Fatal("unrelated error handled")
	}
}
