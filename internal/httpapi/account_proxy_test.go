package httpapi

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/proxysource"
	"yyb_go/internal/store"
)

func TestAccountProxyAPIAndDirectOverride(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{
		ResourceRoot:   t.TempDir(),
		RequestTimeout: time.Second,
		TCPProxy:       "http-connect://global.example:8080",
	})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	status := "alive"
	account, err := app.db.UpsertAccount(context.Background(), "proxy-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}

	proxyValue, fallbackDirect, err := app.resolveAccountProxy(context.Background(), account.ID)
	if err != nil || proxyValue != "http-connect://global.example:8080" || !fallbackDirect {
		t.Fatalf("global proxy = %q, fallback=%v, err=%v", proxyValue, fallbackDirect, err)
	}

	handler := app.Handler()
	response := apiRequest(t, handler, http.MethodPut, "/accounts/proxy", map[string]any{
		"ref": fmt.Sprint(account.ID), "mode": "direct", "proxy_type": "http",
	})
	if response.Code != http.StatusOK {
		t.Fatalf("PUT /accounts/proxy status = %d body=%s", response.Code, response.Body.String())
	}
	proxyValue, fallbackDirect, err = app.resolveAccountProxy(context.Background(), account.ID)
	if err != nil || proxyValue != "" || fallbackDirect {
		t.Fatalf("direct override = %q, fallback=%v, err=%v", proxyValue, fallbackDirect, err)
	}
}

func TestProxySettingPublicUsesAccountTokenTTL(t *testing.T) {
	setting := &store.AccountProxySetting{AccountID: 7, Mode: "api", ProxyType: "http", RefreshAheadSeconds: 300}
	account := &store.WechatAccount{Credentials: map[string]any{"expires_in": float64(5400)}}
	result := proxySettingPublic(setting, account)
	if result["token_ttl_minutes"] != int64(90) {
		t.Fatalf("token_ttl_minutes = %#v, want 90", result["token_ttl_minutes"])
	}
	if result["keepalive_supported"] != false || result["lifetime_class"] != "short" || result["proxy_warning"] == "" {
		t.Fatalf("dynamic proxy status = %#v, want explicit short-lived warning", result)
	}
	stable := proxySettingPublic(&store.AccountProxySetting{AccountID: 7, Mode: "static", ProxyType: "http"}, account)
	if stable["keepalive_supported"] != true || stable["proxy_warning"] != "" {
		t.Fatalf("stable proxy status = %#v, want keepalive enabled without warning", stable)
	}
}

