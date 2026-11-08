package qbbrowser

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protowire"
)

func authReply(method string, body []byte) []byte {
	p := pbBytes(nil, 2, []byte(wxServant))
	p = pbBytes(p, 3, []byte(method))
	p = pbBytes(p, 4, body)
	f := make([]byte, 4)
	binary.BigEndian.PutUint32(f, uint32(len(p)+4))
	return append(f, p...)
}

func tokenBody(identity string) []byte {
	b := pbBytes(nil, 1, nil) // proto3 success header can be empty
	b = pbBytes(b, 2, []byte(identity))
	b = pbBytes(b, 3, []byte("new-social-refresh"))
	b = pbBytes(b, 4, []byte("new-social-access"))
	b = protowire.AppendTag(b, 5, protowire.VarintType)
	return protowire.AppendVarint(b, 7200)
}

func TestWXAuthNativeWireAndDeviceFreeTicket(t *testing.T) {
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requests++
		if r.URL.String() == BufferURL {
			body, _ := io.ReadAll(r.Body)
			if strings.Contains(string(body), "user_id") || strings.Contains(string(body), "guid") || r.Header.Get("x-qbrn-cookie") != "" {
				t.Fatal("standalone flow invented a browser device")
			}
			return gatewayResponse(craftBuffer(HostAppID)), nil
		}
		if r.URL.String() != PBProxyURL || r.Method != "POST" {
			t.Fatal("not a native PBProxy request")
		}
		raw, _ := io.ReadAll(r.Body)
		if int(binary.BigEndian.Uint32(raw)) != len(raw) {
			t.Fatal("framing excludes its four-byte prefix")
		}
		envelope, err := readPB(raw[4:])
		if err != nil {
			t.Fatal(err)
		}
		if envelope[1].n != 100 || string(envelope[2].b) != "trpc.mtt.wx_auth.wx_auth" || string(envelope[3].b) != "/trpc.mtt.wx_auth.wx_auth/WXAuth" {
			t.Fatal("native servant/method fields reversed or wrong")
		}
		body, _ := readPB(envelope[4].b)
		if len(body[1].b) != 0 || string(body[3].b) != "synthetic-code" || string(body[4].b) != HostAppID {
			t.Fatal("WXAuth request fields mismatch")
		}
		call, _ := readPB(body[2].b)
		if string(call[1].b) != "qb" {
			t.Fatal("native AuthCall missing")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(authReply("WXAuth", pbBytes(tokenBody("test-account"), 7, []byte("synthetic-union")))))}, nil
	})}
	result, err := ExchangeCode(context.Background(), client, "synthetic-code")
	if err != nil || result.ExpiresIn != 7200 || !result.Credentials.CanRefresh() {
		t.Fatalf("WXAuth: %v", err)
	}
	if result.Credentials.QBAccessToken != "new-social-access" || result.Credentials.UserID != "" || result.Credentials.UnionID != "synthetic-union" {
		t.Fatal("social token or device mapping incorrect")
	}
	if _, err := FetchBuffer(context.Background(), client, result.Credentials); err != nil {
		t.Fatal(err)
	}
	if requests != 2 {
		t.Fatal("unexpected upstream calls")
	}
}

func TestWXRefreshNativeWireAndAccountBinding(t *testing.T) {
	for _, identity := range []string{"test-account", "different-account"} {
		client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			raw, _ := io.ReadAll(r.Body)
			env, _ := readPB(raw[4:])
			body, _ := readPB(env[4].b)
			if string(env[3].b) != "/trpc.mtt.wx_auth.wx_auth/RefreshToken" || string(body[3].b) != "old-social-refresh" || string(body[4].b) != HostAppID || string(body[5].b) != "test-account" {
				t.Fatal("QB/social token refresh chain confused")
			}
			return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(authReply("RefreshToken", tokenBody(identity))))}, nil
		})}
		result, err := Refresh(context.Background(), client, Credentials{OpenID: "test-account", WXRefreshToken: "old-social-refresh", QBAccessToken: "old"})
		if identity == "test-account" {
			if err != nil || result.Credentials.WXRefreshToken != "new-social-refresh" {
				t.Fatal("refresh rotation not preserved")
			}
		} else if err == nil {
			t.Fatal("refresh changed account identity")
		}
	}
}

func TestAuthRejectsMalformedAndLeakingReplies(t *testing.T) {
	valid := authReply("WXAuth", tokenBody("test-account"))
	header := protowire.AppendTag(nil, 1, protowire.VarintType)
	header = protowire.AppendVarint(header, 40029)
	header = pbBytes(header, 2, []byte("invalid code: synthetic-secret"))
	rejected := authReply("WXAuth", pbBytes(nil, 1, header))
	for name, raw := range map[string][]byte{
		"business rejection": rejected,
		"short frame":        {0, 0, 0, 4},
		"truncated":          valid[:len(valid)-1],
		"wrong route":        authReply("Other", tokenBody("test-account")),
		"missing header":     authReply("WXAuth", pbBytes(nil, 4, []byte("access"))),
		"duplicate tokens":   authReply("WXAuth", pbBytes(tokenBody("test-account"), 4, []byte("another"))),
	} {
		t.Run(name, func(t *testing.T) {
			client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(raw))}, nil
			})}
			_, err := ExchangeCode(context.Background(), client, "synthetic-secret")
			if err == nil || strings.Contains(err.Error(), "synthetic-secret") {
				t.Fatal("malformed/rejected response accepted or leaked")
			}
			if name == "business rejection" {
				var rejected *AuthError
				if !errors.As(err, &rejected) || rejected.Code != 40029 {
					t.Fatal("typed business error missing")
				}
			}
		})
	}
}

func TestWXAuthNoLifetimeIsNotInvented(t *testing.T) {
	b := pbBytes(nil, 1, nil)
	b = pbBytes(b, 2, []byte("account"))
	b = pbBytes(b, 3, []byte("refresh"))
	b = pbBytes(b, 4, []byte("access"))
	client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(authReply("WXAuth", b)))}, nil
	})}
	result, err := ExchangeCode(context.Background(), client, "synthetic")
	if err != nil || result.ExpiresIn != 0 {
		t.Fatal("missing upstream lifetime became a local renewal")
	}
}
