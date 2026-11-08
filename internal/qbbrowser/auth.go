package qbbrowser

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"google.golang.org/protobuf/encoding/protowire"
)

const (
	PBProxyURL = "https://pbprxpc.qq.com/"
	wxServant  = "trpc.mtt.wx_auth.wx_auth"
)

// AuthError never includes upstream text, authorization codes or credentials.
type AuthError struct {
	Operation   string
	Code        int32
	Reauthorize bool
}

func (e *AuthError) Error() string {
	return fmt.Sprintf("QQBrowser %s rejected: code=%d", e.Operation, e.Code)
}

type AuthResult struct {
	Credentials Credentials
	ExpiresIn   int64 // upstream seconds; zero means unknown, never a local default
}

// ExchangeCode implements the native WXAuth RPC, not the browser extension.
// Field numbers come from QQ Browser 22.3's native serializers. The gateway
// accepts UserBase omitted; no fabricated machine identifier is necessary.
func ExchangeCode(ctx context.Context, client *http.Client, code string) (AuthResult, error) {
	if strings.TrimSpace(code) == "" || len(code) > 4096 {
		return AuthResult{}, errors.New("invalid QQBrowser authorization code")
	}
	body := pbBytes(nil, 2, pbBytes(nil, 1, []byte("qb"))) // AuthCall.call_from
	body = pbBytes(body, 3, []byte(code))
	body = pbBytes(body, 4, []byte(HostAppID))
	return wxAuth(ctx, client, "WXAuth", body, Credentials{})
}

// Refresh uses the WeChat refresh token. Qbtoken/RefreshToken rotates a
// different QB account token and must not be substituted here: ctk is the
// social access_token, whereas qb_ctk is the QB account token.
func Refresh(ctx context.Context, client *http.Client, c Credentials) (AuthResult, error) {
	if !c.CanRefresh() {
		return AuthResult{}, errors.New("QQBrowser WeChat refresh token is missing")
	}
	body := pbBytes(nil, 2, pbBytes(nil, 1, []byte("qb")))
	body = pbBytes(body, 3, []byte(c.WXRefreshToken))
	body = pbBytes(body, 4, []byte(HostAppID))
	body = pbBytes(body, 5, []byte(c.OpenID))
	return wxAuth(ctx, client, "RefreshToken", body, c)
}

func wxAuth(ctx context.Context, client *http.Client, method string, body []byte, previous Credentials) (AuthResult, error) {
	data, err := callPB(ctx, client, method, body)
	if err != nil {
		return AuthResult{}, err
	}
	msg, err := readPB(data)
	if err != nil {
		return AuthResult{}, errors.New("invalid QQBrowser authentication response")
	}
	header, ok := msg[1]
	if !ok || header.typ != protowire.BytesType {
		return AuthResult{}, errors.New("QQBrowser authentication header missing")
	}
	h, err := readPB(header.b)
	if err != nil || !validTypes(h, map[protowire.Number]protowire.Type{1: protowire.VarintType, 2: protowire.BytesType}) {
		return AuthResult{}, errors.New("invalid QQBrowser authentication header")
	}
	if code := int32(h[1].n); code != 0 {
		reason := strings.ToLower(string(h[2].b))
		reauthorize := code == 40030 || code == 42002 || code == 42007 ||
			(strings.Contains(reason, "refresh_token") && (strings.Contains(reason, "invalid") || strings.Contains(reason, "expired")))
		return AuthResult{}, &AuthError{Operation: method, Code: code, Reauthorize: reauthorize}
	}
	if !validTypes(msg, map[protowire.Number]protowire.Type{2: protowire.BytesType, 3: protowire.BytesType, 4: protowire.BytesType, 5: protowire.VarintType, 6: protowire.BytesType}) {
		return AuthResult{}, errors.New("invalid QQBrowser token fields")
	}
	c := previous
	c.OpenID, c.QBAccessToken = string(msg[2].b), string(msg[4].b)
	if method == "WXAuth" {
		if f, ok := msg[7]; ok && f.typ != protowire.BytesType {
			return AuthResult{}, errors.New("invalid QQBrowser identity fields")
		}
		c.UnionID = string(msg[7].b)
	}
	if token := string(msg[3].b); token != "" {
		c.WXRefreshToken = token
	}
	if c.OpenID == "" || c.QBAccessToken == "" || c.WXRefreshToken == "" || previous.OpenID != "" && c.OpenID != previous.OpenID {
		return AuthResult{}, errors.New("QQBrowser authentication identity or tokens missing/mismatched")
	}
	// Field 5 is expire. Do not extend the schedule without an upstream value.
	expires := msg[5].n
	if expires > 366*86400 {
		return AuthResult{}, errors.New("invalid QQBrowser token lifetime")
	}
	return AuthResult{Credentials: c, ExpiresIn: int64(expires)}, nil
}

