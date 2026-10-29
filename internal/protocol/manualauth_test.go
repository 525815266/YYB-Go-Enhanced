package protocol

import (
	"bytes"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"strings"
	"testing"
)

func TestManualAuthRequestMatchesOfficialFieldLayout(t *testing.T) {
	// Generated with tools/inspect_native_manualauth.py using synthetic data.
	// Official ilink2.dll serializers: AppBaseRequest 0x168280, ManualAuthRequest
	// 0x169ab0; builder 0x1032d4 sets the outer field 2 to 1901.
	buffer := append(pbLen(1, []byte("synthetic-ticket")), pbLen(2, []byte("ticket-device"))...)
	got, err := buildManualAuthRequest(base64.StdEncoding.EncodeToString(buffer), []byte("0123456789abcdef"))
	if err != nil {
		t.Fatal(err)
	}
	want, err := hex.DecodeString("0a120a103031323334353637383961626364656610ed0e1a120a1073796e7468657469632d7469636b6574200438004006")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("synthetic ManualAuthRequest differs from official layout:\n got %x\nwant %x", got, want)
	}
}

func TestManualAuthBusinessRejectionPrecedesSessionParsing(t *testing.T) {
	// Shape observed from the official server with an old saved login buffer.
	base := append(pbVar(1, ^uint64(9)), pbLen(2, []byte("private diagnostic"))...)
	response := append(pbLen(1, base), pbLen(3, append(pbVar(4, 0), pbLen(5, nil)...))...)
	_, err := extractSession(response)
	var rejection *ManualAuthRejectedError
	if !errors.As(err, &rejection) || rejection.Code != -10 {
		t.Fatalf("expected upstream rejection -10, got %v", err)
	}
	if strings.Contains(err.Error(), "private diagnostic") {
		t.Fatal("upstream diagnostic leaked into public error")
	}
}

func TestManualAuthSuccessAndMalformedSession(t *testing.T) {
	session := append(pbLen(1, []byte("send")), pbLen(2, []byte("recv"))...)
	body := append(pbLen(2, session), pbLen(3, pbVar(1, 123))...)
	response := append(pbLen(1, pbVar(1, 0)), pbLen(3, body)...)
	got, err := extractSession(response)
	if err != nil || got.UIN != 123 {
		t.Fatalf("valid session rejected: %v", err)
	}
	_, err = extractSession(pbLen(1, pbVar(1, 0)))
	var rejection *ManualAuthRejectedError
	if err == nil || errors.As(err, &rejection) {
		t.Fatal("malformed success must remain a parsing error")
	}
}
