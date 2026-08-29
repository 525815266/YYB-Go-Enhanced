package httpapi

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/proxysource"
	"yyb_go/internal/qr"
	"yyb_go/internal/store"
)

type accountProxyIn struct {
	Product             string `json:"product"`
	Ref                 string `json:"ref"`
	Mode                string `json:"mode"`
	ProxyType           string `json:"proxy_type"`
	StaticProxy         string `json:"static_proxy"`
	APIURL              string `json:"api_url"`
	ProviderProfileID   *int64 `json:"provider_profile_id"`
	RegionCode          string `json:"region_code"`
	RegionProvince      string `json:"region_province"`
	RegionCity          string `json:"region_city"`
	RefreshAheadMinutes int64  `json:"refresh_ahead_minutes"`
}

const loginProductAppStore = "appstore"

func normalizeLoginProduct(product string) (string, error) {
	product = strings.ToLower(strings.TrimSpace(product))
	if product == "" {
		return loginProductAppStore, nil
	}
	if product != loginProductAppStore {
		return "", fmt.Errorf("登录产品 %q 尚未完成凭据兑换验证，目前仅支持 appstore", product)
	}
	return product, nil
}

type qrLoginSession struct {
	Session   *qr.Session
	Client    *qr.Client
	ProxySpec proxysource.Spec
	ProxyIn   accountProxyIn
	// Keep the freshly fetched image in memory so the image endpoint does not
	// depend on the QR cache directory being writable in a container.
	ImageBytes []byte
	// mu serializes cancellation with the final account write in confirm.
	mu        sync.Mutex
	cancelled bool
}

type accountProxyLease struct {
	Value            string
	SettingUpdatedAt int64
	ExpiresAt        time.Time
}

const accountProxyLeaseTTL = 45 * time.Second

func (body accountProxyIn) spec() proxysource.Spec {
	return proxysource.Spec{Mode: body.Mode, ProxyType: body.ProxyType, StaticProxy: body.StaticProxy, APIURL: body.APIURL}
}

func proxySpecFromSetting(setting *store.AccountProxySetting) proxysource.Spec {
	if setting == nil {
		return proxysource.Spec{Mode: "direct", ProxyType: "http"}
	}
	return proxysource.Spec{
		Mode: setting.Mode, ProxyType: setting.ProxyType,
		StaticProxy: setting.StaticProxy, APIURL: setting.APIURL,
	}
}

func proxySettingPublic(setting *store.AccountProxySetting, account *store.WechatAccount) map[string]any {
	expiresIn := protocol.CredentialsFromMap(account.Credentials).ExpiresIn
	tokenTTLMinutes := (expiresIn + 59) / 60
	dynamic := setting.Mode == "api"
	lifetimeClass := "stable"
	keepaliveSupported := true
	keepaliveNote := "可参与自动保活"
	if dynamic {
		lifetimeClass = "short"
		keepaliveSupported = false
		keepaliveNote = "短效代理，无法用于账号保活；仅用于扫码和临时请求"
	}
	return map[string]any{
		"account_id": setting.AccountID, "mode": setting.Mode, "proxy_type": setting.ProxyType,
		"static_proxy": setting.StaticProxy, "api_url": setting.APIURL,
		"provider_profile_id": setting.ProviderProfileID,
		"region_code":         setting.RegionCode, "region_province": setting.RegionProvince, "region_city": setting.RegionCity,
		"refresh_ahead_minutes": setting.RefreshAheadSeconds / 60,
		"token_ttl_minutes":     tokenTTLMinutes,
		"lifetime_class":        lifetimeClass, "keepalive_supported": keepaliveSupported, "keepalive_note": keepaliveNote,
		"configured": setting.Mode != "direct", "updated_at": setting.UpdatedAt,
	}
}