func callPB(ctx context.Context, client *http.Client, method string, body []byte) ([]byte, error) {
	packet := protowire.AppendTag(nil, 1, protowire.VarintType)
	packet = protowire.AppendVarint(packet, 100)
	packet = pbBytes(packet, 2, []byte(wxServant))
	packet = pbBytes(packet, 3, []byte("/"+wxServant+"/"+method))
	packet = pbBytes(packet, 4, body)
	framed := make([]byte, 4, len(packet)+4)
	binary.BigEndian.PutUint32(framed, uint32(len(packet)+4))
	framed = append(framed, packet...)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, PBProxyURL, bytes.NewReader(framed))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	if client == nil {
		client = &http.Client{}
	}
	hc := *client
	hc.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	resp, err := hc.Do(req)
	if err != nil {
		return nil, errors.New("QQBrowser authentication transport failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("QQBrowser authentication HTTP %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1_000_001))
	if err != nil || len(raw) < 5 || len(raw) > 1_000_000 || int(binary.BigEndian.Uint32(raw[:4])) != len(raw) {
		return nil, errors.New("invalid QQBrowser PBProxy frame")
	}
	msg, err := readPB(raw[4:])
	if err != nil || !validTypes(msg, map[protowire.Number]protowire.Type{1: protowire.VarintType, 2: protowire.BytesType, 3: protowire.BytesType, 4: protowire.BytesType, 5: protowire.VarintType, 7: protowire.VarintType}) {
		return nil, errors.New("invalid QQBrowser PBProxy reply")
	}
	// The reply uses the short method name, unlike the request's full route.
	if string(msg[2].b) != wxServant || string(msg[3].b) != method || int32(msg[5].n) != 0 || int32(msg[7].n) != 0 || len(msg[4].b) == 0 {
		return nil, errors.New("QQBrowser PBProxy route or result mismatch")
	}
	return msg[4].b, nil
}

func pbBytes(dst []byte, field protowire.Number, value []byte) []byte {
	dst = protowire.AppendTag(dst, field, protowire.BytesType)
	return protowire.AppendBytes(dst, value)
}

type pbField struct {
	typ protowire.Type
	n   uint64
	b   []byte
}

func readPB(raw []byte) (map[protowire.Number]pbField, error) {
	fields := map[protowire.Number]pbField{}
	for len(raw) > 0 {
		num, typ, n := protowire.ConsumeTag(raw)
		if n < 0 || !num.IsValid() {
			return nil, errors.New("invalid protobuf tag")
		}
		raw = raw[n:]
		// Only these scalar fields are interpreted; maps/extensions can repeat.
		if num <= 5 || num == 7 {
			if _, exists := fields[num]; exists {
				return nil, errors.New("duplicate protobuf field")
			}
		}
		f := pbField{typ: typ}
		switch typ {
		case protowire.BytesType:
			f.b, n = protowire.ConsumeBytes(raw)
		case protowire.VarintType:
			f.n, n = protowire.ConsumeVarint(raw)
		default:
			n = protowire.ConsumeFieldValue(num, typ, raw)
		}
		if n < 0 {
			return nil, errors.New("invalid protobuf value")
		}
		raw = raw[n:]
		fields[num] = f
	}
	return fields, nil
}

func validTypes(fields map[protowire.Number]pbField, expected map[protowire.Number]protowire.Type) bool {
	for num, typ := range expected {
		if f, ok := fields[num]; ok && f.typ != typ {
			return false
		}
	}
	return true
}