func TestAccountProxyAPIParsesJSON2AndCascades(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	proxyAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"success":true,"data":{"proxy_list":[{"server_ip":"203.0.113.30","proxy_port":"9010","account":"city-user","pwd":"city-pass"}]}}`))
	}))
	defer proxyAPI.Close()

	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	status := "alive"
	account, err := app.db.UpsertAccount(context.Background(), "json2-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	handler := app.Handler()
	payload := map[string]any{
		"ref": fmt.Sprint(account.ID), "mode": "api", "proxy_type": "socks5", "api_url": proxyAPI.URL + "?province=山东&city=济南",
	}
	if response := apiRequest(t, handler, http.MethodPut, "/accounts/proxy", payload); response.Code != http.StatusOK {
		t.Fatalf("PUT /accounts/proxy status = %d body=%s", response.Code, response.Body.String())
	}
	tested := apiRequest(t, handler, http.MethodPost, "/accounts/proxy/test", payload)
	if tested.Code != http.StatusOK {
		t.Fatalf("POST /accounts/proxy/test status = %d body=%s", tested.Code, tested.Body.String())
	}
	var result struct {
		Data struct {
			Resolved bool   `json:"resolved"`
			Proxy    string `json:"proxy"`
		} `json:"data"`
	}
	if err := json.Unmarshal(tested.Body.Bytes(), &result); err != nil {
		t.Fatalf("decode proxy test response: %v", err)
	}
	if !result.Data.Resolved || result.Data.Proxy != "socks5://203.0.113.30:9010" {
		t.Fatalf("proxy test response = %#v", result.Data)
	}

	if err := app.db.DeleteAccount(context.Background(), account.ID); err != nil {
		t.Fatalf("DeleteAccount() error = %v", err)
	}
	if _, err := app.db.GetAccountProxySetting(context.Background(), account.ID); !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("proxy setting after account deletion error = %v", err)
	}
}

func TestDynamicProxyLeaseAvoidsRepeatedExtraction(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	proxyCalls := 0
	proxyAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		proxyCalls++
		_, _ = w.Write([]byte(`{"data":{"ip":"203.0.113.31","port":8080}}`))
	}))
	defer proxyAPI.Close()
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	status := "alive"
	account, err := app.db.UpsertAccount(context.Background(), "lease-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	if _, err := app.db.UpsertAccountProxySetting(context.Background(), account.ID, "api", "http", "", proxyAPI.URL, nil, "", "", "", 300); err != nil {
		t.Fatalf("UpsertAccountProxySetting() error = %v", err)
	}
	for range 2 {
		proxyValue, _, err := app.resolveAccountProxy(context.Background(), account.ID)
		if err != nil || proxyValue != "http-connect://203.0.113.31:8080" {
			t.Fatalf("resolveAccountProxy() = %q, %v", proxyValue, err)
		}
	}
	if proxyCalls != 1 {
		t.Fatalf("proxy API calls = %d, want 1", proxyCalls)
	}
	app.invalidateProxyLease(account.ID)
	if _, _, err := app.resolveAccountProxy(context.Background(), account.ID); err != nil {
		t.Fatalf("resolve after invalidation: %v", err)
	}
	if proxyCalls != 2 {
		t.Fatalf("proxy API calls after invalidation = %d, want 2", proxyCalls)
	}
}

func TestIPZanProfilesCanBeNamedAndBoundByRegion(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	status := "alive"
	account, err := app.db.UpsertAccount(context.Background(), "ipzan-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	handler := app.Handler()
	created := apiRequest(t, handler, http.MethodPost, "/api/proxy-profiles", map[string]any{
		"name": "品赞代理 1", "provider": "ipzan", "proxy_type": "http",
		"authorization_mode": "auth",
		"api_url":            "https://service.ipzan.com/core-extract?no=123&secret=test&format=txt&area=110000",
	})
	if created.Code != http.StatusCreated {
		t.Fatalf("POST /api/proxy-profiles status = %d body=%s", created.Code, created.Body.String())
	}
	var profileResponse struct {
		Data struct {
			ID int64 `json:"id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(created.Body.Bytes(), &profileResponse); err != nil || profileResponse.Data.ID == 0 {
		t.Fatalf("decode profile response = %#v, %v", profileResponse, err)
	}
	profileID := profileResponse.Data.ID
	saved := apiRequest(t, handler, http.MethodPut, "/accounts/proxy", map[string]any{
		"ref": fmt.Sprint(account.ID), "mode": "api", "provider_profile_id": profileID,
		"region_code": "370100", "region_province": "山东省", "region_city": "济南市",
		"refresh_ahead_minutes": 15,
	})
	if saved.Code != http.StatusOK {
		t.Fatalf("PUT /accounts/proxy status = %d body=%s", saved.Code, saved.Body.String())
	}
	setting, err := app.db.GetAccountProxySetting(context.Background(), account.ID)
	if err != nil || setting.ProviderProfileID == nil || *setting.ProviderProfileID != profileID || setting.APIURL != "" || setting.RegionCode != "370100" || setting.RefreshAheadSeconds != 900 {
		t.Fatalf("saved profile setting = %#v, %v", setting, err)
	}
	spec, err := app.proxySpecForSetting(context.Background(), setting)
	if err != nil || !strings.Contains(spec.APIURL, "area=370100") || !strings.Contains(spec.APIURL, "protocol=1") || !strings.Contains(spec.APIURL, "mode=auth") || !strings.Contains(spec.APIURL, "format=json") || strings.Contains(spec.APIURL, "area=110000") {
		t.Fatalf("resolved profile spec = %#v, %v", spec, err)
	}
	deleted := apiRequest(t, handler, http.MethodDelete, fmt.Sprintf("/api/proxy-profiles/%d", profileID), nil)
	if deleted.Code != http.StatusConflict {
		t.Fatalf("DELETE bound profile status = %d body=%s", deleted.Code, deleted.Body.String())
	}
}

