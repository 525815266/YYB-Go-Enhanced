package store

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
)

func TestAccountProxySettingLifecycle(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	defer db.Close()
	status := "alive"
	account, err := db.UpsertAccount(context.Background(), "openid-proxy", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	setting, err := db.AccountProxySettingOrDefault(context.Background(), account.ID)
	if err != nil || setting.Mode != "direct" || setting.ProtocolProxyMode != "account" {
		t.Fatalf("default setting = %#v, %v", setting, err)
	}
	policy := "direct"
	setting, err = db.UpsertAccountProxySetting(context.Background(), account.ID, "api", "http", "", "https://proxy.example/get?city=jinan", nil, "370100", "山东省", "济南市", 900, &policy)
	if err != nil || setting.Mode != "api" || setting.APIURL == "" {
		t.Fatalf("saved setting = %#v, %v", setting, err)
	}
	if setting.RegionCode != "370100" || setting.RefreshAheadSeconds != 900 || setting.ProtocolProxyMode != policy {
		t.Fatalf("saved region/refresh = %#v", setting)
	}
	target, err := db.UpsertAccount(context.Background(), "copy-target", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.CopyAccountProxySetting(context.Background(), account.ID, target.ID); err != nil {
		t.Fatal(err)
	}
	copied, err := db.GetAccountProxySetting(context.Background(), target.ID)
	if err != nil || copied.ProtocolProxyMode != policy || copied.APIURL != setting.APIURL {
		t.Fatalf("copied policy = %#v, %v", copied, err)
	}
}

func TestProxyProviderProfileLifecycle(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	defer db.Close()
	profile, err := db.CreateProxyProviderProfile(context.Background(), "品赞代理 1", "ipzan", "http", "https://service.ipzan.com/core-extract?no=1&secret=x")
	if err != nil {
		t.Fatalf("CreateProxyProviderProfile() error = %v", err)
	}
	profiles, err := db.ListProxyProviderProfiles(context.Background())
	if err != nil || len(profiles) != 1 || profiles[0].ID != profile.ID {
		t.Fatalf("ListProxyProviderProfiles() = %#v, %v", profiles, err)
	}
	profile, err = db.UpdateProxyProviderProfile(context.Background(), profile.ID, "济南套餐", "ipzan", "socks5", profile.APIURL)
	if err != nil || profile.Name != "济南套餐" || profile.ProxyType != "socks5" {
		t.Fatalf("UpdateProxyProviderProfile() = %#v, %v", profile, err)
	}
	if err := db.DeleteProxyProviderProfile(context.Background(), profile.ID); err != nil {
		t.Fatalf("DeleteProxyProviderProfile() error = %v", err)
	}
}

func TestAccountProxySettingMigratesExistingDatabase(t *testing.T) {
	path := filepath.Join(t.TempDir(), "old.db")
	old, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("sql.Open() error = %v", err)
	}
	_, err = old.Exec(`
CREATE TABLE account_proxy_settings (
  account_id INTEGER PRIMARY KEY,
  mode TEXT NOT NULL DEFAULT 'direct',
  proxy_type TEXT NOT NULL DEFAULT 'http',
  static_proxy TEXT NOT NULL DEFAULT '',
  api_url TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
INSERT INTO account_proxy_settings(account_id, mode, proxy_type, static_proxy, api_url, created_at, updated_at)
VALUES(1, 'api', 'http', '', 'https://proxy.example/get', 1, 1);`)
	if err != nil {
		_ = old.Close()
		t.Fatalf("create old schema error = %v", err)
	}
	if err := old.Close(); err != nil {
		t.Fatalf("close old database error = %v", err)
	}
	db, err := Open(path)
	if err != nil {
		t.Fatalf("Open(migrated) error = %v", err)
	}
	defer db.Close()
	setting, err := db.GetAccountProxySetting(context.Background(), 1)
	if err != nil || setting.Mode != "api" || setting.RefreshAheadSeconds != DefaultProxyRefreshAheadSeconds || setting.RegionCode != "" || setting.ProtocolProxyMode != "account" {
		t.Fatalf("migrated setting = %#v, %v", setting, err)
	}
}
