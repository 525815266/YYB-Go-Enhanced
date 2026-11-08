package qbbrowser

import (
	"context"
	"encoding/base64"
	"encoding/json"
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
	if (Credentials{OpenID: "account-a", UserID: "same-machine"}).Identity() == (Credentials{OpenID: "account-b", UserID: "same-machine"}).Identity() {
		t.Fatal("different accounts on one machine must not overwrite each other")
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

func TestFetchBufferRejectsMalformedBusinessCode(t *testing.T) {
	for _, code := range []string{`,"err_code":null`, `,"err_code":"0"`} {
		client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"login_buffer":"` + craftBuffer(HostAppID) + `"` + code + `}`))}, nil
		})}
		if _, err := FetchBuffer(context.Background(), client, Credentials{QBAccessToken: "synthetic", UserID: "machine"}); err == nil {
			t.Fatal("null or non-numeric success must not accept a ticket")
		}
	}
}

func TestFetchBufferDefaultCodeStillValidatesTicket(t *testing.T) {
	for _, ticket := range []string{craftBuffer(HostAppID), craftBuffer("wrong-host"), ""} {
		client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"login_buffer":"` + ticket + `"}`))}, nil
		})}
		_, err := FetchBuffer(context.Background(), client, Credentials{QBAccessToken: "synthetic", UserID: "machine"})
		if (err == nil) != (ticket == craftBuffer(HostAppID)) {
			t.Fatal("omitted default code bypassed ticket validation or rejected a valid ticket")
		}
	}
}

func TestFetchBufferOfficialDeviceParameters(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		var body map[string]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["access_token"] != "browser-ctk" || body["user_id"] != "machine" || body["guid"] != "browser-guid" || r.Header.Get("x-qbrn-cookie") != "QIMEI36=synthetic" {
			t.Fatal("official ctk/device parameters not forwarded")
		}
		return gatewayResponse(craftBuffer(HostAppID)), nil
	})}
	_, err := FetchBuffer(context.Background(), client, Credentials{QBAccessToken: "browser-ctk", UserID: "machine", GUID: "browser-guid", QBRNCookie: "QIMEI36=synthetic"})
	if err != nil {
		t.Fatal(err)
	}
}

func TestParsePBFieldsWireTypes(t *testing.T) {
	bad := protowire.AppendTag(nil, 1, protowire.VarintType)
	bad = protowire.AppendVarint(bad, 123)
	if _, err := parsePBFields(bad); err == nil {
		t.Fatal("integer ticket accepted")
	}
	good := protowire.AppendTag(nil, 9, protowire.Fixed64Type)
	good = protowire.AppendFixed64(good, 123)
	good = protowire.AppendTag(good, 1, protowire.BytesType)
	good = protowire.AppendBytes(good, []byte("ticket"))
	if fields, err := parsePBFields(good); err != nil || string(fields[1]) != "ticket" {
		t.Fatal("unknown fixed64 field not skipped")
	}
	if _, err := parsePBFields([]byte{0x4a, 0xff}); err == nil {
		t.Fatal("truncated field accepted")
	}
}
