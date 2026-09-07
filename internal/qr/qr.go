package qr

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"image"
	_ "image/gif"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"yyb_go/internal/protocol"
)

const (
	AppID            = "wxd44977328b36e647"
	OAuthRedirectURI = "https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=WX"
	OAuthScope       = "snsapi_login,snsapi_runtime_pcsdk"
	OAuthState       = "web"

	oauthURL = "https://open.weixin.qq.com/connect/qrconnect" +
		"?appid=" + AppID +
		"&redirect_uri=" + OAuthRedirectURI +
		"&response_type=code&scope=" + OAuthScope +
		"&state=" + OAuthState + "&fast_login=1&self_redirect=true"
	callbackURL  = "https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth"
	qrBase       = "https://open.weixin.qq.com/connect/qrcode/"
	longPollBase = "https://long.open.weixin.qq.com/connect/l/qrconnect"
	qrPollWait   = 35 * time.Second
	qrPollLimit  = 40 * time.Second
)

var (
	uuidRE = regexp.MustCompile(`/connect/qrcode/([^"'>\s]+)`)
	errRE  = regexp.MustCompile(`wx_errcode\s*=\s*(\d+)`)
	codeRE = regexp.MustCompile(`wx_code\s*=\s*'([^']*)'`)
)

type Session struct {
	ID            string
	WXUUID        string
	QRCodeURL     string
	CreatedAt     time.Time
	HTTPClient    *http.Client
	Jar           http.CookieJar
	AuthorizeCode string
	Credentials   *protocol.LoginBufferCredentials
	LoginBuffer   string
	Status        string
	Error         string
	mu            sync.Mutex
}

func (s *Session) Age() time.Duration { return time.Since(s.CreatedAt) }

type ImageResult struct {
	Session    *Session
	ImageBytes []byte
}

type PollResult struct {
	Status  string `json:"status"`
	ErrCode *int   `json:"errcode,omitempty"`
	Code    string `json:"-"`
	Message string `json:"-"`
}

type Client struct {
	timeout      time.Duration
	transport    http.RoundTripper
	loginBuffers *protocol.LoginBufferClient
}

func NewClient(timeout time.Duration) *Client {
	client, _ := NewClientWithProxy(timeout, "", false)
	return client
}

func NewClientWithProxy(timeout time.Duration, proxyValue string, fallbackDirect bool) (*Client, error) {
	transport, err := protocol.NewHTTPTransport(proxyValue, fallbackDirect)
	if err != nil {
		return nil, err
	}
	httpClient := &http.Client{Timeout: timeout, Transport: transport}
	return &Client{
		timeout:      timeout,
		transport:    transport,
		loginBuffers: protocol.NewLoginBufferClientWithHTTPClient(httpClient, timeout),
	}, nil
}

func (c *Client) LoginBuffers() *protocol.LoginBufferClient { return c.loginBuffers }

func (c *Client) GetQRCodeImage(ctx context.Context) (ImageResult, error) {
	sess, err := c.CreateSession(ctx)
	if err != nil {
		return ImageResult{}, err
	}
	data, err := c.FetchQRCodeImage(ctx, sess)
	if err != nil {
		return ImageResult{}, err
	}
	return ImageResult{Session: sess, ImageBytes: data}, nil
}

func (c *Client) CreateSession(ctx context.Context) (*Session, error) {
	jar, err := cookiejar.New(nil)
	if err != nil {
		return nil, err
	}
	hc := &http.Client{Timeout: c.timeout, Jar: jar, Transport: c.transport}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, oauthURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	m := uuidRE.FindStringSubmatch(string(body))
	if len(m) < 2 {
		return nil, fmt.Errorf("failed to parse WeChat QR uuid")
	}
	id, err := randomHex(16)
	if err != nil {
		return nil, err
	}
	wxuuid := m[1]
	return &Session{
		ID:         id,
		WXUUID:     wxuuid,
		QRCodeURL:  qrBase + wxuuid,
		CreatedAt:  time.Now(),
		HTTPClient: hc,
		Jar:        jar,
		Status:     "pending",
	}, nil
}

