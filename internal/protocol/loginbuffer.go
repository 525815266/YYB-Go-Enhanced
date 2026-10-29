package protocol

import (
	"bytes"
	"context"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"strconv"
	"strings"
	"time"
)

var ErrMissingRefreshToken = errors.New("missing refresh token")

type RefreshRejectedError struct {
	Code      int
	Message   string
	Operation string
}

func (e *RefreshRejectedError) Error() string {
	return fmt.Sprintf("refresh failed: code=%d msg=%s", e.Code, e.Message)
}

// LoginBufferRejectedError identifies a business rejection from the login
// buffer endpoint, separate from proxy, HTTP and JSON decoding failures.
type LoginBufferRejectedError struct {
	Code    int
	Message string
}

func (e *LoginBufferRejectedError) Error() string {
	return fmt.Sprintf("login_buffer failed: code=%d msg=%s", e.Code, e.Message)
}

const (
	yybHost         = "https://yybadaccess.3g.qq.com"
	loginBufferURL  = yybHost + "/pc_yyb_auth/pcyyb_get_wx_login_buffer_auth"
	refreshTokenURL = yybHost + "/pc_yyb_auth/pcyyb_refresh_token_auth"
	mainRefreshURL  = yybHost + "/pc_yyb/pcyyb_refresh_token"
	checkLoginURL   = yybHost + "/pc_yyb/pcyyb_check_login"
	userInfoURL     = yybHost + "/pc_yyb/pcyyb_get_user_info"
	loginBusinessID = "pc_yyb_auth"
	loginAccessKey  = "wgrdg373hy26ww2"
)

type LoginBufferCredentials struct {
	OpenID                 string `json:"openid"`
	AccessToken            string `json:"accesstoken"`
	RefreshToken           string `json:"refreshtoken"`
	RefreshTokenObservedAt int64  `json:"refresh_token_observed_at"`
	RefreshBusinessID      string `json:"refresh_business_id"`
	LoginType              string `json:"logintype"`
	Nickname               string `json:"nickname"`
	ExpiresAt              int64  `json:"expires_at"`
	ExpiresIn              int64  `json:"expires_in"`
}

func CredentialsFromMap(m map[string]any) LoginBufferCredentials {
	expiresIn := defaultInt64(int64FromMap(m, "expires_in"), 7200)
	expiresAt := int64FromMap(m, "expires_at")
	if expiresAt <= 0 {
		if refreshedAt := int64FromMap(m, "refresh_refreshed_at"); refreshedAt > 0 {
			expiresAt = refreshedAt + expiresIn
		}
	}
	return LoginBufferCredentials{
		OpenID:                 stringFromMap(m, "openid"),
		AccessToken:            stringFromMap(m, "accesstoken"),
		RefreshToken:           stringFromMap(m, "refreshtoken"),
		RefreshTokenObservedAt: int64FromMap(m, "refresh_token_observed_at"),
		RefreshBusinessID:      stringFromMap(m, "refresh_business_id"),
		LoginType:              defaultString(stringFromMap(m, "logintype"), "WX"),
		Nickname:               stringFromMap(m, "nickname"),
		ExpiresAt:              expiresAt,
		ExpiresIn:              expiresIn,
	}
}

func (c LoginBufferCredentials) ToMap() map[string]any {
	return map[string]any{
		"openid":                    c.OpenID,
		"accesstoken":               c.AccessToken,
		"refreshtoken":              c.RefreshToken,
		"refresh_token_observed_at": c.RefreshTokenObservedAt,
		"refresh_business_id":       c.RefreshBusinessID,
		"logintype":                 defaultString(c.LoginType, "WX"),
		"nickname":                  c.Nickname,
		"expires_at":                c.ExpiresAt,
		"expires_in":                defaultInt64(c.ExpiresIn, 7200),
		"refresh_refreshed_at":      time.Now().Unix(),
	}
}

// ToMapForScan serializes credentials captured from a successful QR scan.
// The 30-day window is the scan/authorization window, not the time at which a
// legacy account happens to be refreshed by keepalive. Keeping this explicit
// prevents old records without lifecycle metadata from being backfilled with
// a misleading "scanned just now" timestamp.
func (c LoginBufferCredentials) ToMapForScan() map[string]any {
	if c.RefreshToken != "" && c.RefreshTokenObservedAt <= 0 {
		c.RefreshTokenObservedAt = time.Now().Unix()
	}
	return c.ToMap()
}

