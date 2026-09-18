package httpapi

import (
	"context"
	"crypto/md5"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"

	"yyb_go/internal/proxysource"
	"yyb_go/internal/store"
)

const ipzanHost = "service.ipzan.com"

const (
	juliangProfileScheme = "juliang"
	juliangAPIEndpoint   = "http://v2.api.juliangip.com/company/dynamic/getips"
)

var (
	ipzanProvinceAreasEndpoint = "https://service.ipzan.com/area-get-province"
	ipzanCityAreasEndpoint     = "https://service.ipzan.com/area-find-citys?province="
)

type proxyProfileIn struct {
	Name              string `json:"name"`
	Provider          string `json:"provider"`
	ProxyType         string `json:"proxy_type"`
	APIURL            string `json:"api_url"`
	AuthorizationMode string `json:"authorization_mode"`
	TradeNo           string `json:"trade_no"`
	APIKey            string `json:"api_key"`
}

type proxyArea struct {
	Code string `json:"code"`
	Name string `json:"name"`
}

func (a *App) handleProxyProfiles(w http.ResponseWriter, r *http.Request) {
	path := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/proxy-profiles"), "/")
	if path == "areas/provinces" {
		a.handleIPZanAreas(w, r, ipzanProvinceAreasEndpoint)
		return
	}
	if path == "areas/cities" {
		province := strings.TrimSpace(r.URL.Query().Get("province"))
		if len(province) != 6 || !digitsOnly(province) {
			writeError(w, http.StatusBadRequest, "province must be a 6-digit area code")
			return
		}
		a.handleIPZanAreas(w, r, ipzanCityAreasEndpoint+url.QueryEscape(province))
		return
	}
	if path == "" {
		a.handleProxyProfileCollection(w, r)
		return
	}
	id, err := strconv.ParseInt(path, 10, 64)
	if err != nil || id <= 0 {
		writeError(w, http.StatusNotFound, "proxy profile not found")
		return
	}
	a.handleProxyProfileItem(w, r, id)
}

func (a *App) handleProxyProfileCollection(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && a.auth != nil && !requireAdmin(w, r) {
		return
	}
	switch r.Method {
	case http.MethodGet:
		profiles, err := a.db.ListProxyProviderProfiles(r.Context())
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, profiles)
	case http.MethodPost:
		body, ok := decodeProxyProfile(w, r)
		if !ok {
			return
		}
		profile, err := a.db.CreateProxyProviderProfile(r.Context(), body.Name, body.Provider, body.ProxyType, body.APIURL)
		if err != nil {
			writeProxyProfileStoreError(w, err)
			return
		}
		writeJSON(w, http.StatusCreated, profile)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (a *App) handleProxyProfileItem(w http.ResponseWriter, r *http.Request, id int64) {
	if a.auth != nil && !requireAdmin(w, r) {
		return
	}
	switch r.Method {
	case http.MethodPut:
		body, ok := decodeProxyProfile(w, r)
		if !ok {
			return
		}
		profile, err := a.db.UpdateProxyProviderProfile(r.Context(), id, body.Name, body.Provider, body.ProxyType, body.APIURL)
		if errors.Is(err, sql.ErrNoRows) {
			writeError(w, http.StatusNotFound, "proxy profile not found")
			return
		}
		if err != nil {
			writeProxyProfileStoreError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, profile)
	case http.MethodDelete:
		count, err := a.db.CountAccountsUsingProxyProfile(r.Context(), id)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		if count > 0 {
			writeError(w, http.StatusConflict, fmt.Sprintf("该配置仍被 %d 个账号使用，请先切换这些账号", count))
			return
		}
		if _, err := a.db.GetProxyProviderProfile(r.Context(), id); errors.Is(err, sql.ErrNoRows) {
			writeError(w, http.StatusNotFound, "proxy profile not found")
			return
		} else if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		if err := a.db.DeleteProxyProviderProfile(r.Context(), id); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"deleted": id})
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func decodeProxyProfile(w http.ResponseWriter, r *http.Request) (proxyProfileIn, bool) {
	var body proxyProfileIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return proxyProfileIn{}, false
	}
	body.Name = strings.TrimSpace(body.Name)
	body.Provider = strings.ToLower(strings.TrimSpace(body.Provider))
	body.ProxyType = strings.ToLower(strings.TrimSpace(body.ProxyType))
	body.AuthorizationMode = strings.ToLower(strings.TrimSpace(body.AuthorizationMode))
	if body.Provider == "" {
		body.Provider = "ipzan"
	}
	if body.ProxyType == "" {
		body.ProxyType = "http"
	}
	if body.Name == "" || len([]rune(body.Name)) > 50 {
		writeError(w, http.StatusBadRequest, "配置名称不能为空且不能超过 50 个字符")
		return proxyProfileIn{}, false
	}
	var apiURL string
	var err error
	switch body.Provider {
	case "ipzan":
		apiURL, err = normalizeIPZanURL(body.APIURL, body.ProxyType, body.AuthorizationMode)
	case "juliang":
		apiURL, err = normalizeJuliangProfile(body.APIURL, body.TradeNo, body.APIKey, body.AuthorizationMode)
	case "static":
		var normalized proxysource.Spec
		normalized, err = proxysource.NormalizeSpec(proxysource.Spec{
			Mode: "static", ProxyType: body.ProxyType, StaticProxy: body.APIURL,
		})
		if err == nil {
			apiURL = normalized.StaticProxy
		}
	default:
		err = fmt.Errorf("代理配置类型必须为 static、ipzan 或 juliang")
	}
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return proxyProfileIn{}, false
	}
	body.APIURL = apiURL
	return body, true
}

