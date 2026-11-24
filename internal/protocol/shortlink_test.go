package protocol

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
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

func TestShortlinkResponseFailureStages(t *testing.T) {
	psk := bytes.Repeat([]byte{7}, 32)
	hello, serverHello := []byte("synthetic-client"), []byte("synthetic-server")
	hash := sha256.Sum256(append(append([]byte(nil), hello...), serverHello...))
	keys := derivePSKOneWayKeys(psk, labelHandshakeKeys, hash[:])
	first, _ := buildRecord(ctHandshake, serverHello)
	application, err := encryptRecordPayload(keys.Key, keys.IV, 2, ctAppData, []byte("invalid-application-ciphertext"))
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name            string
		data            []byte
		payloadCategory bool
	}{
		{"mmtls_records", []byte("not-mmtls-records"), false},
		{"mmtls_decrypt", []byte("invalid-record-ciphertext"), true},
		{"session_payload", application, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			wire := tc.data
			if tc.name != "mmtls_records" {
				second, _ := buildRecord(ctAppData, tc.data)
				wire = append(append([]byte(nil), first...), second...)
			}
			_, _, err := parse0RTTResponse(wire, psk, hello, nil, bytes.Repeat([]byte{3}, 32))
			var response *ShortlinkResponseError
			if !errors.As(err, &response) || response.Stage != tc.name || errors.Is(err, ErrShortlinkPayload) != tc.payloadCategory {
				t.Fatalf("response stage or recovery category changed: %v", err)
			}
		})
	}
}

func TestLoginCodeRebuildPreservesFirstRejection(t *testing.T) {
	first := &ShortlinkRejectedError{Code: -20011, Transport: true}
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

func TestApplicationRejectionKeepsSessionWithoutRetry(t *testing.T) {
	first := &ShortlinkRejectedError{Code: -14}
	calls := 0
	_, err := retryLoginCode(context.Background(), func() (map[string]any, error) {
		calls++
		return nil, first
	}, func() error {
		t.Fatal("application refusal must not invalidate the shared session")
		return nil
	})
	if calls != 1 || !errors.Is(err, first) || !IsApplicationRequestRejection(err) {
		t.Fatal("application rejection retried or lost its diagnostic")
	}
	for _, other := range []error{nil, &ShortlinkRejectedError{Code: -14, Transport: true},
		&ShortlinkRejectedError{Code: -1}, errors.Join(first, &ManualAuthRejectedError{Code: -10}),
		errors.Join(first, &ShortlinkRejectedError{Code: -20011, Transport: true}),
		errors.Join(first, &RefreshRejectedError{Code: -109})} {
		if IsApplicationRequestRejection(other) {
			t.Fatal("request-level protection suppressed another recovery condition")
		}
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

func startShortlinkMockServer(t *testing.T, handler func(conn net.Conn, reqBody []byte) []byte) (Target, *atomic.Int32) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var hits atomic.Int32
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			hits.Add(1)
			go func(c net.Conn) {
				defer c.Close()
				br := bufio.NewReader(c)
				contentLen := 0
				for {
					line, err := br.ReadString('\n')
					if err != nil {
						return
					}
					trimmed := strings.TrimSpace(line)
					if trimmed == "" {
						break
					}
					if strings.HasPrefix(strings.ToLower(trimmed), "content-length:") {
						contentLen, _ = strconv.Atoi(strings.TrimSpace(trimmed[len("content-length:"):]))
					}
				}
				reqBody := make([]byte, contentLen)
				if _, err := io.ReadFull(br, reqBody); err != nil {
					return
				}
				respBody := handler(c, reqBody)
				if respBody == nil {
					return
				}
				resp := append([]byte(fmt.Sprintf("HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n", len(respBody))), respBody...)
				_, _ = c.Write(resp)
			}(conn)
		}
	}()
	t.Cleanup(func() {
		_ = ln.Close()
		<-done
	})
	addr := ln.Addr().(*net.TCPAddr)
	return Target{IP: "127.0.0.1", Port: addr.Port}, &hits
}

func build0RTTTestResponse(t *testing.T, psk, reqBody, application []byte) []byte {
	t.Helper()
	recs, _ := parseRecords(reqBody)
	if len(recs) == 0 {
		t.Fatal("request contained no MMTLS records")
	}
	pskCH := recs[0].Payload
	serverHello := []byte("synthetic-server-hello")
	hash := sha256.Sum256(append(append([]byte(nil), pskCH...), serverHello...))
	keys := derivePSKOneWayKeys(psk, labelHandshakeKeys, hash[:])
	sealed, err := encryptRecordPayload(keys.Key, keys.IV, 2, ctAppData, application)
	if err != nil {
		t.Fatal(err)
	}
	r0, err := buildRecord(ctHandshake, serverHello)
	if err != nil {
		t.Fatal(err)
	}
	r1, err := buildRecord(ctAppData, sealed)
	if err != nil {
		t.Fatal(err)
	}
	return append(r0, r1...)
}