func (c LoginBufferCredentials) Expired(skew time.Duration) bool {
	return time.Now().Add(skew).Unix() >= c.ExpiresAt
}

type LoginBufferResult struct {
	LoginBuffer string
	Credentials LoginBufferCredentials
	Refreshed   bool
}

type LoginBufferClient struct {
	httpClient *http.Client
	timeout    time.Duration
}

func NewLoginBufferClient(timeout time.Duration) *LoginBufferClient {
	return NewLoginBufferClientWithHTTPClient(&http.Client{Timeout: timeout}, timeout)
}

func NewLoginBufferClientWithHTTPClient(httpClient *http.Client, timeout time.Duration) *LoginBufferClient {
	if httpClient == nil {
		httpClient = &http.Client{Timeout: timeout}
	}
	return &LoginBufferClient{
		httpClient: httpClient,
		timeout:    timeout,
	}
}

func (c *LoginBufferClient) FetchLoginBuffer(ctx context.Context, creds LoginBufferCredentials) (string, error) {
	body, err := json.Marshal(loginBufferRequest{
		ExtInfo: loginExtInfo{
			ListS: loginListS{
				UnionID:     loginValueString{Value: []string{creds.OpenID}},
				UserID:      loginValueString{Value: []string{creds.OpenID}},
				AccessToken: loginValueString{Value: []string{creds.AccessToken}},
			},
			ListI: loginListI{UserType: loginValueInt{Value: []int{0}}},
		},
	})
	if err != nil {
		return "", err
	}
	ts, nonce := timestampMS(), nonce4()
	var data loginCredentialResponse
	if err = c.requestJSON(ctx, http.MethodPost, loginBufferURL, body, map[string]string{
		"Content-Type":          "application/json",
		"Ual-Access-Businessid": loginBusinessID,
		"Ual-Access-Timestamp":  ts,
		"Ual-Access-Nonce":      nonce,
		"Ual-Access-Signature":  sign(string(body), nonce, ts, loginAccessKey),
		"Cookie":                fmt.Sprintf("openid=%s; accesstoken=%s; refreshtoken=%s", creds.OpenID, creds.AccessToken, creds.RefreshToken),
	}, &data); err != nil {
		return "", err
	}
	code, err := credentialResponseCode(data.Code)
	if err != nil {
		return "", err
	}
	if code != 0 {
		return "", &LoginBufferRejectedError{Code: code, Message: redactCredentials(fmt.Sprint(data.Message), creds)}
	}
	values := stringSlicePath(data.ExtInfo, "list_s", "login_buffer", "value")
	if len(values) == 0 || values[0] == "" {
		return "", fmt.Errorf("login_buffer response is empty")
	}
	return values[0], nil
}

func (c *LoginBufferClient) RefreshCredentials(ctx context.Context, creds LoginBufferCredentials) (LoginBufferCredentials, error) {
	// QR credentials come from /pc_yyb/pcyyb_oauth, including legacy records
	// without a source marker. wxapp's separate authorization uses _auth.
	business, endpoint, key := "pc_yyb", mainRefreshURL, ""
	if creds.RefreshBusinessID == loginBusinessID {
		business, endpoint, key = loginBusinessID, refreshTokenURL, loginAccessKey
	} else if creds.RefreshBusinessID != "" && creds.RefreshBusinessID != business {
		return LoginBufferCredentials{}, fmt.Errorf("unsupported credential refresh business")
	}
	refreshed, err := c.requestCredentials(ctx, creds, endpoint, business, key)
	var rejected *RefreshRejectedError
	if business == "pc_yyb" && errors.As(err, &rejected) && rejected.Code == -109 && ctx.Err() == nil {
		// -109 is a wrapper, not proof of revocation. Official check-login may
		// recover/rotate credentials or expose the underlying WeChat rejection.
		checked, checkErr := c.CheckLogin(ctx, creds)
		if checkErr == nil {
			return checked, nil
		}
		return LoginBufferCredentials{}, fmt.Errorf("%w; check login: %w", err, checkErr)
	}
	return refreshed, err
}

