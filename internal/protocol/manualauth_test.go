package protocol

import (
	"errors"
	"strings"
	"testing"
)

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