func (c *Client) FetchQRCodeImage(ctx context.Context, sess *Session) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, sess.QRCodeURL, nil)
	if err != nil {
		return nil, err
	}
	resp, err := sess.HTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("QR image HTTP %d", resp.StatusCode)
	}
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if err := validateQRCodeImage(data); err != nil {
		return nil, err
	}
	return data, nil
}

func (c *Client) PollQRCode(ctx context.Context, sess *Session) (PollResult, error) {
	sess.mu.Lock()
	defer sess.mu.Unlock()
	if sess.LoginBuffer != "" {
		return PollResult{Status: "confirmed"}, nil
	}
	if sess.AuthorizeCode != "" {
		code := 405
		return PollResult{Status: "authorized", ErrCode: &code, Code: sess.AuthorizeCode}, nil
	}
	u := longPollBase + "?" + url.Values{
		"uuid": {sess.WXUUID},
		"_":    {strconv.FormatInt(time.Now().UnixMilli(), 10)},
	}.Encode()
	reqCtx, cancel := context.WithTimeout(ctx, qrPollWait)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, u, nil)
	if err != nil {
		return PollResult{}, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")
	pollClient := *sess.HTTPClient
	if pollClient.Timeout == 0 || pollClient.Timeout < qrPollLimit {
		pollClient.Timeout = qrPollLimit
	}
	resp, err := pollClient.Do(req)
	if err != nil {
		if ctx.Err() == nil && errors.Is(err, context.DeadlineExceeded) {
			sess.Status = "pending"
			return PollResult{Status: "pending"}, nil
		}
		return PollResult{}, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	text := string(body)
	errcode, code := parsePoll(text)
	switch {
	case errcode != nil && *errcode == 408:
		sess.Status = "pending"
		return PollResult{Status: "pending", ErrCode: errcode, Code: code}, nil
	case errcode != nil && *errcode == 404:
		sess.Status = "scanned"
		return PollResult{Status: "scanned", ErrCode: errcode, Code: code}, nil
	case errcode != nil && *errcode == 403:
		sess.Status = "cancelled"
		return PollResult{Status: "cancelled", ErrCode: errcode, Code: code}, nil
	case errcode != nil && *errcode == 402:
		sess.Status = "expired"
		return PollResult{Status: "expired", ErrCode: errcode, Code: code}, nil
	case errcode != nil && *errcode == 405 && code != "":
		sess.AuthorizeCode = code
		sess.Status = "authorized"
		return PollResult{Status: "authorized", ErrCode: errcode, Code: code}, nil
	default:
		sess.Status = "unknown"
		if len(text) > 200 {
			text = text[:200]
		}
		sess.Error = text
		return PollResult{Status: "unknown", ErrCode: errcode, Code: code, Message: text}, nil
	}
}

func (c *Client) GetLoginBuffer(ctx context.Context, sess *Session) (protocol.LoginBufferResult, error) {
	sess.mu.Lock()
	defer sess.mu.Unlock()
	if sess.LoginBuffer != "" && sess.Credentials != nil {
		return protocol.LoginBufferResult{LoginBuffer: sess.LoginBuffer, Credentials: *sess.Credentials}, nil
	}
	if sess.AuthorizeCode == "" {
		return protocol.LoginBufferResult{}, fmt.Errorf("QR session is not authorized yet")
	}
	cb := callbackURL + "?" + url.Values{
		"login_type": {"WX"},
		"code":       {sess.AuthorizeCode},
		"state":      {"web"},
	}.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, cb, nil)
	if err != nil {
		return protocol.LoginBufferResult{}, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := sess.HTTPClient.Do(req)
	if err != nil {
		return protocol.LoginBufferResult{}, err
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	creds, err := credentialsFromJar(sess.Jar, cb)
	if err != nil {
		return protocol.LoginBufferResult{}, err
	}
	lb, err := c.loginBuffers.FetchLoginBuffer(ctx, creds)
	if err != nil {
		return protocol.LoginBufferResult{}, err
	}
	sess.Credentials = &creds
	sess.LoginBuffer = lb
	sess.Status = "confirmed"
	return protocol.LoginBufferResult{LoginBuffer: lb, Credentials: creds}, nil
}

func (c *Client) GetLoginBufferFromCode(ctx context.Context, code string) (protocol.LoginBufferResult, error) {
	code = strings.TrimSpace(code)
	if code == "" {
		return protocol.LoginBufferResult{}, fmt.Errorf("authorize code is empty")
	}
	jar, err := cookiejar.New(nil)
	if err != nil {
		return protocol.LoginBufferResult{}, err
	}
	sess := &Session{
		HTTPClient:    &http.Client{Timeout: c.timeout, Jar: jar, Transport: c.transport},
		Jar:           jar,
		AuthorizeCode: code,
		Status:        "authorized",
		CreatedAt:     time.Now(),
	}
	return c.GetLoginBuffer(ctx, sess)
}

func (c *Client) RefreshLoginBuffer(ctx context.Context, creds protocol.LoginBufferCredentials) (protocol.LoginBufferResult, error) {
	return c.loginBuffers.RefreshLoginBuffer(ctx, creds)
}

func DataURIJPEG(data []byte) string {
	return DataURIImage(data)
}

func DataURIImage(data []byte) string {
	mime := http.DetectContentType(data)
	if !strings.HasPrefix(mime, "image/") {
		mime = "image/jpeg"
	}
	return "data:" + mime + ";base64," + base64.StdEncoding.EncodeToString(data)
}

func validateQRCodeImage(data []byte) error {
	if len(data) == 0 {
		return fmt.Errorf("微信二维码接口返回空内容，可能被网络或代理拦截")
	}
	if _, format, err := image.DecodeConfig(bytes.NewReader(data)); err != nil {
		return fmt.Errorf("微信二维码接口返回的不是有效图片，可能被网络或代理拦截（%s）", format)
	}
	return nil
}

func parsePoll(text string) (*int, string) {
	var out *int
	if m := errRE.FindStringSubmatch(text); len(m) == 2 {
		n, _ := strconv.Atoi(m[1])
		out = &n
	}
	code := ""
	if m := codeRE.FindStringSubmatch(text); len(m) == 2 {
		code = m[1]
	}
	return out, code
}

func credentialsFromJar(jar http.CookieJar, rawURL string) (protocol.LoginBufferCredentials, error) {
	u, _ := url.Parse(rawURL)
	var cookies []*http.Cookie
	if u != nil {
		cookies = append(cookies, jar.Cookies(u)...)
	}
	hostURL, _ := url.Parse("https://yybadaccess.3g.qq.com/")
	cookies = append(cookies, jar.Cookies(hostURL)...)
	values := map[string]string{}
	for _, ck := range cookies {
		values[ck.Name] = ck.Value
	}
	openid := values["openid"]
	accessToken := values["accesstoken"]
	if openid == "" || accessToken == "" {
		return protocol.LoginBufferCredentials{}, fmt.Errorf("OAuth callback did not set required cookies")
	}
	expiresIn := int64(7200)
	if values["expires_in"] != "" {
		if n, err := strconv.ParseInt(values["expires_in"], 10, 64); err == nil && n > 0 {
			expiresIn = n
		}
	}
	return protocol.LoginBufferCredentials{
		OpenID:       openid,
		AccessToken:  accessToken,
		RefreshToken: values["refreshtoken"],
		LoginType:    defaultString(values["logintype"], "WX"),
		Nickname:     values["nickname"],
		ExpiresAt:    time.Now().Unix() + expiresIn,
		ExpiresIn:    expiresIn,
	}, nil
}

func defaultString(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

func randomHex(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}
