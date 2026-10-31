//go:build issue75probe

package protocol

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/oauth"
)

// This tagged harness never opens the account DB or invokes a login/recovery.
// Live input is an explicit private snapshot of one already-authenticated session.
type h5AuditField struct {
	wire int
	n    uint64
	b    []byte
}

func h5AuditPB(raw []byte) (map[int]h5AuditField, error) {
	fields := map[int]h5AuditField{}
	for offset := 0; offset < len(raw); {
		tag, next, err := readVarint(raw, offset)
		if err != nil || tag>>3 == 0 {
			return nil, errors.New("invalid protobuf tag")
		}
		offset = next
		field := h5AuditField{wire: int(tag & 7)}
		switch field.wire {
		case 0:
			field.n, offset, err = readVarint(raw, offset)
		case 2:
			var size uint64
			size, offset, err = readVarint(raw, offset)
			if err == nil && size <= uint64(len(raw)-offset) {
				field.b = raw[offset : offset+int(size)]
				offset += int(size)
			} else {
				err = errors.New("invalid protobuf length")
			}
		default:
			err = errors.New("unsupported protobuf wire")
		}
		if err != nil {
			return nil, err
		}
		number := int(tag >> 3)
		if _, duplicate := fields[number]; duplicate {
			return nil, errors.New("duplicate protobuf field")
		}
		fields[number] = field
	}
	return fields, nil
}

func h5AuditCallback(location, callback, state string) (string, error) {
	got, err := url.Parse(location)
	want, wantErr := url.Parse(callback)
	if err != nil || wantErr != nil || got.Scheme != want.Scheme || got.Host != want.Host || got.Path != want.Path || got.RawPath != want.RawPath || got.User != nil || got.Fragment != "" {
		return "", errors.New("callback target mismatch")
	}
	query, err := url.ParseQuery(got.RawQuery)
	if err != nil || len(query["code"]) != 1 || query.Get("code") == "" || len(query["state"]) != 1 || query.Get("state") != state {
		return "", errors.New("callback code or state mismatch")
	}
	for key, values := range want.Query() {
		if key == "code" || key == "state" || len(query[key]) != len(values) || strings.Join(query[key], "\x00") != strings.Join(values, "\x00") {
			return "", errors.New("callback query mismatch")
		}
	}
	return query.Get("code"), nil
}

func h5AuditOAuthResult(raw []byte, callback, state string, depth int) (string, *int32, error) {
	if depth > 8 {
		return "", nil, errors.New("response nesting limit")
	}
	fields, err := h5AuditPB(raw)
	if err != nil {
		return "", nil, err
	}
	base, baseErr := h5AuditPB(fields[1].b)
	ret, hasRet := base[1]
	location := string(fields[6].b)
	if strings.HasPrefix(location, "https://") || strings.HasPrefix(location, "http://") {
		if baseErr != nil || !hasRet || ret.wire != 0 {
			return "", nil, errors.New("missing explicit business status")
		}
		value := int32(ret.n)
		if value != 0 {
			return "", &value, errors.New("OAuth business rejection")
		}
		code, err := h5AuditCallback(location, callback, state)
		return code, &value, err
	}
	if baseErr == nil && hasRet && ret.wire == 0 && int32(ret.n) != 0 {
		value := int32(ret.n)
		return "", &value, errors.New("OAuth business rejection")
	}
	for number := 1; number <= 32; number++ {
		field, exists := fields[number]
		if !exists || field.wire != 2 {
			continue
		}
		code, value, nestedErr := h5AuditOAuthResult(field.b, callback, state, depth+1)
		if value != nil {
			return code, value, nestedErr
		}
	}
	return "", nil, errors.New("OAuth callback payload absent")
}

func h5AuditOAuthRequest(authorizeURL string) []byte {
	base := pbLen(1, nil)
	base = append(base, pbVar(2, 0)...)
	base = append(base, pbLen(3, nil)...)
	base = append(base, pbVar(4, 0)...)
	base = append(base, pbLen(5, []byte("Windows"))...)
	base = append(base, pbVar(6, 0)...)
	body := pbLen(1, base)
	body = append(body, pbLen(2, []byte(authorizeURL))...)
	body = append(body, pbLen(3, nil)...)
	return append(body, pbVar(4, 4)...)
}

func h5AuditPrivate(path string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer file.Close()
	_, err = file.Write(raw)
	return err
}

func h5AuditSafeError(result map[string]any, err error) {
	if err == nil {
		return
	}
	result["error_type"] = fmt.Sprintf("%T", err)
	var rejected *ShortlinkRejectedError
	if errors.As(err, &rejected) {
		result["shortlink_code"] = rejected.Code
		result["transport_rejection"] = rejected.Transport
	}
}

