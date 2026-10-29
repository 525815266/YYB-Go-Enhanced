package pcmgr

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net/http"

	"google.golang.org/protobuf/encoding/protowire"
)

// Reconstructed from PC Manager 18.2.30865.218. The HTTP body uses its
// RSA bootstrap and AES-GCM envelope; no Windows DLL or resident PC is needed.
const nativeEndpoint = "http://conna.gj.qq.com/"
const nativePublicKey = "MIGJAoGBAL6yyUhfCwUl37nvZ1Y3wqLxdIeuM1HegcWfoyWW9TJ3PF/Y8VSgdyuJjsN14F608TG5SlGagfT+i3j7JMqKI/Bq6F+nMISkgvMtrQ5NDsbGwerhop1R3MRxIjkNW4ZBtkElF3QbR41K1vpXRtl1goTbL1YTEm75EHNlFoDBIverAgMBAAE="

type RPCError struct {
	Command uint16
	Code    int32
}

func (e *RPCError) Error() string {
	return fmt.Sprintf("PCMgr command %d rejected: code=%d", e.Command, e.Code)
}

func PBInt(dst []byte, n protowire.Number, v uint64) []byte {
	return protowire.AppendVarint(protowire.AppendTag(dst, n, protowire.VarintType), v)
}
func PBBytes(dst []byte, n protowire.Number, v []byte) []byte {
	return protowire.AppendBytes(protowire.AppendTag(dst, n, protowire.BytesType), v)
}

type pbValue struct {
	wire protowire.Type
	n    uint64
	b    []byte
}

func parsePB(raw []byte) (map[protowire.Number]pbValue, error) {
	values := map[protowire.Number]pbValue{}
	for len(raw) > 0 {
		n, t, k := protowire.ConsumeTag(raw)
		if k < 0 || n <= 0 {
			return nil, errors.New("invalid PCMgr protobuf tag")
		}
		raw = raw[k:]
		if _, ok := values[n]; ok {
			return nil, errors.New("duplicate PCMgr protobuf field")
		}
		v := pbValue{wire: t}
		switch t {
		case protowire.VarintType:
			v.n, k = protowire.ConsumeVarint(raw)
		case protowire.BytesType:
			v.b, k = protowire.ConsumeBytes(raw)
		default:
			return nil, errors.New("invalid PCMgr protobuf wire type")
		}
		if k < 0 {
			return nil, errors.New("truncated PCMgr protobuf")
		}
		raw = raw[k:]
		values[n] = v
	}
	return values, nil
}

func packetHeader(cmd uint16, seq uint32, size int) []byte {
	b := make([]byte, 56)
	binary.BigEndian.PutUint16(b, 11)
	binary.BigEndian.PutUint16(b[2:], cmd)
	binary.BigEndian.PutUint16(b[4:], 56)
	binary.BigEndian.PutUint16(b[6:], 0x5200)
	binary.BigEndian.PutUint32(b[8:], uint32(size))
	binary.BigEndian.PutUint32(b[12:], seq)
	binary.BigEndian.PutUint16(b[40:], 1)
	return b
}
func readPacket(raw []byte, cmd uint16, seq uint32) ([]byte, error) {
	if len(raw) < 56 || len(raw) > 2_000_000 || binary.BigEndian.Uint16(raw) != 11 ||
		binary.BigEndian.Uint16(raw[2:]) != cmd || binary.BigEndian.Uint16(raw[4:]) != 56 ||
		binary.BigEndian.Uint32(raw[8:]) != uint32(len(raw)) || binary.BigEndian.Uint32(raw[12:]) != seq {
		return nil, errors.New("PCMgr envelope mismatch")
	}
	return raw[56:], nil
}
func nonceFor(iv []byte, seq uint32, response bool) []byte {
	n := append([]byte(nil), iv...)
	offset := 0
	if response {
		offset = 4
	}
	binary.LittleEndian.PutUint32(n[offset:], binary.LittleEndian.Uint32(n[offset:])+seq)
	return n
}
func gcmFor(key []byte) (cipher.AEAD, error) {
	b, err := aes.NewCipher(key)
	if err != nil {
		return nil, errors.New("invalid PCMgr session key")
	}
	return cipher.NewGCM(b)
}
func decryptPacket(raw, key, iv []byte, cmd uint16, seq uint32) ([]byte, error) {
	body, err := readPacket(raw, cmd, seq)
	if err != nil {
		return nil, err
	}
	gcm, err := gcmFor(key)
	if err != nil {
		return nil, err
	}
	plain, err := gcm.Open(nil, nonceFor(iv, seq, true), append(append([]byte(nil), body...), raw[24:40]...), nil)
	if err != nil || len(plain) < 64 || binary.BigEndian.Uint16(plain) != 11 || binary.BigEndian.Uint16(plain[2:]) != cmd || binary.BigEndian.Uint32(plain[4:]) != seq || (binary.BigEndian.Uint32(plain[8:]) != uint32(len(plain)) && !(cmd == 395 && binary.BigEndian.Uint32(plain[8:]) == 64)) {
		return nil, errors.New("PCMgr authenticated response invalid")
	}
	return plain[64:], nil
}

