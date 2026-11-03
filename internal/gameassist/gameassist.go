// Package gameassist describes the GameAssist (腾讯手游助手) account service.
// GameAssist 6.10 is built from the Androws source tree: it shares the YYB
// WMPF host identity, but onboards accounts through the syzslogin OAuth
// callback on the same yybadaccess gateway, which answers with camelCase
// envelopes.
package gameassist

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	// OAuthAppID is the WeChat open-platform appid embedded in GameAssist.exe
	// (qrconnect fast-login page; also captured in GameAssist.log).
	OAuthAppID = "wxef99873dbfab493c"
	OAuthScope = "snsapi_login,snsapi_runtime_pcsdk"
	// OAuthRedirectURI is the syzslogin callback registered by the official
	// client. WeChat appends code/state when redirecting the authorization.
	OAuthRedirectURI = "https://yybadaccess.3g.qq.com/syzslogin/syzs_oauth"
	// HostAppID is the WMPF host appid GameAssist shares with YYB: the client
	// is an Androws rebuild, so the mini-program runtime identity is identical
	// and only the credential issuance differs.
	HostAppID = "wxd44977328b36e647"
)

// ExchangeRejectedError carries only the gateway business code, never the
// upstream message text.
type ExchangeRejectedError struct{ Code int }

func (e *ExchangeRejectedError) Error() string {
	return fmt.Sprintf("GameAssist code exchange rejected: code=%d", e.Code)
}

// Credentials mirrors the login state the official client receives after the
// syzs_oauth exchange (GameAssist.log: openId, accessToken "1_...",
// refreshToken, loginType, scope, expireTime).
type Credentials struct {
	OpenID       string `json:"openid"`
	AccessToken  string `json:"accesstoken"`
	RefreshToken string `json:"refreshtoken"`
	LoginType    string `json:"logintype"`
	Nickname     string `json:"nickname"`
	Scope        string `json:"scope"`
	ExpiresAt    int64  `json:"expires_at"`
	ExpiresIn    int64  `json:"expires_in"`
}

func (c Credentials) Validate() error {
	if c.OpenID == "" || c.AccessToken == "" {
		return errors.New("invalid GameAssist credentials")
	}
	return nil
}

// ExchangeCode converts a WeChat OAuth authorize code into syzs login
// credentials by following the redirect the official client registers:
// GET syzs_oauth?login_type=wx&domain=1&syzs_prefix=&code=...&state=...
// The gateway answers with the credential envelope on success; when it
// chooses to hand the tokens over through cookies instead, the session jar
// (carried by QR sessions) still yields them.
func ExchangeCode(ctx context.Context, client *http.Client, code, state string) (Credentials, error) {
	if strings.TrimSpace(code) == "" {
		return Credentials{}, errors.New("GameAssist authorize code missing")
	}
	if client == nil {
		client = &http.Client{}
	}
	// The gateway rejects non-numeric state with -109 RC_PARAMS_INVALID; the
	// official client sends state=100*Math.random(), a plain float. Echo the
	// state the QR session used so the flow matches the official redirect.
	if !isNumericState(state) {
		return Credentials{}, errors.New("GameAssist exchange state must be numeric")
	}
	cb := OAuthRedirectURI + "?" + url.Values{
		"login_type":  {"wx"},
		"domain":      {"1"},
		"syzs_prefix": {""},
		"code":        {code},
		"state":       {state},
	}.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, cb, nil)
	if err != nil {
		return Credentials{}, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := client.Do(req)
	if err != nil {
		return Credentials{}, errors.New("GameAssist code exchange transport failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return Credentials{}, fmt.Errorf("GameAssist code exchange HTTP %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1_000_001))
	if err != nil {
		return Credentials{}, errors.New("invalid GameAssist code exchange response")
	}
	creds, err := parseExchange(data)
	if err != nil {
		return Credentials{}, err
	}
	if creds.OpenID == "" || creds.AccessToken == "" {
		creds.fillFromCookies(client.Jar, cb)
	}
	if err := validateScope(creds.Scope); err != nil {
		return Credentials{}, err
	}
	if err := creds.Validate(); err != nil {
		return Credentials{}, err
	}
	return creds, nil
}

