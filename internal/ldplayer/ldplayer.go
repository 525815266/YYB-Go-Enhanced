// Package ldplayer describes the LDPlayer (雷电模拟器) WeChat mini-game
// channel. The game center inside LDPlayer 14 shows a WeChat QR; scanning it
// yields an OAuth code that the LDPlayer game service exchanges for an ilink
// login buffer. The buffer carries the LDPlayer host identity
// (wx56135c126042c9e7) and works with the shared ilink stack.
//
// Known limitations (docs/ldplayer-channel-audit-20261105.md): the buffer has
// no refresh endpoint — the session is single-shot and dies when the upstream
// revokes it. No AutoAuth recovery. No unionid — same-WeChat binding across
// re-scans cannot be verified. Treat each authorization as disposable.
package ldplayer

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
	HostAppID = "wx56135c126042c9e7"
	// OAuthAppID is the same value; the LDPlayer game center uses it for both
	// the OAuth QR and the ilink identity.
	OAuthAppID = HostAppID
	// OAuthScope matches what the game center requests.
	OAuthScope = "snsapi_login,snsapi_runtime_pcsdk"
	// OAuthRedirectURI is the game center's callback page.
	OAuthRedirectURI = "https://www.ldmnq.com/ldy/pcbox/tencent-login.html"
	// BufferURL exchanges an OAuth code for a base64 login buffer.
	// Empty body; GET returns 405.
	BufferURL = "https://game-service.ldmnq.com/mini-game/wechat/login"
)

// BufferRejectedError carries only the business code.
type BufferRejectedError struct {
	Status int
	Body   string
}

func (e *BufferRejectedError) Error() string {
	return fmt.Sprintf("LDPlayer buffer fetch failed: HTTP %d", e.Status)
}

// FetchBuffer exchanges an OAuth code for a base64 login buffer.
// The game service returns {"code":200,"data":"<base64>"} on success.
func FetchBuffer(ctx context.Context, client *http.Client, oauthCode string) (string, error) {
	if strings.TrimSpace(oauthCode) == "" {
		return "", errors.New("LDPlayer OAuth code missing")
	}
	if client == nil {
		client = &http.Client{}
	}
	url := BufferURL + "?code=" + oauthCode
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return "", errors.New("LDPlayer buffer transport failed")
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1_000_001))
	if err != nil {
		return "", errors.New("invalid LDPlayer buffer response")
	}
	if resp.StatusCode != 200 {
		return "", &BufferRejectedError{Status: resp.StatusCode, Body: string(raw)}
	}
	var out struct {
		Code int    `json:"code"`
		Data string `json:"data"`
	}
	if jsonErr := json.Unmarshal(raw, &out); jsonErr != nil {
		return "", fmt.Errorf("invalid LDPlayer buffer response: %w", jsonErr)
	}
	if out.Code != 200 {
		return "", &BufferRejectedError{Status: out.Code, Body: string(raw)}
	}
	if strings.TrimSpace(out.Data) == "" {
		return "", errors.New("LDPlayer response has no login buffer")
	}
	decoded, err := base64.StdEncoding.Strict().DecodeString(strings.TrimSpace(out.Data))
	if err != nil {
		return "", errors.New("invalid LDPlayer ticket encoding")
	}
	fields, fieldErr := parsePBFields(decoded)
	if fieldErr != nil || len(fields[1]) == 0 || len(fields[2]) == 0 || string(fields[3]) != HostAppID {
		return "", errors.New("LDPlayer ticket provider mismatch")
	}
	return strings.TrimSpace(out.Data), nil
}

// BufferIdentity derives a stable OpenID from the buffer's device_id (field 2).
// Same LDPlayer installation → same device_id → same account identity.
func BufferIdentity(bufferB64 string) (string, error) {
	decoded, err := base64.StdEncoding.Strict().DecodeString(strings.TrimSpace(bufferB64))
	if err != nil {
		return "", errors.New("invalid LDPlayer ticket encoding")
	}
	fields, fieldErr := parsePBFields(decoded)
	if fieldErr != nil || len(fields[2]) == 0 {
		return "", errors.New("LDPlayer ticket has no device_id")
	}
	return "ldplayer:" + fmt.Sprintf("%x", fields[2]), nil
}

// ValidateBuffer checks a base64 ticket's host appid without fetching.
func ValidateBuffer(bufferB64 string) error {
	decoded, err := base64.StdEncoding.Strict().DecodeString(strings.TrimSpace(bufferB64))
	if err != nil {
		return errors.New("invalid LDPlayer ticket encoding")
	}
	fields, fieldErr := parsePBFields(decoded)
	if fieldErr != nil || len(fields[1]) == 0 || len(fields[2]) == 0 || string(fields[3]) != HostAppID {
		return errors.New("LDPlayer ticket provider mismatch")
	}
	return nil
}

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