func (c *LoginBufferClient) CheckLogin(ctx context.Context, creds LoginBufferCredentials) (LoginBufferCredentials, error) {
	return c.requestCredentials(ctx, creds, checkLoginURL, "pc_yyb", "")
}

func (c *LoginBufferClient) requestCredentials(ctx context.Context, creds LoginBufferCredentials, endpoint, business, key string) (LoginBufferCredentials, error) {
	if creds.RefreshToken == "" {
		return LoginBufferCredentials{}, ErrMissingRefreshToken
	}
	body, err := json.Marshal(refreshTokenRequest{UserInfo: refreshTokenUserInfo{
		OpenID:       creds.OpenID,
		RefreshToken: creds.RefreshToken,
		AccessToken:  creds.AccessToken,
		LoginType:    defaultString(creds.LoginType, "WX"),
	}})
	if err != nil {
		return LoginBufferCredentials{}, err
	}
	ts, nonce := timestampMS(), nonce4()
	var data loginCredentialResponse
	if err = c.requestJSON(ctx, http.MethodPost, endpoint, body, map[string]string{
		"Content-Type":          "application/json",
		"Ual-Access-Businessid": business,
		"Ual-Access-Timestamp":  ts,
		"Ual-Access-Nonce":      nonce,
		"Ual-Access-Signature":  sign(string(body), nonce, ts, key),
	}, &data); err != nil {
		return LoginBufferCredentials{}, err
	}
	code, err := credentialResponseCode(data.Code)
	if err != nil {
		return LoginBufferCredentials{}, err
	}
	if code != 0 {
		operation := "refresh_token"
		if endpoint == checkLoginURL {
			operation = "check_login"
		}
		return LoginBufferCredentials{}, &RefreshRejectedError{Code: code, Message: redactCredentials(fmt.Sprint(data.Message), creds), Operation: operation}
	}
	info := data.UserInfo
	if openID := stringFromMap(info, "open_id"); openID != "" && openID != creds.OpenID {
		return LoginBufferCredentials{}, fmt.Errorf("credential response account mismatch")
	}
	if loginType := stringFromMap(info, "login_type"); loginType != "" && loginType != defaultString(creds.LoginType, "WX") {
		return LoginBufferCredentials{}, fmt.Errorf("credential response login type mismatch")
	}
	expiresIn := defaultInt64(int64FromMap(info, "expires_in"), 7200)
	refreshed := creds
	refreshed.RefreshBusinessID = business
	refreshed.LoginType = defaultString(creds.LoginType, "WX")
	refreshed.AccessToken = stringFromMap(info, "access_token")
	if rt := stringFromMap(info, "refresh_token"); rt != "" && rt != refreshed.RefreshToken {
		refreshed.RefreshToken = rt
	}
	refreshed.ExpiresIn = expiresIn
	refreshed.ExpiresAt = time.Now().Unix() + expiresIn
	if refreshed.AccessToken == "" {
		return LoginBufferCredentials{}, fmt.Errorf("refresh response missing access_token")
	}
	return refreshed, nil
}

func (c *LoginBufferClient) RefreshLoginBuffer(ctx context.Context, creds LoginBufferCredentials) (LoginBufferResult, error) {
	refreshed, err := c.RefreshCredentials(ctx, creds)
	if err != nil {
		return LoginBufferResult{}, err
	}
	lb, err := c.FetchLoginBuffer(ctx, refreshed)
	if err != nil {
		return LoginBufferResult{Credentials: refreshed, Refreshed: true}, err
	}
	return LoginBufferResult{LoginBuffer: lb, Credentials: refreshed, Refreshed: true}, nil
}

