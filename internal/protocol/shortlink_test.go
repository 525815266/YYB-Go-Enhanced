package protocol

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"testing"
)

func TestShortlinkAuthenticatedRejectionPreservesCode(t *testing.T) {
	psk := bytes.Repeat([]byte{7}, 32)
	hello, serverHello, type8 := []byte("synthetic-client"), []byte("synthetic-server"), []byte("synthetic-type8")
	hash := sha256.Sum256(append(append([]byte(nil), hello...), serverHello...))
	keys := derivePSKOneWayKeys(psk, labelHandshakeKeys, hash[:])
	code := int64(-14)
	header := wpkgHead(map[int]uint64{4: 0, 23: uint64(code), 25: 0}, nil)
	application := buildShortlink(transferCmd, 0, header, defaultVer)
	sealed, err := encryptRecordPayload(keys.Key, keys.IV, 2, ctAppData, application)
	if err != nil {
		t.Fatal(err)
	}
	first, _ := buildRecord(ctHandshake, serverHello)
	second, _ := buildRecord(ctAppData, sealed)
	_, _, err = parse0RTTResponse(append(first, second...), psk, hello, type8, bytes.Repeat([]byte{3}, 24))
	var rejected *ShortlinkRejectedError
	if !errors.As(err, &rejected) || rejected.Code != -14 || rejected.Transport || !errors.Is(err, ErrShortlinkPayload) {
		t.Fatalf("authenticated upstream rejection lost: %v", err)
	}
}

func TestLoginCodeRebuildPreservesFirstRejection(t *testing.T) {
	first := &ShortlinkRejectedError{Code: -14}
	second := &ManualAuthRejectedError{Code: -10}
	calls := 0
	_, err := retryLoginCode(context.Background(), func() (map[string]any, error) {
		calls++
		if calls == 1 {
			return nil, first
		}
		return nil, second
	}, func() error { return nil })
	if calls != 2 || !errors.Is(err, first) || !errors.Is(err, second) {
		t.Fatalf("rebuild lost first rejection: calls=%d err=%v", calls, err)
	}
}

func TestShortlinkRejectionRequiresEmptyBody(t *testing.T) {
	negative := int64(-14)
	header := wpkgHead(map[int]uint64{4: 0, 23: uint64(negative)}, nil)
	if shortlinkHeaderRejection(append(header, []byte("encrypted-body")...)) != nil {
		t.Fatal("header replaced a nonempty encrypted body")
	}
	transportCode := int64(-20011)
	err := shortlinkHeaderRejection(wpkgHead(map[int]uint64{4: uint64(transportCode), 23: 0}, nil))
	var rejection *ShortlinkRejectedError
	if !errors.As(err, &rejection) || !rejection.Transport || rejection.Code != -20011 {
		t.Fatal("transport rejection lost")
	}
	if shortlinkHeaderRejection(wpkgHead(map[int]uint64{4: 0, 23: 0}, nil)) != nil {
		t.Fatal("successful empty header invented a rejection")
	}
}

func TestShortlinkRejectsUndersizedLength(t *testing.T) {
	if _, err := parseShortlink(make([]byte, 16)); err == nil {
		t.Fatal("invalid total length accepted")
	}
}