func normalizeJuliangProfile(raw, tradeNo, apiKey, authorizationMode string) (string, error) {
	tradeNo = strings.TrimSpace(tradeNo)
	apiKey = strings.TrimSpace(apiKey)
	authorizationMode = strings.ToLower(strings.TrimSpace(authorizationMode))
	if tradeNo == "" || apiKey == "" {
		u, err := url.Parse(strings.TrimSpace(raw))
		if err == nil && u.Scheme == juliangProfileScheme && u.Host == "company" && u.Path == "/dynamic" {
			query := u.Query()
			tradeNo = strings.TrimSpace(query.Get("trade_no"))
			apiKey = strings.TrimSpace(query.Get("key"))
			if authorizationMode == "" {
				authorizationMode = strings.TrimSpace(query.Get("mode"))
			}
		}
	}
	if len(tradeNo) < 8 || len(tradeNo) > 32 || !digitsOnly(tradeNo) {
		return "", fmt.Errorf("巨量业务编号必须为 8 到 32 位数字")
	}
	if len(apiKey) != 32 || !hexOnly(apiKey) {
		return "", fmt.Errorf("巨量 API Key 必须为 32 位十六进制字符串")
	}
	if authorizationMode == "" {
		authorizationMode = "auth"
	}
	if authorizationMode != "auth" && authorizationMode != "whitelist" {
		return "", fmt.Errorf("巨量授权方式必须为 auth 或 whitelist")
	}
	u := &url.URL{Scheme: juliangProfileScheme, Host: "company", Path: "/dynamic"}
	query := u.Query()
	query.Set("trade_no", tradeNo)
	query.Set("key", strings.ToLower(apiKey))
	query.Set("mode", authorizationMode)
	u.RawQuery = query.Encode()
	return u.String(), nil
}

func normalizeIPZanURL(raw, proxyType, authorizationMode string) (string, error) {
	if proxyType != "http" && proxyType != "socks5" {
		return "", fmt.Errorf("代理类型必须为 http 或 socks5")
	}
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme != "https" || !strings.EqualFold(u.Hostname(), ipzanHost) || u.Port() != "" || u.User != nil || u.Path != "/core-extract" {
		return "", fmt.Errorf("请填写 service.ipzan.com/core-extract 的 HTTPS 提取链接")
	}
	query := u.Query()
	if strings.TrimSpace(query.Get("no")) == "" || strings.TrimSpace(query.Get("secret")) == "" {
		return "", fmt.Errorf("品赞提取链接必须包含 no 和 secret")
	}
	query.Del("area")
	query.Set("num", "1")
	if authorizationMode == "" {
		if strings.EqualFold(strings.TrimSpace(query.Get("mode")), "auth") {
			authorizationMode = "auth"
		} else {
			authorizationMode = "whitelist"
		}
	}
	switch authorizationMode {
	case "auth":
		query.Set("mode", "auth")
		query.Set("format", "json")
	case "whitelist":
		query.Del("mode")
	default:
		return "", fmt.Errorf("品赞授权方式必须为 whitelist 或 auth")
	}
	if strings.TrimSpace(query.Get("format")) == "" {
		query.Set("format", "json")
	}
	if proxyType == "socks5" {
		query.Set("protocol", "3")
	} else {
		query.Set("protocol", "1")
	}
	u.RawQuery = query.Encode()
	u.Fragment = ""
	return u.String(), nil
}