func appendKVBlob(b []byte, tag byte, v []byte) []byte {
	b = append(b, tag, 14, 1)
	b = binary.BigEndian.AppendUint32(b, uint32(len(v)))
	return append(b, v...)
}

type kvValue struct {
	kind byte
	b    []byte
}

func parseKV(raw []byte) (map[string]kvValue, error) {
	if len(raw) < 4 {
		return nil, errors.New("truncated PCMgr bootstrap")
	}
	count := binary.BigEndian.Uint32(raw)
	raw = raw[4:]
	if count > 64 {
		return nil, errors.New("oversized PCMgr bootstrap")
	}
	widths := []int{0, 1, 1, 2, 2, 4, 4, 8, 8, 4, 8, 16, 20, -1, -1, -1, -1, 4, -1, 0}
	values := map[string]kvValue{}
	for i := uint32(0); i < count; i++ {
		if len(raw) < 2 {
			return nil, errors.New("truncated PCMgr bootstrap key")
		}
		size := int(binary.BigEndian.Uint16(raw))
		raw = raw[2:]
		if size < 1 || size > 128 || len(raw) < size+2 {
			return nil, errors.New("invalid PCMgr bootstrap key")
		}
		name := string(bytes.TrimSuffix(raw[:size], []byte{0}))
		raw = raw[size:]
		kind := raw[0]
		raw = raw[2:]
		if int(kind) >= len(widths) {
			return nil, errors.New("invalid PCMgr bootstrap type")
		}
		length := widths[kind]
		if length <= 0 {
			if len(raw) < 4 {
				return nil, errors.New("truncated PCMgr bootstrap length")
			}
			encodedLength := binary.BigEndian.Uint32(raw)
			raw = raw[4:]
			if uint64(encodedLength) > uint64(len(raw)) {
				return nil, errors.New("truncated PCMgr bootstrap value")
			}
			length = int(encodedLength)
		}
		if length > len(raw) {
			return nil, errors.New("truncated PCMgr bootstrap value")
		}
		if _, ok := values[name]; ok {
			return nil, errors.New("duplicate PCMgr bootstrap key")
		}
		values[name] = kvValue{kind, raw[:length]}
		raw = raw[length:]
	}
	if len(raw) != 0 {
		return nil, errors.New("trailing PCMgr bootstrap data")
	}
	return values, nil
}

func exchange(ctx context.Context, client *http.Client, packet []byte) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, nativeEndpoint, bytes.NewReader(packet))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	hc := *client
	hc.CheckRedirect = func(*http.Request, []*http.Request) error { return errors.New("PCMgr redirect refused") }
	resp, err := hc.Do(req)
	if err != nil {
		return nil, fmt.Errorf("PCMgr transport: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("PCMgr transport HTTP %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 2_000_001))
	if err != nil {
		return nil, err
	}
	if len(raw) > 2_000_000 {
		return nil, errors.New("PCMgr response too large")
	}
	return raw, nil
}

