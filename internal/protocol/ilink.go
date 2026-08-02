package protocol

import (
	"bytes"
	"crypto/ecdh"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
)

const (
	clientVersion = 524545
	appVersion    = 1901

	transferCmd  = 0x0b41
	jsLoginCmdID = 1029
	phoneCmdID   = 1020
	operateCmdID = 1133

	sessClientVer = 1661404927
)

var (
	serverPubHex = "04ef87876d6478b15f1796eab12068610541173b7176b67f1dcc86683e901acd44" +
		"d18b4ac36938251d0812dd0cf842aa2d6cbb8115712d1c0087dcefc14a44cd58"
	serverPub, _ = hex.DecodeString(serverPubHex)

	hostAppIDDefault  = []byte("wxd44977328b36e647")
	ilinkDevice       = []byte("ilinkapp_060000b7b93f6c")
	transferURL       = []byte("/ilink/ilinkapp/mp/wxaruntime_transfer")
	transferHost      = []byte("shortcloud.weixin.com")
	jsLoginURL        = []byte("/cgi-bin/mmbiz-bin/js-login")
	phoneURL          = []byte("/cgi-bin/mmbiz-bin/js-getuserwxphone")
	operateURL        = []byte("/cgi-bin/mmbiz-bin/js-operatewxdata")
	windowsPlugin     = []byte("WindowsxWebPlugin")
	windowsName       = []byte("Windows")
	unifiedPCWindows  = []byte("UnifiedPCWindows")
	sessionKeyLiteral = []byte("sessionkey")
)

type loginMeta struct {
	Ticket    []byte
	DeviceID  []byte
	HostAppID []byte
	Raw       []byte
}

type manualAuthTemp struct {
	Ephemeral *ecdh.PrivateKey
	OKM       []byte
	CompBody  []byte
}

type AppSession struct {
	SendKey   []byte
	RecvKey   []byte
	F9        []byte
	UIN       int64
	Ticket    []byte
	DeviceID  []byte
	HostAppID []byte
}

func parseLoginBuffer(loginBufferB64 string) (loginMeta, error) {
	raw, err := base64.StdEncoding.DecodeString(loginBufferB64)
	if err != nil {
		return loginMeta{}, err
	}
	fields := pbParse(raw)
	ticket, ok1 := fields[1].([]byte)
	device, ok2 := fields[2].([]byte)
	if !ok1 || !ok2 || len(ticket) == 0 || len(device) == 0 {
		return loginMeta{}, fmt.Errorf("login_buffer missing ticket or device_id")
	}
	meta := loginMeta{Ticket: ticket, DeviceID: device, Raw: raw}
	if host, ok := fields[3].([]byte); ok {
		meta.HostAppID = host
	}
	return meta, nil
}

func randomAppDeviceID() ([]byte, error) {
	b := make([]byte, 32)
	_, err := rand.Read(b)
	return b, err
}

