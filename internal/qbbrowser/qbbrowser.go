// Package qbbrowser describes the QQ Browser account service. The browser's
// WeChat sign-in (WUP WXAuth) yields an outer access token; the token is
// exchanged for an ilink login buffer through kde.qq.com, which the shared
// WMPF/ilink stack then consumes like any other channel. The token itself is
// harvested by a companion browser extension that talks to
// chrome.account/browser.login inside QQ Browser.
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
// The access token is harvested by the companion extension; the browser
// rotates it itself, so there is no server-side refresh — a failed buffer
// fetch means the extension must push a fresh token.
type Credentials struct {
	QBAccessToken string `json:"qb_access_token"`
	UserID        string `json:"user_id"`
	GUID          string `json:"guid"`
	Nickname      string `json:"nickname"`
}

func (c Credentials) Validate() error {
	if strings.TrimSpace(c.QBAccessToken) == "" || strings.TrimSpace(c.UserID) == "" {
		return errors.New("invalid QQBrowser credentials")
	}
	return nil
}

// Identity mirrors the PCMGR convention: a stable, non-openid marker. The
// real WeChat openid only becomes visible after the first buffer exchange.
func (c Credentials) Identity() string { return "qbbrowser:" + c.UserID }

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
	body, _ := json.Marshal(map[string]any{
		"access_token": creds.QBAccessToken,
		"user_id":      creds.UserID,
		"appid":        HostAppID,
		"guid":         creds.GUID,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, BufferURL, strings.NewReader(string(body)))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	req.Header.Set("Accept", "application/json; charset=utf-8")
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
		LoginBuffer string `json:"login_buffer"`
		ErrCode     int    `json:"err_code"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return "", errors.New("invalid QQBrowser buffer response")
	}
	if out.ErrCode != SuccessErrCode {
		return "", &TokenRejectedError{Code: out.ErrCode}
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
		if n < 0 {
			return nil, errors.New("malformed tag")
		}
		raw = raw[n:]
		if typ != protowire.BytesType {
			v, n := protowire.ConsumeVarint(raw)
			if n < 0 {
				return nil, errors.New("malformed varint")
			}
			raw = raw[n:]
			if num <= 3 {
				fields[num] = []byte(fmt.Sprintf("%d", v))
			}
			continue
		}
		v, n := protowire.ConsumeBytes(raw)
		if n < 0 {
			return nil, errors.New("malformed bytes")
		}
		raw = raw[n:]
		fields[num] = v
	}
	return fields, nil
}