// Call executes one allowed business request. Key rotation is never retried
// inside the transport: a lost response must not trigger a second mutation.
func Call(ctx context.Context, client *http.Client, cmd uint16, guid, request []byte) ([]byte, error) {
	if (cmd != 20042 && cmd != 20138) || len(guid) != 16 || len(request) > 1_000_000 {
		return nil, errors.New("invalid PCMgr call")
	}
	if client == nil {
		client = http.DefaultClient
	}
	seed := make([]byte, 48)
	if _, err := rand.Read(seed); err != nil {
		return nil, err
	}
	key, iv := seed[:16], seed[16:28]
	seq := binary.LittleEndian.Uint32(seed[28:32]) & 0x7ffffffe
	auth := binary.BigEndian.AppendUint32(nil, 5)
	auth = appendKVBlob(auth, 1, key)
	auth = appendKVBlob(auth, 2, iv)
	auth = append(auth, 3, 6, 1)
	auth = binary.LittleEndian.AppendUint32(auth, seq)
	auth = append(auth, 4, 8, 1)
	auth = append(auth, make([]byte, 8)...)
	auth = appendKVBlob(auth, 5, guid)
	der, _ := base64.StdEncoding.DecodeString(nativePublicKey)
	pub, err := x509.ParsePKCS1PublicKey(der)
	if err != nil {
		return nil, err
	}
	encrypted, err := rsa.EncryptPKCS1v15(rand.Reader, pub, auth)
	if err != nil {
		return nil, err
	}
	raw, err := exchange(ctx, client, append(packetHeader(395, seq, 56+len(encrypted)), encrypted...))
	if err != nil {
		return nil, err
	}
	plain, err := decryptPacket(raw, key, iv, 395, seq)
	if err != nil {
		return nil, err
	}
	kv, err := parseKV(plain)
	if err != nil {
		return nil, err
	}
	if v := kv["ret"]; len(v.b) != 4 || binary.LittleEndian.Uint32(v.b) != 0 {
		return nil, errors.New("PCMgr bootstrap rejected")
	}
	key, iv = kv["session_key"].b, kv["session_aes_iv"].b
	sid := kv["session_id"].b
	if len(key) != 16 || len(iv) != 12 || len(sid) != 8 {
		return nil, errors.New("PCMgr bootstrap missing session")
	}
	seq++
	head := PBBytes(nil, 1, guid)
	head = PBBytes(head, 2, nil)
	head = PBBytes(head, 3, []byte("thisisjustatestaccount"))
	body := PBInt(nil, 1, uint64(seq))
	body = PBInt(body, 2, uint64(cmd))
	body = PBBytes(body, 3, head)
	body = PBBytes(body, 4, request)
	inner := make([]byte, 64)
	binary.BigEndian.PutUint16(inner, 11)
	binary.BigEndian.PutUint16(inner[2:], cmd)
	binary.BigEndian.PutUint32(inner[4:], seq)
	binary.BigEndian.PutUint32(inner[8:], uint32(64+len(body)))
	binary.BigEndian.PutUint32(inner[12:], 2010)
	binary.BigEndian.PutUint32(inner[36:], seq)
	binary.BigEndian.PutUint32(inner[40:], 30865)
	binary.BigEndian.PutUint16(inner[60:], 1)
	gcm, _ := gcmFor(key)
	sealed := gcm.Seal(nil, nonceFor(iv, seq, false), append(inner, body...), nil)
	outer := packetHeader(cmd, seq, 56+len(sealed)-16)
	copy(outer[16:24], sid)
	copy(outer[24:40], sealed[len(sealed)-16:])
	raw, err = exchange(ctx, client, append(outer, sealed[:len(sealed)-16]...))
	if err != nil {
		return nil, err
	}
	plain, err = decryptPacket(raw, key, iv, cmd, seq)
	if err != nil {
		return nil, err
	}
	envelope, err := parsePB(plain)
	if err != nil {
		return nil, err
	}
	if envelope[2].n != uint64(cmd) || envelope[3].wire != protowire.BytesType {
		return nil, errors.New("PCMgr response command mismatch")
	}
	response, err := parsePB(envelope[3].b)
	if err != nil {
		return nil, err
	}
	if response[1].n != uint64(seq) {
		return nil, errors.New("PCMgr response sequence mismatch")
	}
	for _, n := range []protowire.Number{2, 3} {
		if response[n].n != 0 {
			return nil, &RPCError{cmd, int32(response[n].n)}
		}
	}
	if response[4].wire != protowire.BytesType || len(response[4].b) == 0 {
		return nil, errors.New("PCMgr response missing business data")
	}
	return response[4].b, nil
}