func ipzanURLForRegion(profile *store.ProxyProviderProfile, regionCode string) (string, error) {
	base, err := normalizeIPZanURL(profile.APIURL, profile.ProxyType, "")
	if err != nil {
		return "", err
	}
	u, _ := url.Parse(base)
	query := u.Query()
	regionCode = strings.TrimSpace(regionCode)
	if regionCode != "" && regionCode != "all" {
		if len(regionCode) != 6 || !digitsOnly(regionCode) {
			return "", fmt.Errorf("品赞地区编码必须为 6 位数字")
		}
		query.Set("area", regionCode)
	} else {
		query.Del("area")
	}
	u.RawQuery = query.Encode()
	return u.String(), nil
}

func juliangURLForRegion(profile *store.ProxyProviderProfile, province, city string) (string, error) {
	internalURL, err := normalizeJuliangProfile(profile.APIURL, "", "", "")
	if err != nil {
		return "", err
	}
	u, _ := url.Parse(internalURL)
	profileQuery := u.Query()
	params := map[string]string{
		"trade_no":    profileQuery.Get("trade_no"),
		"num":         "1",
		"pt":          "1",
		"result_type": "json2",
	}
	if strings.EqualFold(profile.ProxyType, "socks5") {
		params["pt"] = "2"
	}
	if profileQuery.Get("mode") == "whitelist" {
		params["auto_white"] = "1"
	} else {
		params["auth_type"] = "2"
	}
	if province = normalizeRegionName(province); province != "" {
		params["province"] = province
	}
	if city = normalizeRegionName(city); city != "" {
		params["city"] = city
	}
	params["sign"] = juliangSign(params, profileQuery.Get("key"))
	endpoint, _ := url.Parse(juliangAPIEndpoint)
	query := endpoint.Query()
	for key, value := range params {
		query.Set(key, value)
	}
	endpoint.RawQuery = query.Encode()
	return endpoint.String(), nil
}

func proxyProfileURLForRegion(profile *store.ProxyProviderProfile, regionCode, province, city string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(profile.Provider)) {
	case "ipzan", "":
		return ipzanURLForRegion(profile, regionCode)
	case "juliang":
		return juliangURLForRegion(profile, province, city)
	default:
		return "", fmt.Errorf("不支持的代理供应商: %s", profile.Provider)
	}
}

func normalizeRegionName(value string) string {
	value = strings.TrimSpace(value)
	for _, suffix := range []string{"壮族自治区", "回族自治区", "维吾尔自治区", "特别行政区", "自治区", "省", "市"} {
		value = strings.TrimSuffix(value, suffix)
	}
	return value
}

func juliangSign(params map[string]string, apiKey string) string {
	keys := make([]string, 0, len(params))
	for key := range params {
		if key != "sign" {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys)+1)
	for _, key := range keys {
		parts = append(parts, key+"="+params[key])
	}
	parts = append(parts, "key="+apiKey)
	sum := md5.Sum([]byte(strings.Join(parts, "&")))
	return fmt.Sprintf("%x", sum)
}

