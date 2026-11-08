// Package qbbrowser describes the QQ Browser account service. The browser's
// WeChat sign-in (WUP WXAuth) yields an outer access token; the token is
// exchanged for an ilink login buffer through kde.qq.com, which the shared
// WMPF/ilink stack then consumes like any other channel. The token itself is
// issued by the native WXAuth RPC or imported by a companion extension.
package qbbrowser

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"google.golang.org/protobuf/encoding/protowire"
)

const (
	// HostAppID is the WeChat open-platform appid embedded in QQ Browser's
	// sign-in flow (chrome.dll ProcessSignin context) and used by its
	// "文件工具"/game extensions as hostWxAppid.
	HostAppID = "wx0dbc1ae97f156c38"
	// BufferURL exchanges the browser's outer access token for an ilink
	// login buffer. Captured from the 文件工具 extension (assets JS):
	// POST {access_token, user_id, appid, guid} → {login_buffer, err_code}.
	BufferURL = "https://kde.qq.com/trpc.ad.wx_oauth_svr.WxOauthHttpSvr/GetWxLoginBuffer"
	// SuccessErrCode is the gateway's zero business code.
	SuccessErrCode = 0
)

// TokenRejectedError carries only the gateway business code, never the
// upstream message text.
type TokenRejectedError struct{ Code int }

func (e *TokenRejectedError) Error() string {
	return fmt.Sprintf("QQBrowser login buffer rejected: code=%d", e.Code)
}

// Credentials is comparable so CAS-style credential writes stay safe.
// Bridge imports retain their native device fields. Standalone WXAuth stores
// the distinct social refresh token and requires no browser device fields.
type Credentials struct {
	QBAccessToken  string `json:"qb_access_token"`
	WXRefreshToken string `json:"wx_refresh_token,omitempty"`
	UserID         string `json:"user_id"`
	OpenID         string `json:"openid,omitempty"`
	UnionID        string `json:"unionid,omitempty"`
	GUID           string `json:"guid"`
	Nickname       string `json:"nickname"`
	// QBRNCookie carries the official runtime's device headers. UserID is
	// GAME_H5_DEVICE_MACHINE_ID, not the account's openid; QBAccessToken is ctk.
	QBRNCookie string `json:"qbrn_cookie,omitempty"`
}

func (c Credentials) Validate() error {
	if strings.TrimSpace(c.QBAccessToken) == "" || strings.TrimSpace(c.UserID) == "" && !c.CanRefresh() {
		return errors.New("invalid QQBrowser credentials")
	}
	if len(c.QBRNCookie) > 8192 || strings.ContainsAny(c.QBRNCookie, "\r\n") {
		return errors.New("invalid QQBrowser device header")
	}
	return nil
}

func (c Credentials) CanRefresh() bool {
	return strings.TrimSpace(c.OpenID) != "" && strings.TrimSpace(c.WXRefreshToken) != ""
}

// Identity separates browser accounts on a shared machine. Older imported
// records without an account openid retain their previous machine-based marker.
func (c Credentials) Identity() string {
	if c.OpenID != "" {
		return "qbbrowser:" + c.OpenID
	}
	return "qbbrowser:" + c.UserID // compatibility with existing imported records
}

// FetchBuffer exchanges the browser token for an ilink login buffer and
// verifies it belongs to this channel (protobuf field 3 carries the host
// appid, exactly like the other providers).
func FetchBuffer(ctx context.Context, client *http.Client, creds Credentials) (string, error) {
	if err := creds.Validate(); err != nil {
		return "", err
	}
	if client == nil {
		client = &http.Client{}
	}
	input := map[string]any{
		"access_token": creds.QBAccessToken,
		"appid":        HostAppID,
	}
	if creds.UserID != "" {
		input["user_id"], input["guid"] = creds.UserID, creds.GUID
	}
	body, _ := json.Marshal(input)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, BufferURL, strings.NewReader(string(body)))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	req.Header.Set("Accept", "application/json; charset=utf-8")
	if creds.QBRNCookie != "" {
		req.Header.Set("x-qbrn-cookie", creds.QBRNCookie)
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", errors.New("QQBrowser buffer transport failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("QQBrowser buffer HTTP %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1_000_001))
	if err != nil {
		return "", errors.New("invalid QQBrowser buffer response")
	}
	var out struct {
		LoginBuffer string          `json:"login_buffer"`
		ErrCode     json.RawMessage `json:"err_code"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return "", errors.New("invalid QQBrowser buffer response")
	}
	// The official client consumes login_buffer directly. Proto3 gateways can
	// omit the default zero field; an omitted code still requires the complete,
	// channel-validated ticket below. Explicit null/string/error codes fail.
	if len(out.ErrCode) != 0 {
		var code *int
		if json.Unmarshal(out.ErrCode, &code) != nil || code == nil {
			return "", errors.New("invalid QQBrowser business code")
		}
		if *code != SuccessErrCode {
			return "", &TokenRejectedError{Code: *code}
		}
	}
	if strings.TrimSpace(out.LoginBuffer) == "" {
		return "", errors.New("QQBrowser response has no login buffer")
	}
	decoded, err := base64.StdEncoding.Strict().DecodeString(strings.TrimSpace(out.LoginBuffer))
	if err != nil {
		return "", errors.New("invalid QQBrowser ticket encoding")
	}
	fields, err := parsePBFields(decoded)
	if err != nil || len(fields[1]) == 0 || len(fields[2]) == 0 || string(fields[3]) != HostAppID {
		return "", errors.New("QQBrowser ticket provider mismatch")
	}
	return strings.TrimSpace(out.LoginBuffer), nil
}

// parsePBFields collects the top-level length-delimited protobuf fields.
func parsePBFields(raw []byte) (map[protowire.Number][]byte, error) {
	fields := map[protowire.Number][]byte{}
	for len(raw) > 0 {
		num, typ, n := protowire.ConsumeTag(raw)
		if n < 0 || !num.IsValid() {
			return nil, errors.New("malformed tag")
		}
		raw = raw[n:]
		if typ != protowire.BytesType {
			if num <= 3 {
				return nil, errors.New("ticket field must be bytes")
			}
			n := protowire.ConsumeFieldValue(num, typ, raw)
			if n < 0 {
				return nil, errors.New("malformed field")
			}
			raw = raw[n:]
			continue
		}
		v, n := protowire.ConsumeBytes(raw)
		if n < 0 {
			return nil, errors.New("malformed bytes")
		}
		raw = raw[n:]
		if num <= 3 && len(fields[num]) != 0 {
			return nil, errors.New("duplicate ticket field")
		}
		fields[num] = v
	}
	return fields, nil
}