// parseExchange decodes the syzs gateway answer. Observed shapes: the
// credential envelope {code,msg,userInfo,extInfo} (camelCase, -109 on bad
// params) and the flat login state handed to the client bridge
// (username/openId/accessToken/refreshToken/loginType/scope/expireTime).
// snake_case variants stay supported because the gateway mixes conventions.
func parseExchange(data []byte) (Credentials, error) {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(data, &top); err != nil {
		return Credentials{}, errors.New("invalid GameAssist code exchange response")
	}
	if raw, ok := top["code"]; ok {
		n, err := decodeInt(raw)
		if err != nil {
			return Credentials{}, errors.New("invalid GameAssist code exchange response")
		}
		if n != 0 {
			return Credentials{}, &ExchangeRejectedError{Code: n}
		}
	}
	body := top
	for _, key := range []string{"userInfo", "user_info"} {
		raw, ok := top[key]
		if !ok || len(raw) == 0 || string(raw) == "null" {
			continue
		}
		var nested map[string]json.RawMessage
		if json.Unmarshal(raw, &nested) != nil {
			return Credentials{}, errors.New("invalid GameAssist code exchange response")
		}
		body = nested
		break
	}
	creds := Credentials{
		OpenID:       decodeString(body, "openId", "open_id"),
		AccessToken:  decodeString(body, "accessToken", "access_token"),
		RefreshToken: decodeString(body, "refreshToken", "refresh_token"),
		LoginType:    decodeString(body, "loginType", "login_type"),
		Nickname:     decodeString(body, "username", "nickname", "nickName"),
		Scope:        decodeString(body, "scope"),
	}
	if creds.LoginType == "" {
		creds.LoginType = "WX"
	}
	now := time.Now().Unix()
	switch {
	case decodeInt64(body, "expireTime", "expire_time") > now:
		creds.ExpiresAt = decodeInt64(body, "expireTime", "expire_time")
		creds.ExpiresIn = creds.ExpiresAt - now
	case decodeInt64(body, "expiresIn", "expires_in") > 0:
		creds.ExpiresIn = decodeInt64(body, "expiresIn", "expires_in")
		creds.ExpiresAt = now + creds.ExpiresIn
	default:
		// Observed official tokens carry a ~30-day window; keep the scan
		// window default instead of pretending a two-hour lifetime.
		creds.ExpiresIn = 30 * 86400
		creds.ExpiresAt = now + creds.ExpiresIn
	}
	return creds, nil
}

// fillFromCookies recovers the token set when the gateway delivers it as
// cookies, matching the pc_yyb_oauth callback behavior.
func (c *Credentials) fillFromCookies(jar http.CookieJar, rawURL string) {
	if jar == nil {
		return
	}
	u, err := url.Parse(rawURL)
	if err != nil {
		return
	}
	hostURL, _ := url.Parse(OAuthRedirectURI)
	for _, target := range []*url.URL{u, hostURL} {
		if target == nil {
			continue
		}
		for _, ck := range jar.Cookies(target) {
			switch ck.Name {
			case "openid":
				if c.OpenID == "" {
					c.OpenID = ck.Value
				}
			case "accesstoken":
				if c.AccessToken == "" {
					c.AccessToken = ck.Value
				}
			case "refreshtoken":
				if c.RefreshToken == "" {
					c.RefreshToken = ck.Value
				}
			case "logintype":
				if c.LoginType == "" {
					c.LoginType = ck.Value
				}
			case "nickname":
				if c.Nickname == "" {
					c.Nickname = ck.Value
				}
			}
		}
	}
}

func validateScope(scope string) error {
	if scope == "" {
		return nil
	}
	for _, s := range strings.Split(scope, ",") {
		if strings.TrimSpace(s) == "snsapi_runtime_pcsdk" {
			return nil
		}
	}
	return errors.New("GameAssist authorization missing mini-program scope")
}

// isNumericState mirrors the official client's state=100*Math.random(): a
// plain decimal float such as "49.57735723381141".
func isNumericState(state string) bool {
	s := strings.TrimSpace(state)
	if s == "" || len(s) > 32 {
		return false
	}
	dot := false
	for i, r := range s {
		switch {
		case r >= '0' && r <= '9':
		case r == '.' && !dot && i > 0 && i < len(s)-1:
			dot = true
		default:
			return false
		}
	}
	return true
}

// RandomState generates a fresh numeric state in the official shape.
func RandomState() string {
	return strconv.FormatFloat(rand.Float64()*100, 'f', 14, 64)
}

func decodeString(m map[string]json.RawMessage, keys ...string) string {
	for _, key := range keys {
		raw, ok := m[key]
		if !ok || len(raw) == 0 || string(raw) == "null" {
			continue
		}
		var s string
		if json.Unmarshal(raw, &s) == nil {
			return s
		}
	}
	return ""
}

func decodeInt64(m map[string]json.RawMessage, keys ...string) int64 {
	for _, key := range keys {
		raw, ok := m[key]
		if !ok || len(raw) == 0 || string(raw) == "null" {
			continue
		}
		var n int64
		if json.Unmarshal(raw, &n) == nil {
			return n
		}
		var s string
		if json.Unmarshal(raw, &s) == nil {
			var parsed int64
			if _, err := fmt.Sscanf(strings.TrimSpace(s), "%d", &parsed); err == nil {
				return parsed
			}
		}
	}
	return 0
}

func decodeInt(raw json.RawMessage) (int, error) {
	var n int
	if err := json.Unmarshal(raw, &n); err == nil {
		return n, nil
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		var parsed int
		if _, err := fmt.Sscanf(strings.TrimSpace(s), "%d", &parsed); err == nil {
			return parsed, nil
		}
	}
	return 0, fmt.Errorf("not an integer")
}