func TestH5ComparisonLive(t *testing.T) {
	inputPath := os.Getenv("YYB_H5_COMPARE_INPUT")
	if inputPath == "" {
		t.Skip("explicit private snapshot required")
	}
	raw, err := os.ReadFile(inputPath)
	if err != nil {
		t.Fatal("private snapshot unavailable")
	}
	var input struct {
		Session     map[string]any `json:"session"`
		AppID       string         `json:"app_id"`
		RedirectURI string         `json:"redirect_uri"`
		Scope       string         `json:"scope"`
	}
	if json.Unmarshal(raw, &input) != nil {
		t.Fatal("invalid private input")
	}
	st, err := sessionFromBlob(input.Session)
	if err != nil || string(st.Session.HostAppID) != "wxd44977328b36e647" || st.TCPProxy != "" || len(st.ShortlinkTargets) == 0 {
		t.Fatal("expected existing direct YYB session")
	}
	st.FallbackDirect = false
	st.ShortlinkTargets = st.ShortlinkTargets[:1]
	p := NewPool(DefaultConfig(), nil)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	root := filepath.Dir(inputPath)
	var results []map[string]any
	emit := func(item map[string]any) {
		item["at"] = time.Now().UTC().Format(time.RFC3339Nano)
		results = append(results, item)
		text, _ := json.Marshal(item)
		fmt.Println("H5_AUDIT " + string(text))
		if err := h5AuditPrivate(filepath.Join(root, fmt.Sprintf("safe-stage-%d.json", len(results))), item); err != nil {
			t.Fatal("stage record unavailable")
		}
	}
	control := func(label string) bool {
		packet, err := buildTransferPacket(st.Session, buildJSAPIPlaintext(st.Session.UIN, "wx3bb18aff4f2fe45f", nil, 0, nil, st.Session.HostAppID, nil))
		if err != nil {
			t.Fatal("control request build failed")
		}
		code, _, err := p.sendEnvelope(ctx, st, packet)
		item := map[string]any{"case": label, "ok": err == nil && len(code) > 0, "code_length": len(code)}
		h5AuditSafeError(item, err)
		emit(item)
		return err == nil && len(code) > 0
	}
	if !control("control_before") {
		return
	}
	if os.Getenv("YYB_SESSION_CONTROL_ONLY") == "1" {
		return
	}
	auth, err := oauth.Build(oauth.Request{AppID: input.AppID, RedirectURI: input.RedirectURI, Scope: input.Scope})
	if err != nil {
		t.Fatal("invalid OAuth target")
	}
	request := h5AuditOAuthRequest(auth.FullURL)
	plain := buildJSAPIPlaintext(st.Session.UIN, input.AppID, []byte("/cgi-bin/mmbiz-bin/oauth_authorize"), 1254, request, st.Session.HostAppID, nil)
	packet, err := buildTransferPacket(st.Session, plain)
	if err != nil {
		t.Fatal("OAuth request build failed")
	}
	_, reply, err := p.sendEnvelope(ctx, st, packet)
	if h5AuditPrivate(filepath.Join(root, "oauth1254-private-response.json"), map[string]any{"response": reply}) != nil {
		t.Fatal("private response unavailable")
	}
	item := map[string]any{"case": "oauth1254", "transport_ok": err == nil, "response_bytes": len(reply), "callback_verified": false}
	if err == nil {
		code, ret, parseErr := h5AuditOAuthResult(reply, input.RedirectURI, auth.State, 0)
		item["callback_verified"], item["code_length"] = parseErr == nil && code != "", len(code)
		if ret != nil {
			item["business_ret"] = *ret
		}
		if parseErr != nil {
			item["parse_error"] = parseErr.Error()
		}
	}
	h5AuditSafeError(item, err)
	emit(item)
	if !control("control_after_oauth1254") {
		return
	}
	auth, err = oauth.Build(oauth.Request{AppID: input.AppID, RedirectURI: input.RedirectURI, Scope: input.Scope})
	if err != nil {
		t.Fatal("invalid OAuth target")
	}
	request = issue75GetA8Key(sessionInfo(uint32(st.Session.UIN), windowsName, randomSessDevice()), auth.URL)
	packet, err = issue75RoutePacket(st.Session, request, 5112, []byte("/ilink/ilinkapp/mm/ilinkgeta8key"))
	if err != nil {
		t.Fatal("A8Key request build failed")
	}
	_, reply, err = p.sendEnvelope(ctx, st, packet)
	if h5AuditPrivate(filepath.Join(root, "a8key-private-response.json"), map[string]any{"response": reply}) != nil {
		t.Fatal("private response unavailable")
	}
	item = map[string]any{"case": "a8key5112", "transport_ok": err == nil, "response_bytes": len(reply), "callback_verified": false}
	h5AuditSafeError(item, err)
	if err == nil {
		fields, parseErr := h5AuditPB(reply)
		base, baseErr := h5AuditPB(fields[1].b)
		ret, explicit := base[1]
		if parseErr == nil && baseErr == nil && explicit && ret.wire == 0 {
			item["business_ret"] = int32(ret.n)
			if int32(ret.n) == 0 {
				h5AuditAuthorizeWeb(ctx, string(fields[2].b), auth, input.RedirectURI, root, item)
			}
		} else {
			item["parse_error"] = "missing explicit A8Key status"
		}
	}
	emit(item)
	control("control_after_a8key")
}