func TestJuliangProfilesSignEachAccountRegion(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	status := "alive"
	account, err := app.db.UpsertAccount(context.Background(), "juliang-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	handler := app.Handler()
	created := apiRequest(t, handler, http.MethodPost, "/api/proxy-profiles", map[string]any{
		"name": "巨量代理 1", "provider": "juliang", "proxy_type": "http",
		"authorization_mode": "auth",
		"trade_no":           "1234567890123456",
		"api_key":            "0123456789abcdef0123456789abcdef",
	})
	if created.Code != http.StatusCreated {
		t.Fatalf("POST juliang profile status = %d body=%s", created.Code, created.Body.String())
	}
	var profileResponse struct {
		Data store.ProxyProviderProfile `json:"data"`
	}
	if err := json.Unmarshal(created.Body.Bytes(), &profileResponse); err != nil || profileResponse.Data.ID == 0 {
		t.Fatalf("decode juliang profile = %#v, %v", profileResponse, err)
	}
	if !strings.HasPrefix(profileResponse.Data.APIURL, "juliang://company/dynamic?") {
		t.Fatalf("stored juliang profile URL = %q", profileResponse.Data.APIURL)
	}
	saved := apiRequest(t, handler, http.MethodPut, "/accounts/proxy", map[string]any{
		"ref": fmt.Sprint(account.ID), "mode": "api", "provider_profile_id": profileResponse.Data.ID,
		"region_code": "370700", "region_province": "山东省", "region_city": "潍坊市",
		"refresh_ahead_minutes": 5,
	})
	if saved.Code != http.StatusOK {
		t.Fatalf("PUT juliang account proxy status = %d body=%s", saved.Code, saved.Body.String())
	}
	setting, err := app.db.GetAccountProxySetting(context.Background(), account.ID)
	if err != nil {
		t.Fatalf("GetAccountProxySetting() error = %v", err)
	}
	spec, err := app.proxySpecForSetting(context.Background(), setting)
	if err != nil {
		t.Fatalf("proxySpecForSetting() error = %v", err)
	}
	u, err := url.Parse(spec.APIURL)
	if err != nil {
		t.Fatalf("parse juliang extraction URL: %v", err)
	}
	query := u.Query()
	if u.Scheme != "http" || u.Host != "v2.api.juliangip.com" || query.Get("province") != "山东" || query.Get("city") != "潍坊" || query.Get("area") != "" || query.Get("auth_type") != "2" || query.Get("result_type") != "json2" || len(query.Get("sign")) != 32 {
		t.Fatalf("resolved juliang spec = %s", spec.APIURL)
	}
}

func TestExistingAccountRescanPreservesProxySetting(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	status := "expired"
	account, err := app.db.UpsertAccount(context.Background(), "rescan-openid", "old-buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	profile, err := app.db.CreateProxyProviderProfile(context.Background(), "品赞代理 2", "ipzan", "http", "https://service.ipzan.com/core-extract?no=2&secret=x")
	if err != nil {
		t.Fatalf("CreateProxyProviderProfile() error = %v", err)
	}
	if _, err := app.db.UpsertAccountProxySetting(context.Background(), account.ID, "api", "http", "", "", &profile.ID, "370100", "山东省", "济南市", 300); err != nil {
		t.Fatalf("UpsertAccountProxySetting() error = %v", err)
	}
	direct, _ := proxysource.NormalizeSpec(proxysource.Spec{Mode: "direct"})
	if err := app.saveNewAccountProxy(context.Background(), account.ID, true, accountProxyIn{Mode: "direct"}, direct); err != nil {
		t.Fatalf("saveNewAccountProxy() error = %v", err)
	}
	setting, err := app.db.GetAccountProxySetting(context.Background(), account.ID)
	if err != nil || setting.ProviderProfileID == nil || *setting.ProviderProfileID != profile.ID || setting.RegionCode != "370100" {
		t.Fatalf("proxy after rescan = %#v, %v", setting, err)
	}
}

func TestWXAppCallUsesExistingLoginBufferBeforeRefreshing(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	status := "alive"
	account, err := app.db.UpsertAccount(context.Background(), "reuse-login-buffer", "buffer", nil, nil, nil, nil, map[string]any{"refreshtoken": "refresh"}, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	refreshCalls := 0
	app.refreshLoginBuffer = func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferResult, error) {
		refreshCalls++
		return protocol.LoginBufferResult{}, errors.New("refresh should not run")
	}
	callCount := 0
	result, err := app.invokeWXApp(context.Background(), account, "wx0000000000000000", nil, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		callCount++
		return map[string]any{"code": "ok"}, nil
	})
	if err != nil || result["code"] != "ok" || callCount != 1 || refreshCalls != 0 {
		t.Fatalf("invokeWXApp() result=%#v err=%v calls=%d refreshes=%d", result, err, callCount, refreshCalls)
	}
}

func TestExpiredAccountRejectsBeforeResolvingProxy(t *testing.T) {
	t.Setenv("GIN_MODE", "test")
	proxyCalls := 0
	proxyAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		proxyCalls++
		_, _ = w.Write([]byte("203.0.113.10:8080"))
	}))
	defer proxyAPI.Close()
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	status := "expired"
	account, err := app.db.UpsertAccount(context.Background(), "expired-openid", "buffer", nil, nil, nil, nil, nil, &status)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	if _, err := app.db.UpsertAccountProxySetting(context.Background(), account.ID, "api", "http", "", proxyAPI.URL, nil, "", "", "", 300); err != nil {
		t.Fatalf("UpsertAccountProxySetting() error = %v", err)
	}
	callCount := 0
	_, err = app.invokeWXApp(context.Background(), account, "wx0000000000000000", nil, func(context.Context, *store.WechatAccount, string, map[string]any, string, bool) (map[string]any, error) {
		callCount++
		return map[string]any{}, nil
	})
	var expired accountExpiredError
	if !errors.As(err, &expired) || proxyCalls != 0 || callCount != 0 {
		t.Fatalf("invokeWXApp() err=%v proxyCalls=%d callCount=%d", err, proxyCalls, callCount)
	}
}