func TestSend0RTTFailsOverFromBlackholedTargetAndDemotesIt(t *testing.T) {
	resetShortlinkHealth()
	t.Cleanup(resetShortlinkHealth)

	psk := bytes.Repeat([]byte{7}, 32)
	recvKey := bytes.Repeat([]byte{4}, 32)
	entry := pskEntry{
		PSKType:      1,
		PreSharedKey: hex.EncodeToString(psk),
		TicketEntry:  "aabbcc",
	}

	var badHits atomic.Int32
	badTarget, _ := startShortlinkMockServer(t, func(c net.Conn, _ []byte) []byte {
		badHits.Add(1)
		// Reproduce 43.137.142.113:80 stalling until client times out or closes.
		_, _ = io.Copy(io.Discard, c)
		return nil
	})
	goodTarget, goodHits := startShortlinkMockServer(t, func(_ net.Conn, reqBody []byte) []byte {
		inner := pbLen(2, pbLen(3, []byte("ok-code-failover")))
		encInner, err := aesGCMEncryptLayout(recvKey, nil, lz4AllLiteral(inner))
		if err != nil {
			t.Fatal(err)
		}
		app := buildShortlink(transferCmd, 0, encInner, defaultVer)
		return build0RTTTestResponse(t, psk, reqBody, app)
	})

	targets := []Target{badTarget, goodTarget}
	// First call: budget splits across [bad, good], so bad stalls ~300ms then fails over cleanly to good within 700ms.
	ctx, cancel := context.WithTimeout(context.Background(), 700*time.Millisecond)
	defer cancel()
	code, _, err := send0RTT(ctx, targets, entry, recvKey, []byte("envelope"), 700*time.Millisecond, "", false, true)
	if err != nil || string(code) != "ok-code-failover" {
		t.Fatalf("first call failed instead of failing over: code=%q err=%v", string(code), err)
	}
	if badHits.Load() != 1 || goodHits.Load() != 1 {
		t.Fatalf("unexpected hit counts on failover: bad=%d good=%d", badHits.Load(), goodHits.Load())
	}

	// Second call: demoted badTarget is skipped in favor of healthy goodTarget immediately.
	ctx2, cancel2 := context.WithTimeout(context.Background(), 700*time.Millisecond)
	defer cancel2()
	code, _, err = send0RTT(ctx2, targets, entry, recvKey, []byte("envelope"), 700*time.Millisecond, "", false, true)
	if err != nil || string(code) != "ok-code-failover" {
		t.Fatalf("second call failed: code=%q err=%v", string(code), err)
	}
	if badHits.Load() != 1 || goodHits.Load() != 2 {
		t.Fatalf("demoted blackhole target was hit again: bad=%d good=%d", badHits.Load(), goodHits.Load())
	}
}

func TestSend0RTTStopsImmediatelyOnAuthenticatedRejection(t *testing.T) {
	resetShortlinkHealth()
	t.Cleanup(resetShortlinkHealth)

	psk := bytes.Repeat([]byte{9}, 32)
	recvKey := bytes.Repeat([]byte{5}, 32)
	entry := pskEntry{
		PSKType:      1,
		PreSharedKey: hex.EncodeToString(psk),
		TicketEntry:  "ddeeff",
	}

	firstTarget, firstHits := startShortlinkMockServer(t, func(_ net.Conn, reqBody []byte) []byte {
		code := int64(-14)
		header := wpkgHead(map[int]uint64{4: 0, 23: uint64(code), 25: 0}, nil)
		app := buildShortlink(transferCmd, 0, header, defaultVer)
		return build0RTTTestResponse(t, psk, reqBody, app)
	})
	secondTarget, secondHits := startShortlinkMockServer(t, func(_ net.Conn, _ []byte) []byte {
		t.Error("second ShortLink target must not receive replayed -14 request")
		return nil
	})

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	_, _, err := send0RTT(ctx, []Target{firstTarget, secondTarget}, entry, recvKey, []byte("envelope"), time.Second, "", false, true)
	var rejected *ShortlinkRejectedError
	if !errors.As(err, &rejected) || rejected.Code != -14 {
		t.Fatalf("expected ShortlinkRejectedError -14, got %v", err)
	}
	if firstHits.Load() != 1 || secondHits.Load() != 0 {
		t.Fatalf("expected immediate stop on -14: first=%d second=%d", firstHits.Load(), secondHits.Load())
	}
}

func TestWrapTimeoutStageRecognizesSocketIOTimeout(t *testing.T) {
	badTarget, _ := startShortlinkMockServer(t, func(c net.Conn, _ []byte) []byte {
		_, _ = io.Copy(io.Discard, c)
		return nil
	})
	_, _, rawErr := send0RTTRaw(context.Background(), badTarget, pskEntry{PreSharedKey: "00", TicketEntry: "00"}, nil, []byte("test"), 80*time.Millisecond, "", false)
	if rawErr == nil || !IsTimeoutError(rawErr) {
		t.Fatalf("expected socket i/o timeout to match IsTimeoutError, got %v", rawErr)
	}
	staged := wrapTimeoutStage("shortlink_exchange", rawErr)
	var stageErr *TimeoutStageError
	if !errors.As(staged, &stageErr) || stageErr.Stage != "shortlink_exchange" {
		t.Fatalf("wrapTimeoutStage did not tag socket timeout: %#v (err=%v)", staged, staged)
	}
}
