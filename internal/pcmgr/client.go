package pcmgr

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"google.golang.org/protobuf/encoding/protowire"
)

// Credentials is comparable, so credential refresh CAS checks can retain the
// existing protection against a concurrent QR authorization replacing a key.
type Credentials struct {
	AccountID     int64  `json:"account_id"`
	LoginKey      string `json:"loginkey"`
	ClientGUID    string `json:"client_guid"`
	OpenID        string `json:"openid"`
	UnionID       string `json:"unionid"`
	Nickname      string `json:"nickname"`
	Avatar        string `json:"avatar"`
	RenewedAt     int64  `json:"renewed_at"`
	RenewInterval int64  `json:"next_renew_len"`
}

func (c Credentials) Validate() error {
	g, err := hex.DecodeString(c.ClientGUID)
	if c.AccountID <= 0 || len(c.LoginKey) < 1 || len(c.LoginKey) > 1024 || err != nil || len(g) != 16 || c.ClientGUID != strings.ToLower(c.ClientGUID) {
		return errors.New("invalid PCMgr credentials")
	}
	return nil
}
func (c Credentials) NextRenewAt() int64 {
	// A schedule, not evidence of the credential's real lifetime. Unknown
	// initial intervals get a daily check. Known intervals follow the official
	// half-interval policy; intervals below 15 days are renewed promptly.
	delay := int64(86400)
	if c.RenewInterval > 0 {
		delay = c.RenewInterval / 2
		if c.RenewInterval < 15*86400 {
			delay = 60
		}
	}
	return c.RenewedAt + delay
}
func (c Credentials) Identity() string { return "pcmgr:" + strconv.FormatInt(c.AccountID, 10) }

func ExchangeCode(ctx context.Context, client *http.Client, code, guid string) (Credentials, error) {
	if strings.TrimSpace(code) == "" {
		return Credentials{}, errors.New("PCMgr authorize code missing")
	}
	body, _ := json.Marshal(map[string]any{"req": map[string]any{"platType": 3, "loginAccType": 32, "authCode": code, "clientGuid": guid}})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://jprx.m.qq.com/data/3078/forward", bytes.NewReader(body))
	if err != nil {
		return Credentials{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return Credentials{}, errors.New("PCMgr code exchange transport failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return Credentials{}, fmt.Errorf("PCMgr code exchange HTTP %d", resp.StatusCode)
	}
	var result struct {
		Ret  *int `json:"ret"`
		Data struct {
			Resp struct {
				RetCode     *int            `json:"retCode"`
				AccountID   json.Number     `json:"accountId"`
				LoginKey    string          `json:"loginkey"`
				RenewalTime json.Number     `json:"renewalTime"`
				WXScope     json.RawMessage `json:"wxscope"`
				ThirdParty  struct {
					BindAccount string `json:"bindAccount"`
					UnionID     string `json:"unionId"`
					Nickname    string `json:"nickName"`
					Avatar      string `json:"headUrl"`
				} `json:"thirdPartyAccInfo"`
			} `json:"resp"`
		} `json:"data"`
	}
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 1_000_001))
	decoder.UseNumber()
	if err := decoder.Decode(&result); err != nil {
		return Credentials{}, errors.New("invalid PCMgr code exchange response")
	}
	v := result.Data.Resp
	if result.Ret == nil || v.RetCode == nil {
		return Credentials{}, errors.New("PCMgr code exchange missing result")
	}
	if *result.Ret != 0 || *v.RetCode != 0 {
		return Credentials{}, fmt.Errorf("PCMgr code exchange rejected: gateway=%d business=%d", *result.Ret, *v.RetCode)
	}
	var scopeText string
	_ = json.Unmarshal(v.WXScope, &scopeText)
	var scopes []string
	_ = json.Unmarshal(v.WXScope, &scopes)
	if scopeText != "" {
		scopes = strings.Split(scopeText, ",")
	}
	allowed := false
	for _, s := range scopes {
		if strings.TrimSpace(s) == "snsapi_runtime_pcsdk" {
			allowed = true
		}
	}
	if !allowed {
		return Credentials{}, errors.New("PCMgr authorization missing mini-program scope")
	}
	id, err := v.AccountID.Int64()
	if err != nil {
		return Credentials{}, errors.New("invalid PCMgr account identity")
	}
	interval, _ := v.RenewalTime.Int64()
	if interval < 0 || interval > 365*86400 {
		interval = 0
	}
	creds := Credentials{AccountID: id, LoginKey: v.LoginKey, ClientGUID: guid, OpenID: v.ThirdParty.BindAccount, UnionID: v.ThirdParty.UnionID, Nickname: v.ThirdParty.Nickname, Avatar: v.ThirdParty.Avatar, RenewedAt: time.Now().Unix(), RenewInterval: interval}
	if err := creds.Validate(); err != nil {
		return Credentials{}, err
	}
	return creds, nil
}

func FetchBuffer(ctx context.Context, client *http.Client, creds Credentials) (string, error) {
	if err := creds.Validate(); err != nil {
		return "", err
	}
	guid, _ := hex.DecodeString(creds.ClientGUID)
	req := PBInt(nil, 1, uint64(creds.AccountID))
	req = PBBytes(req, 2, []byte(creds.LoginKey))
	req = PBBytes(req, 3, []byte(creds.ClientGUID))
	raw, err := Call(ctx, client, 20138, guid, req)
	if err != nil {
		return "", err
	}
	buffer, err := ParseBufferResponse(raw)
	if err != nil {
		return "", err
	}
	decoded, err := base64.StdEncoding.Strict().DecodeString(buffer)
	if err != nil {
		return "", errors.New("invalid PCMgr ticket encoding")
	}
	fields, err := parsePB(decoded)
	if err != nil || len(fields[1].b) == 0 || len(fields[2].b) == 0 || string(fields[3].b) != HostAppID {
		return "", errors.New("PCMgr ticket provider mismatch")
	}
	return buffer, nil
}

func Renew(ctx context.Context, client *http.Client, creds Credentials) (Credentials, error) {
	if err := creds.Validate(); err != nil {
		return Credentials{}, err
	}
	guid, _ := hex.DecodeString(creds.ClientGUID)
	req := PBInt(nil, 1, 2)
	req = PBInt(req, 2, uint64(creds.AccountID))
	req = PBBytes(req, 3, []byte(creds.LoginKey))
	req = PBBytes(req, 4, []byte(creds.ClientGUID))
	raw, err := Call(ctx, client, 20042, guid, req)
	if err != nil {
		return Credentials{}, err
	}
	return parseRenew(raw, creds, time.Now().Unix())
}
func parseRenew(raw []byte, creds Credentials, now int64) (Credentials, error) {
	v, err := parsePB(raw)
	if err != nil {
		return Credentials{}, err
	}
	if v[1].n != 0 {
		return Credentials{}, &RPCError{20042, int32(v[1].n)}
	}
	if v[2].n != 2 || v[3].n != uint64(creds.AccountID) || v[4].wire != protowire.BytesType || v[5].n < 1 || v[5].n > 0x7fffffff {
		return Credentials{}, errors.New("PCMgr renewal identity or interval mismatch")
	}
	creds.LoginKey = string(v[4].b)
	creds.RenewInterval = int64(v[5].n)
	creds.RenewedAt = now
	return creds, creds.Validate()
}
