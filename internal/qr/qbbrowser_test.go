package qr

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"io"
	"net/http"
	"net/url"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protowire"
	"yyb_go/internal/protocol"
	"yyb_go/internal/qbbrowser"
)

func qbField(dst []byte, n protowire.Number, value string) []byte {
	dst = protowire.AppendTag(dst, n, protowire.BytesType)
	return protowire.AppendBytes(dst, []byte(value))
}

func TestQQOAuthRequestsRuntimePermissionAndFreshState(t *testing.T) {
	previous := ""
	for i := 0; i < 2; i++ {
		raw, err := OAuthURL("qbbrowser")
		if err != nil {
			t.Fatal(err)
		}
		u, _ := url.Parse(raw)
		q := u.Query()
		if q.Get("appid") != qbbrowser.HostAppID || q.Get("scope") != "snsapi_login,snsapi_runtime_pcsdk" || q.Get("state") == "" || q.Get("state") == previous {
			t.Fatal("QQ runtime permission or per-session state missing")
		}
		previous = q.Get("state")
	}
}

func TestQQTicketRetryDoesNotReplayOAuthCode(t *testing.T) {
	authCalls, ticketCalls := 0, 0
	hc := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.String() == qbbrowser.PBProxyURL {
			authCalls++
			body := qbField(nil, 1, "")
			body = qbField(body, 2, "synthetic-openid")
			body = qbField(body, 3, "synthetic-refresh")
			body = qbField(body, 4, "synthetic-access")
			body = protowire.AppendTag(body, 5, protowire.VarintType)
			body = protowire.AppendVarint(body, 7200)
			body = qbField(body, 6, "snsapi_login,snsapi_runtime_pcsdk")
			envelope := qbField(nil, 2, "trpc.mtt.wx_auth.wx_auth")
			envelope = qbField(envelope, 3, "WXAuth")
			envelope = qbField(envelope, 4, string(body))
			raw := make([]byte, 4)
			binary.BigEndian.PutUint32(raw, uint32(len(envelope)+4))
			raw = append(raw, envelope...)
			return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(raw))}, nil
		}
		if r.URL.String() != qbbrowser.BufferURL {
			t.Fatal("unexpected channel endpoint")
		}
		ticketCalls++
		payload := `{"err_code":10002}`
		if ticketCalls > 1 {
			ticket := qbField(qbField(qbField(nil, 1, "synthetic-ticket"), 2, "synthetic-device"), 3, qbbrowser.HostAppID)
			payload = `{"login_buffer":"` + base64.StdEncoding.EncodeToString(ticket) + `"}`
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewBufferString(payload))}, nil
	})}
	c := &Client{loginBuffers: protocol.NewLoginBufferClientWithHTTPClient(hc, time.Second)}
	sess := &Session{Product: "qbbrowser", HTTPClient: hc, AuthorizeCode: "single-use-code"}
	if _, err := c.GetLoginBuffer(context.Background(), sess); err == nil {
		t.Fatal("first ticket rejection accepted")
	}
	result, err := c.GetLoginBuffer(context.Background(), sess)
	if err != nil || authCalls != 1 || ticketCalls != 2 {
		t.Fatalf("auth=%d ticket=%d err=%v", authCalls, ticketCalls, err)
	}
	if result.Credentials.ExpiresIn != 7200 || !result.Credentials.QBBrowser.CanRefresh() || result.Credentials.Provider != "qbbrowser" {
		t.Fatal("native credentials lost")
	}
	if _, err = c.GetLoginBuffer(context.Background(), sess); err != nil || authCalls != 1 || ticketCalls != 2 {
		t.Fatal("confirmed session repeated upstream requests")
	}
}

func TestQQFailedOAuthIsNotReplayedByRepeatedConfirmation(t *testing.T) {
	calls := 0
	hc := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) { calls++; return nil, context.DeadlineExceeded })}
	sess := &Session{Product: "qbbrowser", HTTPClient: hc, AuthorizeCode: "single-use-code"}
	for i := 0; i < 2; i++ {
		if _, err := (&Client{}).GetLoginBuffer(context.Background(), sess); err == nil {
			t.Fatal("failed authentication accepted")
		}
	}
	if calls != 1 {
		t.Fatal("ambiguous OAuth exchange replayed")
	}
}
