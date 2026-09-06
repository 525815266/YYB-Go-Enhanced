package store

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

const DefaultProxyRefreshAheadSeconds int64 = 300

type AccountProxySetting struct {
	AccountID           int64  `json:"account_id"`
	Mode                string `json:"mode"`
	ProxyType           string `json:"proxy_type"`
	StaticProxy         string `json:"static_proxy"`
	APIURL              string `json:"api_url"`
	ProviderProfileID   *int64 `json:"provider_profile_id,omitempty"`
	RegionCode          string `json:"region_code"`
	RegionProvince      string `json:"region_province"`
	RegionCity          string `json:"region_city"`
	RefreshAheadSeconds int64  `json:"refresh_ahead_seconds"`
	CreatedAt           int64  `json:"created_at"`
	UpdatedAt           int64  `json:"updated_at"`
}

func (db *DB) GetAccountProxySetting(ctx context.Context, accountID int64) (*AccountProxySetting, error) {
	setting := &AccountProxySetting{}
	err := db.sql.QueryRowContext(ctx, `
SELECT account_id, mode, proxy_type, static_proxy, api_url, provider_profile_id,
       region_code, region_province, region_city, refresh_ahead_seconds, created_at, updated_at
FROM account_proxy_settings WHERE account_id=?`, accountID).Scan(
		&setting.AccountID, &setting.Mode, &setting.ProxyType, &setting.StaticProxy,
		&setting.APIURL, &setting.ProviderProfileID, &setting.RegionCode, &setting.RegionProvince,
		&setting.RegionCity, &setting.RefreshAheadSeconds, &setting.CreatedAt, &setting.UpdatedAt,
	)
	return setting, err
}

func (db *DB) AccountProxySettingOrDefault(ctx context.Context, accountID int64) (*AccountProxySetting, error) {
	setting, err := db.GetAccountProxySetting(ctx, accountID)
	if err == nil {
		return setting, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	return &AccountProxySetting{AccountID: accountID, Mode: "direct", ProxyType: "http", RefreshAheadSeconds: DefaultProxyRefreshAheadSeconds}, nil
}

func (db *DB) UpsertAccountProxySetting(ctx context.Context, accountID int64, mode, proxyType, staticProxy, apiURL string, profileID *int64, regionCode, regionProvince, regionCity string, refreshAheadSeconds int64) (*AccountProxySetting, error) {
	if refreshAheadSeconds <= 0 {
		refreshAheadSeconds = DefaultProxyRefreshAheadSeconds
	}
	now := time.Now().Unix()
	_, err := db.sql.ExecContext(ctx, `
INSERT INTO account_proxy_settings
(account_id, mode, proxy_type, static_proxy, api_url, provider_profile_id, region_code,
 region_province, region_city, refresh_ahead_seconds, created_at, updated_at)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(account_id) DO UPDATE SET
mode=excluded.mode, proxy_type=excluded.proxy_type, static_proxy=excluded.static_proxy,
api_url=excluded.api_url, provider_profile_id=excluded.provider_profile_id,
region_code=excluded.region_code, region_province=excluded.region_province,
region_city=excluded.region_city, refresh_ahead_seconds=excluded.refresh_ahead_seconds,
updated_at=excluded.updated_at`,
		accountID, mode, proxyType, staticProxy, apiURL, profileID, regionCode,
		regionProvince, regionCity, refreshAheadSeconds, now, now,
	)
	if err != nil {
		return nil, err
	}
	return db.GetAccountProxySetting(ctx, accountID)
}

func (db *DB) CopyAccountProxySetting(ctx context.Context, sourceAccountID, targetAccountID int64) error {
	setting, err := db.AccountProxySettingOrDefault(ctx, sourceAccountID)
	if err != nil {
		return err
	}
	_, err = db.UpsertAccountProxySetting(ctx, targetAccountID, setting.Mode, setting.ProxyType,
		setting.StaticProxy, setting.APIURL, setting.ProviderProfileID, setting.RegionCode,
		setting.RegionProvince, setting.RegionCity, setting.RefreshAheadSeconds)
	return err
}

func (db *DB) CountAccountsUsingProxyProfile(ctx context.Context, profileID int64) (int, error) {
	var count int
	err := db.sql.QueryRowContext(ctx, "SELECT count(*) FROM account_proxy_settings WHERE provider_profile_id=?", profileID).Scan(&count)
	return count, err
}
