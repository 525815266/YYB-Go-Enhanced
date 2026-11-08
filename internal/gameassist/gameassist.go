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
	"regexp"
	"strconv"
	"strings"
	"sync"
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
	// DefaultRefreshInterval is a conservative access-token refresh schedule,
	// not the lifetime of the longer scan/refresh authorization.
	DefaultRefreshInterval = 2 * 60 * 60
)

// ExchangeRejectedError carries only the gateway business code, never the
// upstream message text.
type ExchangeRejectedError struct{ Code int }

func (e *ExchangeRejectedError) Error() string {
	return fmt.Sprintf("GameAssist code exchange rejected: code=%d", e.Code)
}

// errNotJSONEnvelope marks a callback answer that is neither the credential
// envelope nor the flat login state; callers enrich it with shape diagnostics.
var errNotJSONEnvelope = errors.New("not a JSON envelope")

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
	ExpirySource string `json:"expiry_source,omitempty"`
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
	// Record every redirect hop's Set-Cookie: the credentials ride .qq.com
	// cookies on an intermediate hop, and relying on the cookie jar alone has
	// proven unreliable across domains (production fingerprint 2026-11-04:
	// the chain ends on login_result.html with no jar-visible cookie).
	recorder := &hopRecorder{rt: client.Transport}
	if recorder.rt == nil {
		recorder.rt = http.DefaultTransport
	}
	hopClient := &http.Client{
		Transport:     recorder,
		Timeout:       client.Timeout,
		Jar:           client.Jar,
		CheckRedirect: client.CheckRedirect,
	}
	req.Header.Set("Accept", "text/html,application/json")
	req.Header.Set("Referer", "https://open.weixin.qq.com/")
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
	resp, err := hopClient.Do(req)
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
	// The callback can answer JSON, or the official HTML hop chain whose
	// credential cookies ride the redirect Set-Cookie headers (production
	// fingerprint 2026-11-04: the syzs_oauth 302 sets openid/accesstoken/
	// refreshtoken/logintype/scope/expires_in before landing on
	// login_result.html). Try every source before diagnosing.
	creds, parseErr := parseExchange(data)
	// Cookies can supply the lifetime even when JSON already supplied tokens.
	// Apply the scheduling fallback only after all callback sources were read.
	creds.fillFromCookies(client.Jar, cb)
	creds.fillFromCookies(recorder.cookiesFor(cb), cb)
	if creds.OpenID == "" || creds.AccessToken == "" {
		// The HTML hop itself may write the cookies via script literals.
		fillFromHTML(&creds, data)
	}
	if creds.Validate() == nil {
		if creds.ExpiresAt == 0 {
			creds.ExpiresIn = DefaultRefreshInterval
			creds.ExpiresAt = time.Now().Unix() + creds.ExpiresIn
			creds.ExpirySource = "fallback"
		}
		if err := validateScope(creds.Scope); err != nil {
			return Credentials{}, err
		}
		return creds, nil
	}
	if parseErr != nil {
		if errors.Is(parseErr, errNotJSONEnvelope) {
			// Bounded diagnostics: shape only, never body text — the page may
			// carry the raw tokens.
			finalURL := sanitizeURLForLog(resp.Request.URL.String())
			return Credentials{}, fmt.Errorf("invalid GameAssist code exchange response: HTTP %d type=%q len=%d hops=%v url=%s",
				resp.StatusCode, resp.Header.Get("Content-Type"), len(data), recorder.hopSummary(), finalURL)
		}
		return Credentials{}, parseErr
	}
	return Credentials{}, creds.Validate()
}

// parseExchange decodes the syzs gateway answer. Observed shapes: the
// credential envelope {code,msg,userInfo,extInfo} (camelCase, -109 on bad
// params) and the flat login state handed to the client bridge
// (username/openId/accessToken/refreshToken/loginType/scope/expireTime).
// snake_case variants stay supported because the gateway mixes conventions.
func parseExchange(data []byte) (Credentials, error) {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(data, &top); err != nil {
		return Credentials{}, errNotJSONEnvelope
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
	case decodeInt64(body, "expireTime", "expire_time") > 0:
		creds.ExpiresAt = decodeInt64(body, "expireTime", "expire_time")
		creds.ExpiresIn = creds.ExpiresAt - now
		creds.ExpirySource = "upstream"
	case decodeInt64(body, "expiresIn", "expires_in") > 0:
		creds.ExpiresIn = decodeInt64(body, "expiresIn", "expires_in")
		creds.ExpiresAt = now + creds.ExpiresIn
		creds.ExpirySource = "upstream"
	}
	return creds, nil
}

