package protocol

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"time"
)

var (
	type8Prefix = []byte{0x00, 0x00, 0x00, 0x10, 0x08, 0x00, 0x00, 0x00, 0x0b, 0x01, 0x00, 0x00, 0x00, 0x06, 0x00, 0x12}
	earlyAlert  = []byte{0x00, 0x00, 0x00, 0x03, 0x00, 0x01, 0x01}
)

func build0RTTRequest(entry pskEntry, envelope []byte) ([]byte, []byte, []byte, []byte, error) {
	psk, err := hex.DecodeString(entry.PreSharedKey)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	ticket, err := hex.DecodeString(entry.TicketEntry)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	randomBytes := make([]byte, 32)
	if _, err = rand.Read(randomBytes); err != nil {
		return nil, nil, nil, nil, err
	}
	ts := uint32(time.Now().Unix())
	pskCH, err := buildPSKClientHello(randomBytes, ts, ticket)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	hEarly := sha256.Sum256(pskCH)
	ek := derivePSKOneWayKeys(psk, labelEarlyKeys, hEarly[:])
	type8 := append([]byte(nil), type8Prefix...)
	var tmp [4]byte
	binary.BigEndian.PutUint32(tmp[:], ts)
	type8 = append(type8, tmp[:]...)

	body := []byte{}
	r0, _ := buildRecord(ctPSKHandshake, pskCH)
	body = append(body, r0...)
	ct1, err := encryptRecordPayload(ek.Key, ek.IV, 1, ctPSKHandshake, type8)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	r1, _ := buildRecord(ctPSKHandshake, ct1)
	body = append(body, r1...)
	ct2, err := encryptRecordPayload(ek.Key, ek.IV, 2, ctAppData, envelope)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	r2, _ := buildRecord(ctAppData, ct2)
	body = append(body, r2...)
	ct3, err := encryptRecordPayload(ek.Key, ek.IV, 3, ctAlert, earlyAlert)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	r3, _ := buildRecord(ctAlert, ct3)
	body = append(body, r3...)
	return body, psk, pskCH, type8, nil
}

func httpPost(path, host string, body []byte) []byte {
	head := fmt.Sprintf("POST %s HTTP/1.0\r\nAccept: */*\r\nCache-Control: no-cache\r\nConnection: close\r\nContent-Length: %d\r\nContent-Type: application/octet-stream\r\nHost: %s\r\nUpgrade: mmtls\r\nUser-Agent: MicroMessenger Client\r\nX-Online-Host: %s\r\n\r\n", path, len(body), host, host)
	return append([]byte(head), body...)
}

func send0RTT(ctx context.Context, targets []Target, entry pskEntry, recvKey, envelope []byte, timeout time.Duration, tcpProxy string, fallbackDirect bool) ([]byte, []byte, error) {
	var last error
	for _, t := range targets {
		code, resp, err := send0RTTRaw(ctx, t, entry, recvKey, envelope, timeout, tcpProxy, fallbackDirect)
		_ = os.WriteFile("/tmp/yyb-send-raw.txt", []byte(fmt.Sprintf("targets=%+v code_len=%d resp_len=%d err=%v\n", t, len(code), len(resp), err)), 0644)
		if err == nil && (len(code) > 0 || len(resp) > 0) {
			return code, resp, nil
		}
		if err != nil {
			last = err
		} else {
			last = fmt.Errorf("ShortLink response did not contain application data")
		}
	}
	if last == nil {
		last = fmt.Errorf("no ShortLink targets")
	}
	return nil, nil, fmt.Errorf("all ShortLink targets failed; last error: %w", last)
}

func send0RTTRaw(ctx context.Context, target Target, entry pskEntry, recvKey, envelope []byte, timeout time.Duration, tcpProxy string, fallbackDirect bool) ([]byte, []byte, error) {
	body, psk, pskCH, type8, err := build0RTTRequest(entry, envelope)
	if err != nil {
		return nil, nil, err
	}
	path := fmt.Sprintf("/mmtls/%08x", uint32(time.Now().Unix()))
	req := httpPost(path, "shortcloud.weixin.com", body)
	conn, err := dialTCP(ctx, target.IP, target.Port, timeout, tcpProxy, fallbackDirect)
	if err != nil {
		return nil, nil, err
	}
	defer conn.Close()
	if timeout > 0 {
		_ = conn.SetDeadline(time.Now().Add(timeout))
	}
	if _, err = conn.Write(req); err != nil {
		return nil, nil, err
	}
	raw, err := io.ReadAll(conn)
	if err != nil {
		return nil, nil, err
	}
	_, responseBody := splitHTTP(raw)
	if len(responseBody) == 0 {
		return nil, nil, fmt.Errorf("empty ShortLink response")
	}
	return parse0RTTResponse(responseBody, psk, pskCH, type8, recvKey)
}

func parse0RTTResponse(rbody, psk, pskCH, type8, recvKey []byte) ([]byte, []byte, error) {
	recs, _ := parseRecords(rbody)
	var sh []byte
	var encHS [][]byte
	var appdata []byte
	for _, r := range recs {
		if r.ContentType == ctHandshake {
			if sh == nil {
				sh = r.Payload
			}
			encHS = append(encHS, r.Payload)
		}
		if r.ContentType == ctAppData && appdata == nil {
			appdata = r.Payload
		}
	}
	if sh == nil || appdata == nil {
		return nil, nil, fmt.Errorf("response missing ServerHello/AppData")
	}
	candidates := [][]byte{
		bytes.Join([][]byte{pskCH, sh}, nil),
		bytes.Join([][]byte{pskCH, type8, sh}, nil),
		bytes.Join([][]byte{pskCH, sh, type8}, nil),
	}
	for _, trans := range candidates {
		h := sha256.Sum256(trans)
		hk := derivePSKOneWayKeys(psk, labelHandshakeKeys, h[:])
		for _, seq := range []uint64{2, 1, 3} {
			ad, err := decryptRecordPayload(hk.Key, hk.IV, seq, ctAppData, appdata)
			if err != nil {
				continue
			}
			if sl, err := parseShortlink(ad); err == nil {
				if resp, err := sessionDecrypt(sl.Body, recvKey); err == nil {
					return extractCode(resp), resp, nil
				}
			}
			if resp, err := sessionDecrypt(ad, recvKey); err == nil {
				return extractCode(resp), resp, nil
			}
		}
	}
	return nil, nil, fmt.Errorf("AppData decrypt/parse failed")
}
