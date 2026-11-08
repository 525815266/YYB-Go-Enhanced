package store

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"testing"
	"time"
	"yyb_go/internal/qbbrowser"
)

func TestQQNativeCredentialsSurviveDatabaseReopen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "qq.db")
	ctx := context.Background()
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	expiry := time.Now().Add(time.Hour).Unix()
	qb := qbbrowser.Credentials{OpenID: "synthetic", UnionID: "synthetic-union", QBAccessToken: "synthetic-access", WXRefreshToken: "synthetic-refresh"}
	status := "alive"
	acc, err := db.UpsertAccount(ctx, qb.Identity(), "synthetic-buffer", nil, nil, nil, map[string]any{"unionid": qb.UnionID}, map[string]any{"provider": "qbbrowser", "qbbrowser": qb, "expires_at": expiry, "expires_in": 7200, "expiry_source": "upstream", "refresh_token_observed_at": time.Now().Add(-26 * 24 * time.Hour).Unix()}, &status)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	db, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	saved, err := db.GetAccount(ctx, acc.ID)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(saved.Credentials["qbbrowser"])
	var got qbbrowser.Credentials
	if err := json.Unmarshal(raw, &got); err != nil || got != qb {
		t.Fatal("native tokens or union identity lost on SQLite reopen")
	}
	p := saved.Public()
	if p.CredentialExpiresAt == nil || *p.CredentialExpiresAt != expiry || p.CredentialExpiresIn != 7200 || p.RefreshTokenObservedAt != nil || p.RescanRecommended {
		t.Fatal("upstream lifetime lost or unverified 30-day lifetime applied")
	}
}

func TestQQBrowserPushCannotEraseStandaloneRefreshChain(t *testing.T) {
	db, err := Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	status := "alive"
	qb := qbbrowser.Credentials{OpenID: "synthetic", QBAccessToken: "access", WXRefreshToken: "refresh", UnionID: "union"}
	original := map[string]any{"provider": "qbbrowser", "qbbrowser": qb, "expires_at": 123, "expires_in": 7200}
	acc, err := db.UpsertAccount(ctx, qb.Identity(), "native-buffer", nil, nil, nil, map[string]any{"unionid": qb.UnionID}, original, &status)
	if err != nil {
		t.Fatal(err)
	}
	bridge := qb
	bridge.WXRefreshToken = ""
	bridge.UserID = "native-machine"
	if _, err := db.UpsertAccount(ctx, qb.Identity(), "bridge-buffer", nil, nil, nil, nil, map[string]any{"provider": "qbbrowser", "qbbrowser": bridge}, &status); !errors.Is(err, ErrQQBrowserBridgeDowngrade) {
		t.Fatal("browser push erased independent authorization")
	}
	saved, _ := db.GetAccount(ctx, acc.ID)
	if saved.LoginBuffer != "native-buffer" || saved.UserInfo["unionid"] != qb.UnionID {
		t.Fatal("rejected push changed persisted state")
	}
	qb.QBAccessToken = "new-access"
	if _, err := db.UpsertAccount(ctx, qb.Identity(), "rescanned-buffer", nil, nil, nil, nil, map[string]any{"provider": "qbbrowser", "qbbrowser": qb}, &status); err != nil {
		t.Fatal("new independent scan blocked")
	}
}
