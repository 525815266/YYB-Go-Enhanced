package pcmgr

import (
	"context"
	"encoding/binary"
	"io"
	"net/http"
	"strings"
	"testing"
)

type roundTrip func(*http.Request) (*http.Response, error)

func (fn roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return fn(r) }

func TestExchangeCodeRequiresBusinessSuccessAndScope(t *testing.T) {
	for _, test := range []struct {
		body string
		ok   bool
	}{
		{`{"ret":0,"data":{"resp":{"retCode":0,"accountId":"42","loginkey":"test-key","renewalTime":"5097600","wxscope":["snsapi_login","snsapi_runtime_pcsdk"],"thirdPartyAccInfo":{"bindAccount":"test-openid","nickName":"demo"}}}}`, true},
		{`{"ret":0,"data":{"resp":{"retCode":0,"accountId":42,"loginkey":"test-key","wxscope":"snsapi_login,snsapi_runtime_pcsdk"}}}`, true},
		{`{"ret":0,"data":{"resp":{"accountId":42,"loginkey":"test-key"}}}`, false},
		{`{"ret":0,"data":{"resp":{"retCode":2,"accountId":42,"loginkey":"test-key"}}}`, false},
		{`{"ret":0,"data":{"resp":{"retCode":0,"accountId":42,"loginkey":"test-key","wxscope":"snsapi_login"}}}`, false},
	} {
		hc := &http.Client{Transport: roundTrip(func(r *http.Request) (*http.Response, error) {
			if r.URL.Host != "jprx.m.qq.com" || r.URL.Path != "/data/3078/forward" {
				t.Fatal("wrong exchange endpoint")
			}
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(test.body)), Header: http.Header{}}, nil
		})}
		c, err := ExchangeCode(context.Background(), hc, "fresh-test-code", strings.Repeat("a", 32))
		if (err == nil) != test.ok {
			t.Fatalf("ok=%v err=%v", test.ok, err)
		}
		if test.ok && (c.Identity() != "pcmgr:42" || c.ClientGUID != strings.Repeat("a", 32)) {
			t.Fatal("identity or device changed")
		}
	}
}

func TestRenewRejectsDifferentIdentityAndMalformedKey(t *testing.T) {
	previous := Credentials{AccountID: 42, LoginKey: "old", ClientGUID: strings.Repeat("a", 32), RenewedAt: 10}
	raw := PBInt(nil, 2, 2)
	raw = PBInt(raw, 3, 42)
	raw = PBBytes(raw, 4, []byte("new"))
	raw = PBInt(raw, 5, 5097600)
	c, err := parseRenew(raw, previous, 20)
	if err != nil || c.LoginKey != "new" || c.RenewedAt != 20 || c.NextRenewAt() != 20+5097600/2 {
		t.Fatal("renewal not retained", err)
	}
	for _, bad := range [][]byte{PBInt(raw, 3, 99), PBBytes(raw, 4, nil), PBInt(nil, 1, 2), PBInt(PBInt(nil, 2, 2), 3, 99)} {
		if _, err := parseRenew(bad, previous, 20); err == nil {
			t.Fatal("invalid renewal accepted")
		}
	}
	if previous.LoginKey != "old" {
		t.Fatal("failed renewal mutated credentials")
	}
}

func TestEnvelopeRejectsTamperAndWrongSequence(t *testing.T) {
	key, iv := make([]byte, 16), make([]byte, 12)
	seq := uint32(123)
	plain := make([]byte, 64)
	binary.BigEndian.PutUint16(plain, 11)
	binary.BigEndian.PutUint16(plain[2:], 20138)
	binary.BigEndian.PutUint32(plain[4:], seq)
	binary.BigEndian.PutUint32(plain[8:], 64)
	gcm, _ := gcmFor(key)
	sealed := gcm.Seal(nil, nonceFor(iv, seq, true), plain, nil)
	packet := packetHeader(20138, seq, 56+len(sealed)-16)
	copy(packet[24:40], sealed[len(sealed)-16:])
	packet = append(packet, sealed[:len(sealed)-16]...)
	if _, err := decryptPacket(packet, key, iv, 20138, seq); err != nil {
		t.Fatal(err)
	}
	if _, err := decryptPacket(packet, key, iv, 20138, seq+1); err == nil {
		t.Fatal("wrong seq accepted")
	}
	packet[60] ^= 1
	if _, err := decryptPacket(packet, key, iv, 20138, seq); err == nil {
		t.Fatal("tampering accepted")
	}
}

func TestBootstrapBounds(t *testing.T) {
	for _, raw := range [][]byte{nil, {0, 0, 0, 65}, {0, 0, 0, 1, 0, 1, 'x', 14, 1, 255, 255, 255, 255}, {0, 0, 0, 1, 0, 1, 'x', 99, 1}} {
		if _, err := parseKV(raw); err == nil {
			t.Fatal("malformed bootstrap accepted")
		}
	}
}
