package ldplayer

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protowire"
)

func craftBuffer(hostAppID string) string {
	var raw []byte
	raw = protowire.AppendTag(raw, 1, protowire.BytesType)
	raw = protowire.AppendBytes(raw, []byte("ticket"))
	raw = protowire.AppendTag(raw, 2, protowire.BytesType)
	raw = protowire.AppendBytes(raw, []byte("device"))
	raw = protowire.AppendTag(raw, 3, protowire.BytesType)
	raw = protowire.AppendBytes(raw, []byte(hostAppID))
	return base64.StdEncoding.EncodeToString(raw)
}

func gatewayResponse(code int, data string) *http.Response {
	header := http.Header{}
	header.Set("Content-Type", "application/json")
	body := `{"code":` + itoa(code) + `,"data":"` + data + `"}`
	return &http.Response{StatusCode: 200, Header: header, Body: io.NopCloser(strings.NewReader(body))}
}

func itoa(v int) string {
	return strings.TrimSpace(strings.Repeat("", 0) + json.Number(strings.Repeat("", 0)).String()[:0] + intToString(v))
}

func intToString(v int) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var buf [20]byte
	pos := len(buf)
	for v > 0 {
		pos--
		buf[pos] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		pos--
		buf[pos] = '-'
	}
	return string(buf[pos:])
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestValidateBuffer(t *testing.T) {
	if err := ValidateBuffer(craftBuffer(HostAppID)); err != nil {
		t.Fatalf("own buffer refused: %v", err)
	}
	if err := ValidateBuffer(craftBuffer("wx0000000000000000")); err == nil {
		t.Fatal("foreign host appid must be refused")
	}
	if err := ValidateBuffer(""); err == nil {
		t.Fatal("empty buffer must fail")
	}
}

func TestFetchBufferSuccess(t *testing.T) {
	rt := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.Host != "game-service.ldmnq.com" || r.Method != http.MethodPost {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL)
		}
		return gatewayResponse(200, craftBuffer(HostAppID)), nil
	})
	buf, err := FetchBuffer(context.Background(), &http.Client{Transport: rt}, "oauth-code-123")
	if err != nil || buf == "" {
		t.Fatalf("fetch failed: %v", err)
	}
}

func TestFetchBufferRejectsNon200(t *testing.T) {
	rt := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return gatewayResponse(40001, ""), nil
	})
	if _, err := FetchBuffer(context.Background(), &http.Client{Transport: rt}, "code"); err == nil {
		t.Fatal("business rejection must surface")
	}
}

func TestFetchBufferEmptyCode(t *testing.T) {
	if _, err := FetchBuffer(context.Background(), nil, "  "); err == nil {
		t.Fatal("empty code must fail")
	}
}

func TestHostAppID(t *testing.T) {
	if HostAppID != "wx56135c126042c9e7" {
		t.Fatalf("HostAppID = %q", HostAppID)
	}
}