func (c *LoginBufferClient) FetchUserInfo(ctx context.Context, creds LoginBufferCredentials) (map[string]any, error) {
	ts, nonce := timestampMS(), nonce4()
	var data map[string]any
	err := c.requestJSON(ctx, http.MethodGet, userInfoURL, nil, map[string]string{
		"Ual-Access-Access-Token": creds.AccessToken,
		"Ual-Access-Login-Type":   "2",
		"Ual-Access-Openid":       creds.OpenID,
		"Ual-Access-Businessid":   "pc_yyb",
		"Ual-Access-Guid":         "web",
		"Ual-Access-Nonce":        nonce,
		"Ual-Access-Requestid":    fmt.Sprintf("%d", rand.Intn(9000)+1000),
		"Ual-Access-Signature":    sign("", nonce, ts, ""),
		"Ual-Access-Timestamp":    ts,
	}, &data)
	if err != nil {
		return nil, err
	}
	return data, nil
}

func (c *LoginBufferClient) requestJSON(ctx context.Context, method, url string, body []byte, headers map[string]string, out any) error {
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Error bodies can echo the submitted access/refresh tokens. Keep the
		// HTTP status without copying an untrusted response into logs or APIs.
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	if err = json.Unmarshal(data, out); err != nil {
		return fmt.Errorf("decode JSON: %w", err)
	}
	return nil
}

func redactCredentials(message string, creds LoginBufferCredentials) string {
	for _, secret := range []string{creds.AccessToken, creds.RefreshToken, creds.OpenID} {
		if secret != "" {
			message = strings.ReplaceAll(message, secret, "[redacted]")
		}
	}
	return message
}

type loginBufferRequest struct {
	ExtInfo loginExtInfo `json:"extInfo"`
}
type loginExtInfo struct {
	ListS loginListS `json:"listS"`
	ListI loginListI `json:"listI"`
}
type loginListS struct {
	UnionID     loginValueString `json:"unionid"`
	UserID      loginValueString `json:"user_id"`
	AccessToken loginValueString `json:"access_token"`
}
type loginListI struct {
	UserType loginValueInt `json:"user_type"`
}
type loginValueString struct {
	Value []string `json:"value"`
}
type loginValueInt struct {
	Value []int `json:"value"`
}
type refreshTokenRequest struct {
	UserInfo refreshTokenUserInfo `json:"userInfo"`
}
type refreshTokenUserInfo struct {
	OpenID       string `json:"openId"`
	RefreshToken string `json:"refreshToken"`
	AccessToken  string `json:"accessToken"`
	LoginType    string `json:"loginType"`
}

type loginCredentialResponse struct {
	Code     json.RawMessage `json:"code"`
	Message  any             `json:"msg"`
	UserInfo map[string]any  `json:"user_info"`
	ExtInfo  map[string]any  `json:"ext_info"`
}

func timestampMS() string {
	return fmt.Sprintf("%d", time.Now().UnixMilli())
}

func nonce4() string {
	return fmt.Sprintf("%d", rand.Intn(10000))
}

func sign(body, nonce, ts, key string) string {
	sum := md5.Sum([]byte(body + ts + key + nonce))
	return hex.EncodeToString(sum[:])
}

func stringSlicePath(m map[string]any, path ...string) []string {
	var cur any = m
	for _, p := range path {
		obj, ok := cur.(map[string]any)
		if !ok {
			return nil
		}
		cur = obj[p]
	}
	arr, ok := cur.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, v := range arr {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func stringFromMap(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	if s, ok := m[key].(string); ok {
		return s
	}
	return ""
}

func int64FromMap(m map[string]any, key string) int64 {
	if m == nil {
		return 0
	}
	switch v := m[key].(type) {
	case int:
		return int64(v)
	case int64:
		return v
	case float64:
		return int64(v)
	case json.Number:
		n, _ := v.Int64()
		return n
	default:
		return 0
	}
}

func credentialResponseCode(raw json.RawMessage) (int, error) {
	// Preserve the JSON representation: truncating a float or defaulting a
	// missing/unrecognized value to zero would turn a rejection into success.
	var number json.Number
	if err := json.Unmarshal(raw, &number); err != nil {
		return 0, fmt.Errorf("credential response code must be an integer")
	}
	code, err := strconv.ParseInt(number.String(), 10, 32)
	if err != nil {
		return 0, fmt.Errorf("credential response code must be an integer")
	}
	return int(code), nil
}

func defaultString(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

func defaultInt64(v, def int64) int64 {
	if v == 0 {
		return def
	}
	return v
}