func (a *App) handleAccountProxy(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		acc, ok := a.resolveAccountFromQuery(w, r)
		if !ok {
			return
		}
		setting, err := a.db.AccountProxySettingOrDefault(r.Context(), acc.ID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, proxySettingPublic(setting, acc))
	case http.MethodPut:
		var body accountProxyIn
		if err := decodeOptionalJSON(r, &body); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		acc, ok := a.resolveAccountRef(w, r, body.Ref)
		if !ok {
			return
		}
		normalizedBody, normalized, err := a.normalizeAccountProxyInput(r.Context(), body)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		setting, err := a.saveAccountProxyInput(r.Context(), acc.ID, normalizedBody, normalized)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		if err := a.db.InvalidateAccountSessions(r.Context(), acc.ID); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		a.invalidateProxyLease(acc.ID)
		writeJSON(w, http.StatusOK, proxySettingPublic(setting, acc))
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (a *App) handleAccountProxyTest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body accountProxyIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	_, spec, err := a.normalizeAccountProxyInput(r.Context(), body)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if strings.TrimSpace(body.Ref) != "" && strings.TrimSpace(body.Mode) == "" {
		acc, ok := a.resolveAccountRef(w, r, body.Ref)
		if !ok {
			return
		}
		setting, err := a.db.AccountProxySettingOrDefault(r.Context(), acc.ID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		spec, err = a.proxySpecForSetting(r.Context(), setting)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	resolved, err := a.resolveProxySpec(r.Context(), spec)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	result := map[string]any{"resolved": resolved != "", "proxy": proxysource.Mask(resolved)}
	if resolved != "" {
		probe, probeErr := a.probeProxyExit(r.Context(), resolved)
		if probeErr != nil {
			result["probe_error"] = "出口检测请求失败，请重试"
		} else {
			result["exit_ip"] = probe.IP
			result["exit_country"] = probe.Country
			result["exit_region"] = probe.Region
			result["exit_city"] = probe.City
		}
	}
	writeJSON(w, http.StatusOK, result)
}

type proxyExitProbe struct {
	IP      string `json:"ip"`
	Country string `json:"country"`
	Region  string `json:"region"`
	City    string `json:"city"`
}

func (a *App) probeProxyExit(ctx context.Context, proxyValue string) (proxyExitProbe, error) {
	transport, err := protocol.NewHTTPTransport(proxyValue, false)
	if err != nil {
		return proxyExitProbe{}, err
	}
	client := &http.Client{Timeout: a.cfg.RequestTimeout, Transport: transport}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://api.ip.sb/geoip", nil)
	if err != nil {
		return proxyExitProbe{}, err
	}
	req.Header.Set("User-Agent", "YYB-Go proxy check")
	resp, err := client.Do(req)
	if err != nil {
		return proxyExitProbe{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return proxyExitProbe{}, fmt.Errorf("exit probe returned HTTP %d", resp.StatusCode)
	}
	var result proxyExitProbe
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64<<10)).Decode(&result); err != nil {
		return proxyExitProbe{}, err
	}
	if strings.TrimSpace(result.IP) == "" {
		return proxyExitProbe{}, fmt.Errorf("exit probe returned no IP")
	}
	return result, nil
}

func (a *App) resolveProxySpec(ctx context.Context, spec proxysource.Spec) (string, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	client := &http.Client{Timeout: a.cfg.RequestTimeout, Transport: transport}
	return proxysource.Resolve(ctx, client, spec)
}

func (a *App) resolveAccountProxy(ctx context.Context, accountID int64) (string, bool, error) {
	setting, err := a.db.GetAccountProxySetting(ctx, accountID)
	if errors.Is(err, sql.ErrNoRows) {
		if strings.TrimSpace(a.cfg.TCPProxy) == "" {
			return "", false, nil
		}
		normalized, normalizeErr := proxysource.NormalizeSpec(proxysource.Spec{Mode: "static", StaticProxy: a.cfg.TCPProxy})
		if normalizeErr != nil {
			return "", false, normalizeErr
		}
		return normalized.StaticProxy, true, nil
	}
	if err != nil {
		return "", false, err
	}
	spec, err := a.proxySpecForSetting(ctx, setting)
	if err != nil {
		return "", false, err
	}
	if spec.Mode != "api" {
		proxyValue, err := a.resolveProxySpec(ctx, spec)
		return proxyValue, false, err
	}
	leaseLock := a.proxyLeaseLockFor(accountID)
	leaseLock.Lock()
	defer leaseLock.Unlock()
	a.proxyMu.Lock()
	if lease, ok := a.proxyLeases[accountID]; ok && lease.SettingUpdatedAt == setting.UpdatedAt && time.Now().Before(lease.ExpiresAt) {
		a.proxyMu.Unlock()
		return lease.Value, false, nil
	}
	a.proxyMu.Unlock()
	proxyValue, err := a.resolveProxySpec(ctx, spec)
	a.proxyMu.Lock()
	defer a.proxyMu.Unlock()
	if err == nil && proxyValue != "" {
		a.proxyLeases[accountID] = accountProxyLease{Value: proxyValue, SettingUpdatedAt: setting.UpdatedAt, ExpiresAt: time.Now().Add(accountProxyLeaseTTL)}
	} else {
		delete(a.proxyLeases, accountID)
	}
	return proxyValue, false, err
}

func (a *App) proxyLeaseLockFor(accountID int64) *sync.Mutex {
	a.proxyLeaseLocksMu.Lock()
	defer a.proxyLeaseLocksMu.Unlock()
	if lock := a.proxyLeaseLocks[accountID]; lock != nil {
		return lock
	}
	lock := &sync.Mutex{}
	a.proxyLeaseLocks[accountID] = lock
	return lock
}

func (a *App) invalidateProxyLease(accountID int64) {
	a.proxyMu.Lock()
	delete(a.proxyLeases, accountID)
	a.proxyMu.Unlock()
}

func (a *App) qrClientForSpec(ctx context.Context, spec proxysource.Spec) (*qr.Client, string, error) {
	resolved, err := a.resolveProxySpec(ctx, spec)
	if err != nil {
		return nil, "", err
	}
	if resolved == "" {
		return a.qr, "", nil
	}
	client, err := qr.NewClientWithProxy(a.cfg.RequestTimeout, resolved, false)
	if err != nil {
		return nil, "", fmt.Errorf("创建代理客户端失败: %w", err)
	}
	return client, resolved, nil
}

func (a *App) saveAccountProxyInput(ctx context.Context, accountID int64, body accountProxyIn, normalized proxysource.Spec) (*store.AccountProxySetting, error) {
	refreshAheadSeconds := body.RefreshAheadMinutes * 60
	apiURL := normalized.APIURL
	if body.ProviderProfileID != nil {
		apiURL = ""
	}
	return a.db.UpsertAccountProxySetting(ctx, accountID, normalized.Mode, normalized.ProxyType,
		normalized.StaticProxy, apiURL, body.ProviderProfileID,
		strings.TrimSpace(body.RegionCode), strings.TrimSpace(body.RegionProvince), strings.TrimSpace(body.RegionCity),
		refreshAheadSeconds)
}

func (a *App) accountExistsBeforeScan(ctx context.Context, openID string) (bool, error) {
	_, err := a.db.GetAccountByOpenID(ctx, openID)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func (a *App) saveNewAccountProxy(ctx context.Context, accountID int64, existed bool, body accountProxyIn, normalized proxysource.Spec) error {
	if existed {
		return nil
	}
	_, err := a.saveAccountProxyInput(ctx, accountID, body, normalized)
	return err
}