func (a *App) normalizeAccountProxyInput(ctx context.Context, body accountProxyIn) (accountProxyIn, proxysource.Spec, error) {
	body.RegionCode = strings.TrimSpace(body.RegionCode)
	body.RegionProvince = strings.TrimSpace(body.RegionProvince)
	body.RegionCity = strings.TrimSpace(body.RegionCity)
	if body.RefreshAheadMinutes == 0 {
		body.RefreshAheadMinutes = store.DefaultProxyRefreshAheadSeconds / 60
	}
	if body.RefreshAheadMinutes < 5 || body.RefreshAheadMinutes > 90 {
		return accountProxyIn{}, proxysource.Spec{}, fmt.Errorf("代理账号提前刷新时间必须在 5 到 90 分钟之间")
	}
	if body.ProviderProfileID != nil {
		profile, err := a.db.GetProxyProviderProfile(ctx, *body.ProviderProfileID)
		if errors.Is(err, sql.ErrNoRows) {
			return accountProxyIn{}, proxysource.Spec{}, fmt.Errorf("品赞代理配置不存在")
		}
		if err != nil {
			return accountProxyIn{}, proxysource.Spec{}, err
		}
		profileMode := "api"
		profileValue := ""
		if strings.EqualFold(profile.Provider, "static") {
			profileMode = "static"
			profileValue = profile.APIURL
		} else {
			apiURL, err := proxyProfileURLForRegion(profile, body.RegionCode, body.RegionProvince, body.RegionCity)
			if err != nil {
				return accountProxyIn{}, proxysource.Spec{}, err
			}
			profileValue = apiURL
		}
		body.Mode = profileMode
		body.ProxyType = profile.ProxyType
		body.StaticProxy = ""
		body.APIURL = ""
		profileSpec := proxysource.Spec{Mode: profileMode, ProxyType: profile.ProxyType}
		if profileMode == "static" {
			profileSpec.StaticProxy = profileValue
		} else {
			profileSpec.APIURL = profileValue
		}
		normalized, err := proxysource.NormalizeSpec(profileSpec)
		return body, normalized, err
	}
	normalized, err := proxysource.NormalizeSpec(body.spec())
	return body, normalized, err
}

func (a *App) proxySpecForSetting(ctx context.Context, setting *store.AccountProxySetting) (proxysource.Spec, error) {
	if setting == nil || setting.ProviderProfileID == nil {
		return proxysource.NormalizeSpec(proxySpecFromSetting(setting))
	}
	profile, err := a.db.GetProxyProviderProfile(ctx, *setting.ProviderProfileID)
	if err != nil {
		return proxysource.Spec{}, err
	}
	if strings.EqualFold(profile.Provider, "static") {
		return proxysource.NormalizeSpec(proxysource.Spec{Mode: "static", ProxyType: profile.ProxyType, StaticProxy: profile.APIURL})
	}
	apiURL, err := proxyProfileURLForRegion(profile, setting.RegionCode, setting.RegionProvince, setting.RegionCity)
	if err != nil {
		return proxysource.Spec{}, err
	}
	return proxysource.NormalizeSpec(proxysource.Spec{Mode: "api", ProxyType: profile.ProxyType, APIURL: apiURL})
}

func (a *App) handleIPZanAreas(w http.ResponseWriter, r *http.Request, endpoint string) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	areas, err := a.fetchProxyAreas(r.Context(), endpoint)
	if err != nil {
		writeError(w, http.StatusBadGateway, "读取品赞地区失败: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, areas)
}

func (a *App) fetchProxyAreas(ctx context.Context, endpoint string) ([]proxyArea, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	client := &http.Client{Timeout: a.cfg.RequestTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("品赞地区接口返回 HTTP %d", resp.StatusCode)
	}
	var payload struct {
		Data []proxyArea `json:"data"`
	}
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 1<<20))
	if err := decoder.Decode(&payload); err != nil {
		return nil, fmt.Errorf("无法解析品赞地区响应")
	}
	return payload.Data, nil
}

func writeProxyProfileStoreError(w http.ResponseWriter, err error) {
	if strings.Contains(strings.ToLower(err.Error()), "unique") {
		writeError(w, http.StatusConflict, "代理配置名称已存在")
		return
	}
	writeError(w, http.StatusInternalServerError, err.Error())
}

func digitsOnly(value string) bool {
	for _, char := range value {
		if char < '0' || char > '9' {
			return false
		}
	}
	return value != ""
}

func hexOnly(value string) bool {
	for _, char := range value {
		if (char < '0' || char > '9') && (char < 'a' || char > 'f') && (char < 'A' || char > 'F') {
			return false
		}
	}
	return value != ""
}