func randomSessDevice() []byte {
	mac := make([]byte, 6)
	_, _ = rand.Read(mac)
	mac[0] = (mac[0] | 0x02) & 0xfe
	return []byte(fmt.Sprintf("%02X-%02X-%02X-%02X-%02X-%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]))
}

func buildManualAuthRequest(loginBufferB64 string, appDeviceID []byte) ([]byte, error) {
	meta, err := parseLoginBuffer(loginBufferB64)
	if err != nil {
		return nil, err
	}
	appBase := append(pbLen(1, appDeviceID), pbVar(2, appVersion)...)
	thirdApp := pbLen(1, meta.Ticket)
	out := make([]byte, 0, len(appBase)+len(thirdApp)+32)
	out = append(out, pbLen(1, appBase)...)
	out = append(out, pbLen(3, thirdApp)...)
	out = append(out, pbVar(4, 4)...)
	out = append(out, pbLen(6, nil)...)
	out = append(out, pbVar(7, 0)...)
	out = append(out, pbVar(8, 6)...)
	return out, nil
}

func wpkgHead(ints map[int]uint64, byts map[int][]byte) []byte {
	out := append([]byte{}, varint(1)...)
	keys := make([]int, 0, len(ints))
	for k := range ints {
		keys = append(keys, k)
	}
	sort.Ints(keys)
	for _, k := range keys {
		out = append(out, varint(uint64(k))...)
		out = append(out, varint(ints[k])...)
	}
	out = append(out, varint(0)...)
	bkeys := make([]int, 0, len(byts))
	for k := range byts {
		bkeys = append(bkeys, k)
	}
	sort.Ints(bkeys)
	for _, k := range bkeys {
		v := byts[k]
		out = append(out, varint(uint64(k))...)
		out = append(out, varint(uint64(len(v)))...)
		out = append(out, v...)
	}
	prefixLen := len(out)
	out = append(out, 0)
	out = append(out, varint(uint64(prefixLen+1))...)
	return out
}

func manualAuthHead(deviceID []byte) []byte {
	ints := map[int]uint64{
		1: 1, 2: 0, 3: 0, 4: 0, 5: clientVersion, 6: 11, 7: 0, 8: 0, 9: 0,
		10: 1, 11: 0, 12: 0, 13: 0, 17: 0, 18: 1, 20: 1504, 21: 0, 22: 0, 23: 0,
		25: 17, 26: 4, 28: 1, 29: 1, 30: 0,
	}
	return wpkgHead(ints, map[int][]byte{14: []byte{}, 24: deviceID, 27: []byte{}})
}

func buildLoginBody(loginBufferB64 string, deviceID, appDeviceID []byte, temp *manualAuthTemp) ([]byte, error) {
	plaintext, err := buildManualAuthRequest(loginBufferB64, appDeviceID)
	if err != nil {
		return nil, err
	}
	req, err := hybridECDHEncrypt(plaintext, serverPub, temp)
	if err != nil {
		return nil, err
	}
	return append(manualAuthHead(deviceID), req...), nil
}

func hybridECDHEncrypt(plaintext, serverPub []byte, temp *manualAuthTemp) ([]byte, error) {
	eph, ephPub, err := generateECDH()
	if err != nil {
		return nil, err
	}
	srv, err := ecdh.P256().NewPublicKey(serverPub)
	if err != nil {
		return nil, err
	}
	shared, err := eph.ECDH(srv)
	if err != nil {
		return nil, err
	}
	secret := sha256.Sum256(shared)
	hash1 := sha256.Sum256(bytes.Join([][]byte{[]byte("1"), []byte("415"), ephPub}, nil))
	cek := make([]byte, 32)
	if _, err = rand.Read(cek); err != nil {
		return nil, err
	}
	encCEK, err := aesGCMEncryptLayout(secret[:24], hash1[:], cek)
	if err != nil {
		return nil, err
	}
	prk := hkdfExtract(ilinkHKDFSalt, cek)
	okm := hkdfExpand(prk, hash1[:], 56)
	hash2 := sha256.Sum256(bytes.Join([][]byte{[]byte("1"), []byte("415"), ephPub, encCEK}, nil))
	comp := lz4AllLiteral(plaintext)
	encBody, err := aesGCMEncryptLayout(okm[:24], hash2[:], comp)
	if err != nil {
		return nil, err
	}
	ecdhKey := append(pbVar(1, 415), pbLen(2, ephPub)...)
	if temp != nil {
		temp.Ephemeral = eph
		temp.OKM = okm
		temp.CompBody = comp
	}
	out := make([]byte, 0, len(ecdhKey)+len(encCEK)+len(encBody)+16)
	out = append(out, pbVar(1, 1)...)
	out = append(out, pbLen(2, ecdhKey)...)
	out = append(out, pbLen(3, encCEK)...)
	out = append(out, pbLen(4, nil)...)
	out = append(out, pbLen(5, encBody)...)
	return out, nil
}

func decodeWpkgHead(data []byte) (map[int]uint64, map[int][]byte, int, error) {
	i := 0
	if _, ni, err := readVarint(data, i); err != nil {
		return nil, nil, 0, err
	} else {
		i = ni
	}
	ints := map[int]uint64{}
	for {
		fn, ni, err := readVarint(data, i)
		if err != nil {
			return nil, nil, 0, err
		}
		i = ni
		if fn == 0 {
			break
		}
		v, ni, err := readVarint(data, i)
		if err != nil {
			return nil, nil, 0, err
		}
		i = ni
		ints[int(fn)] = v
	}
	byts := map[int][]byte{}
	for {
		fn, ni, err := readVarint(data, i)
		if err != nil {
			return nil, nil, 0, err
		}
		i = ni
		if fn == 0 {
			break
		}
		ln, ni, err := readVarint(data, i)
		if err != nil {
			return nil, nil, 0, err
		}
		i = ni
		if i+int(ln) > len(data) {
			return nil, nil, 0, fmt.Errorf("wpkg bytes field truncated")
		}
		byts[int(fn)] = append([]byte(nil), data[i:i+int(ln)]...)
		i += int(ln)
	}
	_, ni, err := readVarint(data, i)
	if err != nil {
		return nil, nil, 0, err
	}
	i = ni
	return ints, byts, i, nil
}

func parseLoginResponse(respBody []byte, temp *manualAuthTemp) ([]byte, error) {
	var hybrid []byte
	if _, _, hlen, err := decodeWpkgHead(respBody); err == nil && hlen < len(respBody) && respBody[hlen] == 0x0a {
		hybrid = respBody[hlen:]
	} else {
		marker := []byte{0x08, 0x9f, 0x03, 0x12, 0x41, 0x04}
		idx := bytes.Index(respBody, marker)
		if idx < 2 {
			return nil, fmt.Errorf("HybridEcdhResponse not found")
		}
		hybrid = respBody[idx-2:]
	}
	return hybridECDHDecrypt(hybrid, temp)
}

func hybridECDHDecrypt(response []byte, temp *manualAuthTemp) ([]byte, error) {
	if temp == nil || temp.Ephemeral == nil {
		return nil, fmt.Errorf("missing manualauth temp state")
	}
	F := pbParse(response)
	f1, ok := F[1].([]byte)
	if !ok {
		return nil, fmt.Errorf("HybridEcdhResponse missing field 1")
	}
	keyFields := pbParse(f1)
	serverRespPub, ok := keyFields[2].([]byte)
	if !ok {
		return nil, fmt.Errorf("HybridEcdhResponse missing server pubkey")
	}
	credType := int64(1)
	if v, ok := F[2].(int64); ok {
		credType = v
	}
	ct, ok := F[3].([]byte)
	if !ok {
		return nil, fmt.Errorf("HybridEcdhResponse missing ciphertext")
	}
	peer, err := ecdh.P256().NewPublicKey(serverRespPub)
	if err != nil {
		return nil, err
	}
	shared, err := temp.Ephemeral.ECDH(peer)
	if err != nil {
		return nil, err
	}
	secret := sha256.Sum256(shared)
	aadInput := bytes.Join([][]byte{temp.OKM[24:56], temp.CompBody, []byte("415"), serverRespPub, []byte(strconv.FormatInt(credType, 10))}, nil)
	aad := sha256.Sum256(aadInput)
	comp, err := aesGCMDecryptLayout(secret[:24], aad[:], ct)
	if err != nil {
		return nil, err
	}
	return lz4Decompress(comp)
}

func extractSession(mar []byte) (AppSession, error) {
	F := pbParse(mar)
	f3, ok := F[3].([]byte)
	if !ok {
		return AppSession{}, fmt.Errorf("ManualAuthResponse missing f3")
	}
	body := pbParse(f3)
	body2, ok := body[2].([]byte)
	if !ok {
		return AppSession{}, fmt.Errorf("ManualAuthResponse missing session block")
	}
	body3, ok := body[3].([]byte)
	if !ok {
		return AppSession{}, fmt.Errorf("ManualAuthResponse missing identity block")
	}
	sess := pbParse(body2)
	ident := pbParse(body3)
	uin, _ := ident[1].(int64)
	out := AppSession{
		SendKey:  safeBytes(sess[1]),
		RecvKey:  safeBytes(sess[2]),
		F9:       safeBytes(sess[9]),
		UIN:      uin,
		Ticket:   safeBytes(sess[12]),
		DeviceID: append([]byte(nil), ilinkDevice...),
	}
	if len(out.SendKey) == 0 || len(out.RecvKey) == 0 || out.UIN == 0 {
		return AppSession{}, fmt.Errorf("ManualAuthResponse session fields incomplete")
	}
	return out, nil
}

func sessionInfo(uin32 uint32, win, sessDevice []byte) []byte {
	out := make([]byte, 0, 80)
	out = append(out, pbLen(1, sessionKeyLiteral)...)
	out = append(out, pbVar(2, uint64(uin32))...)
	out = append(out, pbLen(3, sessDevice)...)
	out = append(out, pbVar(4, sessClientVer)...)
	out = append(out, pbLen(5, win)...)
	out = append(out, pbVar(6, 0)...)
	return out
}

func buildJSAPIPlaintext(uin int64, appID string, transURL []byte, transCmdID uint64, loginReq []byte, hostAppID []byte, sessDevice []byte) []byte {
	if len(transURL) == 0 {
		transURL = jsLoginURL
	}
	if transCmdID == 0 {
		transCmdID = jsLoginCmdID
	}
	if len(hostAppID) == 0 {
		hostAppID = hostAppIDDefault
	}
	if len(sessDevice) == 0 {
		sessDevice = randomSessDevice()
	}
	aid := []byte(appID)
	uin32 := uint32(uin)
	if loginReq == nil {
		loginReq = make([]byte, 0, 120)
		loginReq = append(loginReq, pbLen(1, sessionInfo(uin32, unifiedPCWindows, sessDevice))...)
		loginReq = append(loginReq, pbLen(2, aid)...)
		loginReq = append(loginReq, pbVar(4, 1)...)
		loginReq = append(loginReq, pbLen(5, nil)...)
		loginReq = append(loginReq, pbLen(6, nil)...)
		loginReq = append(loginReq, pbVar(7, 1)...)
	}
	out := make([]byte, 0, 256+len(loginReq))
	out = append(out, pbLen(1, sessionInfo(uin32, windowsName, sessDevice))...)
	out = append(out, pbLen(2, transURL)...)
	out = append(out, pbLen(3, hostAppID)...)
	out = append(out, pbVar(4, 5)...)
	out = append(out, pbLen(5, loginReq)...)
	out = append(out, pbLen(6, aid)...)
	out = append(out, pbVar(7, transCmdID)...)
	out = append(out, pbVar(8, 1610627409)...)
	out = append(out, pbLen(9, windowsPlugin)...)
	out = append(out, pbVar(10, 573651281)...)
	return out
}

func sessionWpkgHead(uin int64, f9, deviceID []byte) []byte {
	if len(deviceID) == 0 {
		deviceID = ilinkDevice
	}
	ints := map[int]uint64{
		1: 1, 2: uint64(uin), 3: 0, 4: 0, 5: clientVersion, 6: 11, 7: 0, 8: 0, 9: 0,
		10: 1, 11: 0, 12: 0, 13: 0, 17: 0, 18: 1, 20: 1504, 21: 0, 22: uint64(uin),
		23: 0, 25: 16, 26: 4, 28: 1, 29: 1, 30: 0,
	}
	return wpkgHead(ints, map[int][]byte{14: []byte{}, 24: deviceID, 27: f9})
}

func buildTransferPacket(session AppSession, plaintext []byte) ([]byte, error) {
	enc, err := aesGCMEncryptLayout(session.SendKey, nil, lz4AllLiteral(plaintext))
	if err != nil {
		return nil, err
	}
	wpkg := sessionWpkgHead(session.UIN, session.F9, session.DeviceID)
	innerBody := append(wpkg, enc...)
	inner := buildShortlink(transferCmd, 0, innerBody, defaultVer)
	env := make([]byte, 0, 2+len(transferURL)+2+len(transferHost)+4+len(inner))
	var tmp [4]byte
	binary.BigEndian.PutUint16(tmp[:2], uint16(len(transferURL)))
	env = append(env, tmp[:2]...)
	env = append(env, transferURL...)
	binary.BigEndian.PutUint16(tmp[:2], uint16(len(transferHost)))
	env = append(env, tmp[:2]...)
	env = append(env, transferHost...)
	binary.BigEndian.PutUint32(tmp[:4], uint32(len(inner)))
	env = append(env, tmp[:4]...)
	env = append(env, inner...)
	out := make([]byte, 4, 4+len(env))
	binary.BigEndian.PutUint32(out[:4], uint32(len(env)))
	out = append(out, env...)
	return out, nil
}

func sessionDecrypt(body, recvKey []byte) ([]byte, error) {
	offsets := []int{}
	if _, _, hlen, err := decodeWpkgHead(body); err == nil {
		offsets = append(offsets, hlen)
	}
	limit := min(len(body), 220)
	for i := 0; i < limit; i++ {
		offsets = append(offsets, i)
	}
	seen := map[int]bool{}
	for _, off := range offsets {
		if seen[off] || off < 0 || off >= len(body) {
			continue
		}
		seen[off] = true
		blob := body[off:]
		if len(blob) < 29 {
			continue
		}
		comp, err := aesGCMDecryptLayout(recvKey, nil, blob)
		if err != nil {
			continue
		}
		pt, err := lz4Decompress(comp)
		if err == nil {
			return pt, nil
		}
	}
	return nil, fmt.Errorf("response decrypt failed")
}

func extractCode(resp []byte) []byte {
	F := pbParse(resp)
	if f2, ok := F[2].([]byte); ok {
		inner := pbParse(f2)
		if f3, ok := inner[3].([]byte); ok {
			return f3
		}
	}
	_ = os.WriteFile("/tmp/yyb-debug.txt", []byte(fmt.Sprintf("len=%d respHex=%s F=%v\n", len(resp), hex.EncodeToString(resp), F)), 0644)
	return nil
}

func parseRawResponse(codeOrJSON, resp []byte) map[string]any {
	raw := bytes.TrimSpace(codeOrJSON)
	if len(raw) > 0 && raw[0] == '{' {
		var out map[string]any
		if json.Unmarshal(raw, &out) == nil {
			return out
		}
	}
	return protobufToMap(resp)
}

func buildPhoneRequest(uin int64, appID string) []byte {
	sessDevice := randomSessDevice()
	aid := []byte(appID)
	body := make([]byte, 0, 140)
	body = append(body, pbLen(1, sessionInfo(uint32(uin), unifiedPCWindows, sessDevice))...)
	body = append(body, pbLen(2, aid)...)
	body = append(body, pbVar(4, 1)...)
	body = append(body, pbLen(5, nil)...)
	body = append(body, pbLen(6, nil)...)
	body = append(body, pbVar(7, 1)...)
	return buildJSAPIPlaintext(uin, appID, phoneURL, phoneCmdID, body, nil, sessDevice)
}

func buildOperateRequest(uin int64, appID string, payload map[string]any) ([]byte, error) {
	payload = extractOperatePayload(payload)
	sessDevice := randomSessDevice()
	aid := []byte(appID)
	f3, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	body := make([]byte, 0, 180+len(f3))
	body = append(body, pbLen(1, sessionInfo(uint32(uin), unifiedPCWindows, sessDevice))...)
	body = append(body, pbLen(2, aid)...)
	body = append(body, pbLen(3, f3)...)
	body = append(body, pbLen(4, nil)...)
	body = append(body, pbVar(5, 0)...)
	body = append(body, pbVar(6, 0)...)
	return buildJSAPIPlaintext(uin, appID, operateURL, operateCmdID, body, nil, sessDevice), nil
}

func extractOperatePayload(raw map[string]any) map[string]any {
	if raw == nil {
		return nil
	}
	if d, ok := raw["data"].(map[string]any); ok {
		if _, ok = d["api_name"]; ok {
			return d
		}
	}
	if _, ok := raw["api_name"]; !ok {
		if d, ok := raw["data"].(map[string]any); ok {
			return d
		}
	}
	return raw
}