// fillFromHTML extracts the credential literals an HTML callback hop writes
// via document.cookie / setCookie(...) style script assignments. Values stay
// out of error paths; the page is never logged.
func fillFromHTML(c *Credentials, data []byte) bool {
	text := string(data)
	picked := false
	set := func(name, value string) {
		before := *c
		c.fillFromCookie(name, strings.TrimSpace(value))
		if *c != before {
			picked = true
		}
	}
	// Matches document.cookie="name=value;..." and setCookie("name","value")
	// with either quote style, honoring JS escapes crudely.
	re := regexp.MustCompile(`(?i)(?:document\.cookie|setCookie)\s*[(=]\s*"((?:[^"\\]|\\.)*)"|"((?:[^"\\]|\\.)+?)"\s*,\s*"((?:[^"\\]|\\.)+?)"`)
	for _, m := range re.FindAllStringSubmatch(text, 200) {
		if m[1] != "" {
			for _, part := range strings.Split(m[1], ";") {
				kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
				if len(kv) == 2 {
					set(unquoteJS(kv[0]), unquoteJS(kv[1]))
				}
			}
			continue
		}
		set(unquoteJS(m[2]), unquoteJS(m[3]))
	}
	return picked
}

func unquoteJS(s string) string {
	if v, err := strconv.Unquote(`"` + s + `"`); err == nil {
		return v
	}
	return s
}

func cookieNames(cookies []*http.Cookie) []string {
	names := make([]string, 0, len(cookies))
	for _, ck := range cookies {
		names = append(names, ck.Name)
	}
	return names
}

// hopRecorder remembers the Set-Cookie of every redirect hop along the
// exchange. Only cookie names travel into error paths; values stay in
// cookiesFor until they land in Credentials.
type hopRecorder struct {
	rt      http.RoundTripper
	mu      sync.Mutex
	hops    []string
	cookies []*http.Cookie
}

func (h *hopRecorder) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := h.rt.RoundTrip(req)
	if err == nil && resp != nil {
		h.mu.Lock()
		h.hops = append(h.hops, fmt.Sprintf("%d:%s", resp.StatusCode, sanitizeURLForLog(req.URL.String())))
		h.cookies = append(h.cookies, resp.Cookies()...)
		h.mu.Unlock()
	}
	return resp, err
}

func (h *hopRecorder) hopSummary() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	summary := make([]string, 0, len(h.hops)+1)
	summary = append(summary, h.hops...)
	summary = append(summary, "set:"+strings.Join(cookieNames(h.cookies), ","))
	return summary
}

// cookiesFor returns the recorded cookies that would be visible to rawURL
// (host suffix or .qq.com wide), regardless of jar semantics.
func (h *hopRecorder) cookiesFor(rawURL string) http.CookieJar {
	return &staticCookieJar{cookies: func() []*http.Cookie {
		h.mu.Lock()
		defer h.mu.Unlock()
		out := make([]*http.Cookie, len(h.cookies))
		copy(out, h.cookies)
		return out
	}()}
}

// staticCookieJar serves a fixed cookie set for any URL; extraction filters
// by name, so per-domain scoping is unnecessary here.
type staticCookieJar struct{ cookies []*http.Cookie }

func (j *staticCookieJar) Cookies(*url.URL) []*http.Cookie     { return j.cookies }
func (j *staticCookieJar) SetCookies(*url.URL, []*http.Cookie) {}

func sanitizeURLForLog(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return "[unparsed]"
	}
	q := u.Query()
	for _, key := range []string{"code", "openid", "accesstoken", "refreshtoken", "access_token", "refresh_token", "open_id"} {
		if q.Get(key) != "" {
			q.Set(key, "[redacted]")
		}
	}
	u.RawQuery = q.Encode()
	return u.String()
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
			c.fillFromCookie(ck.Name, ck.Value)
		}
	}
}

// fillFromCookie applies one callback cookie. Values may be URL-encoded by
// the gateway (observed scope with %2C); decode tolerantly.
func (c *Credentials) fillFromCookie(name, value string) {
	value = decodeCookieValue(value)
	if value == "" {
		return
	}
	switch strings.ToLower(name) {
	case "openid":
		if c.OpenID == "" {
			c.OpenID = value
		}
	case "accesstoken":
		if c.AccessToken == "" {
			c.AccessToken = value
		}
	case "refreshtoken":
		if c.RefreshToken == "" {
			c.RefreshToken = value
		}
	case "logintype":
		if c.LoginType == "" {
			c.LoginType = value
		}
	case "nickname":
		if c.Nickname == "" {
			c.Nickname = value
		}
	case "scope":
		if c.Scope == "" {
			c.Scope = value
		}
	case "expires_in":
		if c.ExpiresAt == 0 {
			if n, err := strconv.ParseInt(value, 10, 64); err == nil && n > 0 {
				c.ExpiresIn = n
				c.ExpiresAt = time.Now().Unix() + n
				c.ExpirySource = "upstream"
			}
		}
	}
}

func decodeCookieValue(value string) string {
	if !strings.Contains(value, "%") {
		return value
	}
	if decoded, err := url.QueryUnescape(value); err == nil {
		return decoded
	}
	return value
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
