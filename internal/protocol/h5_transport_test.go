package protocol

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"yyb_go/internal/oauth"
	"yyb_go/internal/store"
)

// Receive real wire requests on loopback, then deliberately return an invalid
// response. No account credentials or upstream endpoints are used.
func h5CaptureServer(t *testing.T) (Target, <-chan []byte) {
	t.Helper()
	captured := make(chan []byte, 8)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		captured <- body
		_, _ = w.Write([]byte("synthetic-invalid-response"))
	}))
	t.Cleanup(server.Close)
	host, rawPort, err := net.SplitHostPort(server.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	port, _ := strconv.Atoi(rawPort)
	return Target{IP: host, Port: port}, captured
}

func h5WireTestSession(targets ...Target) WmpfSession {
	return WmpfSession{
		Session:          AppSession{UIN: 9007199254740993, SendKey: bytes.Repeat([]byte{3}, 32), RecvKey: bytes.Repeat([]byte{4}, 32), HostAppID: []byte("wx5cd60c5d4817a188")},
		PSK:              pskEntry{PSKType: 1, PreSharedKey: hex.EncodeToString(bytes.Repeat([]byte{7}, 32)), TicketEntry: "aabbcc", ExpiredAt: time.Now().Add(time.Hour).Unix()},
		ShortlinkTargets: targets,
		Device:           DeviceIdentity{SessDevice: []byte("06-11-22-33-44-55")},
	}
}

func TestH5WireKeepsSessionIdentityAndCommand(t *testing.T) {
	target, captured := h5CaptureServer(t)
	st := h5WireTestSession(target)
	auth := oauthResultForTest(t, "wx1234567890abcdef", "https://example.com/login?tenant=one", "snsapi_userinfo", "expected")
	_, _ = NewPool(DefaultConfig(), nil).requestH5OAuthCode(context.Background(), st, "wx1234567890abcdef", auth)
	if len(captured) != 1 {
		t.Fatal("expected exactly one wire request")
	}
	// Authenticate and decode the actual outgoing MMTLS and application layers.
	records, _ := parseRecords(<-captured)
	if len(records) != 4 {
		t.Fatal("unexpected request records")
	}
	psk, _ := hex.DecodeString(st.PSK.PreSharedKey)
	hash := sha256.Sum256(records[0].Payload)
	keys := derivePSKOneWayKeys(psk, labelEarlyKeys, hash[:])
	envelope, err := decryptRecordPayload(keys.Key, keys.IV, 2, ctAppData, records[2].Payload)
	if err != nil {
		t.Fatal(err)
	}
	offset := 4
	for i := 0; i < 2; i++ {
		size := int(binary.BigEndian.Uint16(envelope[offset:]))
		offset += 2 + size
	}
	packet, err := parseShortlink(envelope[offset+4:])
	if err != nil {
		t.Fatal(err)
	}
	_, _, headerSize, err := decodeWpkgHead(packet.Body)
	if err != nil {
		t.Fatal(err)
	}
	compressed, err := aesGCMDecryptLayout(st.Session.SendKey, nil, packet.Body[headerSize:])
	if err != nil {
		t.Fatal(err)
	}
	plain, err := lz4Decompress(compressed)
	if err != nil {
		t.Fatal(err)
	}
	fields := pbParse(plain)
	session := pbParse(fields[1].([]byte))
	if !bytes.Equal(session[3].([]byte), st.Device.SessDevice) {
		t.Error("H5 generated a different device instead of using the account device")
	}
	if fields[7] != int64(1254) || string(fields[2].([]byte)) != string(h5OAuthURL) || !bytes.Equal(fields[3].([]byte), st.Session.HostAppID) || string(fields[6].([]byte)) != "wx1234567890abcdef" {
		t.Fatal("H5 command, host channel or target identity changed")
	}
	inner := pbParse(fields[5].([]byte))
	if string(inner[2].([]byte)) != auth.FullURL {
		t.Fatal("H5 authorization URL was changed in transit")
	}
}

func TestH5FailureDoesNotReplayOrInvalidate(t *testing.T) {
	for _, proxyFallback := range []bool{false, true} {
		t.Run(strconv.FormatBool(proxyFallback), func(t *testing.T) {
			ctx := context.Background()
			target, captured := h5CaptureServer(t)
			db, err := store.Open(":memory:")
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			acc, err := db.UpsertAccount(ctx, "synthetic-h5-account", "unused-buffer", nil, nil, nil, nil, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			proxy := ""
			if proxyFallback {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusBadGateway) }))
				defer server.Close()
				proxy = "http-connect://" + server.Listener.Addr().String()
			}
			st := h5WireTestSession(Target{IP: "127.0.0.1", Port: 0}, target, target)
			for _, route := range []string{"", proxy} {
				st.TCPProxy = route
				if err := db.PutSessionForAccount(ctx, acc, st.Session.UIN, st.toBlob(), st.PSK.ExpiredAt, route); err != nil {
					t.Fatal(err)
				}
			}
			before, _ := db.GetSession(ctx, acc.ID, proxy)
			beforeJSON, _ := json.Marshal(before.SessionBlob)
			pool := NewPool(DefaultConfig(), db)
			_, err = pool.GetH5Code(ctx, acc.LoginBuffer, oauth.Request{AppID: "wx1234567890abcdef", RedirectURI: "https://example.com/login", Scope: "snsapi_base", State: "expected"}, acc.ID, proxy, proxyFallback, acc)
			if err == nil {
				t.Fatal("invalid response accepted")
			}
			if len(captured) != 1 {
				t.Errorf("H5 authorization sent %d times, want one", len(captured))
			}
			after, err := db.GetSession(ctx, acc.ID, proxy)
			if err != nil {
				t.Fatalf("H5 failure invalidated the existing session: %v", err)
			}
			afterJSON, _ := json.Marshal(after.SessionBlob)
			if !bytes.Equal(beforeJSON, afterJSON) {
				t.Fatal("H5 failure replaced the existing session")
			}
		})
	}
}