func h5AuditAuthorizeWeb(ctx context.Context, full string, auth oauth.Result, callback, root string, item map[string]any) {
	u, err := url.Parse(full)
	if err != nil || u.Scheme != "https" || u.Host != "open.weixin.qq.com" || u.Path != "/connect/oauth2/authorize" || u.User != nil || u.Query().Get("state") != auth.State {
		item["web_error"] = "unexpected A8Key authorization URL"
		return
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	defer transport.CloseIdleConnections()
	jar, _ := cookiejar.New(nil)
	client := &http.Client{Transport: transport, Jar: jar, Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	referer := u.String()
	for step := 0; step < 2; step++ {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
		req.Header.Set("User-Agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 13_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.43")
		if step > 0 {
			req.Header.Set("Referer", referer)
		}
		response, err := client.Do(req)
		if err != nil {
			item["web_error"] = "authorization request failed"
			return
		}
		body, readErr := io.ReadAll(io.LimitReader(response.Body, (1<<20)+1))
		response.Body.Close()
		item[fmt.Sprintf("web_http_%d", step+1)] = response.StatusCode
		if readErr != nil || len(body) > 1<<20 {
			item["web_error"] = "authorization page read failed"
			return
		}
		if h5AuditPrivate(filepath.Join(root, fmt.Sprintf("authorize-private-%d.json", step)), map[string]any{"body": body, "location": response.Header.Get("Location")}) != nil {
			item["web_error"] = "private record failed"
			return
		}
		if response.StatusCode >= 300 && response.StatusCode < 400 {
			code, err := h5AuditCallback(response.Header.Get("Location"), callback, auth.State)
			item["callback_verified"], item["code_length"] = err == nil && code != "", len(code)
			if err != nil {
				item["web_error"] = err.Error()
			}
			return
		}
		if step == 0 && response.StatusCode == 200 {
			next, _ := issue75AuthorizeReply(body, u, auth.Scope)
			if next != nil && next.Scheme == "https" && next.Host == "open.weixin.qq.com" && next.Path == "/connect/oauth2/authorize_reply" && next.User == nil {
				u = next
				continue
			}
		}
		item["web_error"] = "authorization not completed"
		return
	}
}

func TestH5CompareCallbackValidation(t *testing.T) {
	callback := "https://example.com/login?tenant=one"
	for _, tc := range []struct {
		name, location string
		ok             bool
	}{
		{"matching", callback + "&code=synthetic&state=expected", true},
		{"wrong host", "https://other.example/login?tenant=one&code=x&state=expected", false},
		{"wrong state", callback + "&code=x&state=other", false},
		{"duplicate state", callback + "&code=x&state=expected&state=other", false},
		{"missing code", callback + "&state=expected", false},
		{"changed callback query", "https://example.com/login?tenant=two&code=x&state=expected", false},
		{"fragment", callback + "&code=x&state=expected#unexpected", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := h5AuditCallback(tc.location, callback, "expected")
			if (err == nil) != tc.ok {
				t.Fatal("callback validation mismatch")
			}
		})
	}
}

func TestH5CompareRequiresBusinessStatus(t *testing.T) {
	location := []byte("https://example.com/login?code=synthetic&state=expected")
	for _, base := range [][]byte{nil, pbVar(1, 0), pbVar(1, uint64(10004))} {
		raw := pbLen(6, location)
		if base != nil {
			raw = append(pbLen(1, base), raw...)
		}
		code, _, err := h5AuditOAuthResult(raw, "https://example.com/login", "expected", 0)
		wantOK := bytes.Equal(base, pbVar(1, 0))
		if (err == nil && code != "") != wantOK {
			t.Fatal("business status validation mismatch")
		}
	}
	if _, err := h5AuditPB([]byte{0x0a, 0x20, 0x01}); err == nil {
		t.Fatal("truncated payload accepted")
	}
}
