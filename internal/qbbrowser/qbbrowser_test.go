package qbbrowser

import (
	"context"
	"encoding/base64"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protowire"
)

// craftBuffer builds a login-buffer protobuf with the given host appid in
// field 3 (field 1/2 are filler bytes like a real ticket carries).
func craftBuffer(hostAppID string) string {
	var raw []byte
	raw = protowire.AppendTag(raw, 1, protowire.BytesType)
	raw = protowire.AppendBytes(raw, []byte("ticket-bytes"))
	raw = protowire.AppendTag(raw, 2, protowire.BytesType)
	raw = protowire.AppendBytes(raw, []byte("device-id"))
	raw = protowire.AppendTag(raw, 3, protowire.BytesType)
	raw = protowire.AppendBytes(raw, []byte(hostAppID))
	return base64.StdEncoding.EncodeToString(raw)
}

func gatewayResponse(buffer string) *http.Response {
	header := http.Header{}
	header.Set("Content-Type", "application/json")
	body := `{"login_buffer":"` + buffer + `","err_code":0}`
	return &http.Response{StatusCode: 200, Header: header, Body: io.NopCloser(strings.NewReader(body))}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestValidate(t *testing.T) {
	if err := (Credentials{QBAccessToken: "tok"}).Validate(); err == nil {
		t.Fatal("missing user id must fail")
	}
	if err := (Credentials{QBAccessToken: "tok", UserID: "u1", GUID: "g"}).Validate(); err != nil {
		t.Fatalf("valid credentials rejected: %v", err)
	}
}

func TestIdentity(t *testing.T) {
	if got := (Credentials{UserID: "u9"}).Identity(); got != "qbbrowser:u9" {
		t.Fatalf("identity = %q", got)
	}
}

func TestFetchBufferRejectsGatewayError(t *testing.T) {
	rt := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		header := http.Header{}
		header.Set("Content-Type", "application/json")
		return &http.Response{StatusCode: 200, Header: header,
			Body: io.NopCloser(strings.NewReader(`{"login_buffer":"","err_code":10002}`))}, nil
	})
	_, err := FetchBuffer(context.Background(), &http.Client{Transport: rt}, Credentials{QBAccessToken: "tok", UserID: "u1"})
	var rejected *TokenRejectedError
	if !errors.As(err, &rejected) || rejected.Code != 10002 {
		t.Fatalf("expected TokenRejectedError 10002, got %v", err)
	}
}

func TestFetchBufferValidatesHostAppID(t *testing.T) {
	// A buffer whose field 3 does not carry the QQ Browser host appid must be
	// refused, exactly like the other providers' ticket checks.
	rt := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return gatewayResponse(craftBuffer("wx5cd60c5d4817a188")), nil
	})
	if _, err := FetchBuffer(context.Background(), &http.Client{Transport: rt}, Credentials{QBAccessToken: "tok", UserID: "u1"}); err == nil {
		t.Fatal("foreign host appid must be refused")
	}
}

func TestFetchBufferAcceptsOwnHostAppID(t *testing.T) {
	rt := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return gatewayResponse(craftBuffer(HostAppID)), nil
	})
	buffer, err := FetchBuffer(context.Background(), &http.Client{Transport: rt}, Credentials{QBAccessToken: "tok", UserID: "u1"})
	if err != nil || buffer == "" {
		t.Fatalf("own buffer refused: %v", err)
	}
}

func TestFetchBufferEmptyCredentials(t *testing.T) {
	if _, err := FetchBuffer(context.Background(), nil, Credentials{}); err == nil {
		t.Fatal("empty credentials must fail")
	}
}
