//go:build protocolaudit

package protocol

// Explicitly gated live audit. Never opened by normal tests or production.
// Credentials are read-only; experimental session state stays in a private file.
import (
	"bytes"
	"context"
	"crypto/ecdh"
	"crypto/sha256"
	"database/sql"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"testing"
	"time"

	"yyb_go/internal/pcmgr"
)

type nativeAuditState struct {
	AppDevice        []byte
	Device           []byte
	AutoKey          []byte
	EncKey           []byte
	Response         []byte
	Session          AppSession
	PSK              pskEntry
	WireResponse     []byte
	WireCommand      uint32
	EphemeralPrivate []byte
	OKM              []byte
	Compressed       []byte
	RequestCipher    []byte
}

func auditOutput(stage string, fields map[string]any) {
	fields["stage"] = stage
	raw, _ := json.Marshal(fields)
	fmt.Printf("NATIVE_AUTOAUTH %s\n", raw)
}

func auditBufferFailure(err error) {
	fields := map[string]any{"error_type": fmt.Sprintf("%T", err)}
	var rejected *LoginBufferRejectedError
	if errors.As(err, &rejected) {
		fields["upstream_code"] = rejected.Code
		pattern := regexp.MustCompile(`(?i)(?:^|(?:\berror\b|\bcode\b)\s*[:=]?\s*|\[)(40030|42002|42007|40188)(?:$|[\s\]|,;])`)
		if match := pattern.FindStringSubmatch(rejected.Message); len(match) > 1 {
			fields["wechat_code"], _ = strconv.Atoi(match[1])
		}
	}
	auditOutput("fresh_buffer_failed", fields)
}

// Compare application requests against one saved protocol session without
// rebuilding/invalidating it or updating the production account's credentials.
func TestNativeCachedSessionCodeAudit(t *testing.T) {
	path := os.Getenv("YYB_CODE_SESSION_AUDIT_PATH")
	if path == "" {
		t.Skip("requires an explicit private saved session")
	}
	if !filepath.IsAbs(path) {
		t.Fatal("private session path must be absolute")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private session unavailable")
	}
	var blob map[string]any
	if json.Unmarshal(raw, &blob) != nil {
		t.Fatal("private session malformed")
	}
	st, err := sessionFromBlob(blob)
	if err != nil || st.PSK.ExpiredAt <= time.Now().Unix() || st.TCPProxy != "" {
		t.Fatal("requires a valid saved direct session")
	}
	pool := NewPool(DefaultConfig(), nil)
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	apps := []string{"wx3bb18aff4f2fe45f", "wx91d27dbf599dff74", "wx3bb18aff4f2fe45f", "wx2f5d8f9715c59d10", "wx3bb18aff4f2fe45f"}
	if appID := os.Getenv("YYB_CODE_SESSION_AUDIT_APPID"); appID != "" {
		if len(appID) != 18 || appID[:2] != "wx" {
			t.Fatal("audit application ID malformed")
		}
		if _, err := hex.DecodeString(appID[2:]); err != nil {
			t.Fatal("audit application ID malformed")
		}
		apps = []string{"wx3bb18aff4f2fe45f", appID, "wx3bb18aff4f2fe45f"}
	}
	for _, appID := range apps {
		plain := buildJSAPIPlaintext(st.Session.UIN, appID, jsLoginURL, jsLoginCmdID, nil, st.Session.HostAppID, nil)
		envelope, err := buildTransferPacket(st.Session, plain)
		if err != nil {
			t.Fatal("audit code packet build failed")
		}
		code, response, err := pool.sendEnvelope(ctx, st, envelope)
		auditOutput("saved_session_code", map[string]any{"app_id": appID,
			"success": err == nil && len(code) > 0, "code_length": len(code),
			"response_length": len(response), "error_type": fmt.Sprintf("%T", err),
			"shortlink_payload_error":        errors.Is(err, ErrShortlinkPayload),
			"production_session_invalidated": false, "credentials_written": false})
		if ctx.Err() != nil {
			t.Fatal("audit budget expired")
		}
	}
}

func TestNativeAutoAuthAudit(t *testing.T) {
	path := os.Getenv("YYB_AUTOAUTH_AUDIT_DB")
	pcTicket := os.Getenv("YYB_AUTOAUTH_AUDIT_PCMGR_TICKET")
	statePath := os.Getenv("YYB_AUTOAUTH_AUDIT_STATE")
	if statePath == "" || (pcTicket == "" && (path == "" || os.Getenv("YYB_AUTOAUTH_AUDIT_ACCOUNT") != "1")) {
		t.Skip("requires explicit private read-only database, state path and account 1")
	}
	if !filepath.IsAbs(statePath) || (pcTicket == "" && !filepath.IsAbs(path)) || (pcTicket != "" && (!filepath.IsAbs(pcTicket) || path != "")) {
		t.Fatal("audit paths must be absolute")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	phase := os.Getenv("YYB_AUTOAUTH_AUDIT_PHASE")
	if phase == "auto" {
		auditAutoAuth(t, ctx, statePath)
		return
	} else if phase != "manual" && phase != "buffer" {
		t.Fatal("explicit audit phase required")
	}
	var buffer string
	var creds LoginBufferCredentials
	var err error
	if pcTicket != "" {
		raw, readErr := os.ReadFile(pcTicket)
		if readErr != nil {
			t.Fatal("private PCMgr ticket unavailable")
		}
		buffer, err = pcmgr.ParseBufferResponse(raw)
		meta, parseErr := parseLoginBuffer(buffer)
		if err != nil || parseErr != nil || string(meta.HostAppID) != pcmgr.HostAppID {
			t.Fatal("requires a fresh PCMgr ticket with the matching provider")
		}
	} else {
		db, openErr := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro")
		if openErr != nil {
			t.Fatal("read-only database open failed")
		}
		defer db.Close()
		var raw, status, proxyMode string
		err = db.QueryRowContext(ctx, "SELECT a.login_buffer,COALESCE(a.credentials,''),a.status,COALESCE(p.mode,'direct') FROM wechat_accounts a LEFT JOIN account_proxy_settings p ON p.account_id=a.id WHERE a.id=1").Scan(&buffer, &raw, &status, &proxyMode)
		if err != nil || status != "alive" || proxyMode != "direct" || buffer == "" {
			t.Fatal("audit requires an existing alive direct account with a login buffer")
		}
		var mapped map[string]any
		if json.Unmarshal([]byte(raw), &mapped) != nil {
			t.Fatal("credential parsing failed")
		}
		creds = CredentialsFromMap(mapped)
	}
	client := NewLoginBufferClient(12 * time.Second)
	if phase == "buffer" {
		if pcTicket != "" {
			t.Fatal("PCMgr audit uses the explicitly supplied fresh ticket")
		}
		ticket, err := client.FetchLoginBuffer(ctx, creds)
		if err != nil {
			auditBufferFailure(err)
			t.Fatal("read-only buffer probe rejected; no handshake or OAuth refresh attempted")
		}
		auditOutput("fresh_buffer", map[string]any{"ticket_length": len(ticket),
			"credentials_written": false, "oauth_refresh_requested": false})
		return
	}
	targets, err := getLonglinkTargets(ctx, 4*time.Second, time.Minute)
	if err != nil {
		t.Fatal("longlink target discovery failed")
	}
	pool := NewPool(DefaultConfig(), nil)
	var captured nativeAuditState
	login := func(ticket string) error {
		_, e := tryLonglinkCandidates(ctx, targets, 35*time.Second, func(callCtx context.Context, target Target) (WmpfSession, error) {
			mc, e := connectMmtls(callCtx, target, 12*time.Second, "", false)
			if e != nil {
				return WmpfSession{}, e
			}
			defer mc.close()
			meta, e := parseLoginBuffer(ticket)
			if e != nil {
				return WmpfSession{}, errors.New("audit login buffer malformed")
			}
			device, e := randomAppDeviceID()
			if e != nil {
				return WmpfSession{}, e
			}
			if os.Getenv("YYB_AUTOAUTH_AUDIT_DEVICE_STYLE") == "native16" {
				// Network-layer default; TDI has a separate device ID.
				device = []byte(hex.EncodeToString(device[:8]))
			} else if os.Getenv("YYB_AUTOAUTH_AUDIT_DEVICE_STYLE") == "ticket" {
				// Isolate application/header identity consistency. Do not copy
				// the official client's identity or modify production state.
				device = append([]byte(nil), meta.DeviceID...)
			}
			temp := &manualAuthTemp{}
			body, e := buildLoginBody(ticket, meta.DeviceID, device, temp)
			if e != nil {
				return WmpfSession{}, e
			}
			if e = mc.sendApp(cmdManualAuth, body); e != nil {
				return WmpfSession{}, e
			}
			response, e := mc.recvApp()
			if e != nil {
				return WmpfSession{}, e
			}
			if response.Cmd != cmdManualAuth {
				return WmpfSession{}, errors.New("unexpected manual auth response command")
			}
			decoded, e := parseLoginResponse(response.Body, temp)
			if e != nil {
				return WmpfSession{}, e
			}
			session, e := extractSession(decoded)
			if e != nil {
				return WmpfSession{}, e
			}
			sect := pbParse(safeBytes(pbParse(safeBytes(pbParse(decoded)[3]))[2]))
			session.DeviceID, session.HostAppID = meta.DeviceID, meta.HostAppID
			keys, e := mc.extractPSKs()
			if e != nil {
				return WmpfSession{}, e
			}
			psk, ok := pickAccessPSK(keys)
			if !ok {
				return WmpfSession{}, errors.New("audit handshake returned no access PSK")
			}
			captured = nativeAuditState{AppDevice: device, Device: meta.DeviceID,
				AutoKey: safeBytes(sect[3]), EncKey: safeBytes(sect[4]),
				Response: decoded, Session: session, PSK: psk}
			if os.Getenv("YYB_AUTOAUTH_AUDIT_SAME_CONNECTION") == "1" {
				baseline := WmpfSession{Session: session, PSK: psk,
					ShortlinkTargets: getShortlinkTargets(ctx, 4*time.Second, time.Minute)}
				result, probeErr := pool.requestLoginCode(ctx, baseline, "wx3bb18aff4f2fe45f")
				loginCode, _ := result["code"].(string)
				auditOutput("manual_code_before_auto", map[string]any{"success": probeErr == nil && loginCode != "", "code_length": len(loginCode)})
				if probeErr != nil || loginCode == "" {
					t.Fatal("manual code baseline failed; AutoKey not sent")
				}
				// One fresh key, one request, on the original ManualAuth
				// transport. Keep the rotated result separate from the
				// pre-AutoAuth state and never write either to production.
				auditAutoAuthOnConnection(t, ctx, statePath+".auto", captured, mc)
			}
			return WmpfSession{Session: session, PSK: psk, CreatedAt: time.Now()}, nil
		})
		return e
	}
	err = login(buffer)
	var rejected *ManualAuthRejectedError
	if errors.As(err, &rejected) && rejected.Code == -10 {
		auditOutput("saved_buffer_rejected", map[string]any{"code": rejected.Code})
		if pcTicket != "" {
			t.Fatal("PCMgr ticket rejected; acquire a new ticket explicitly")
		}
		buffer, err = client.FetchLoginBuffer(ctx, creds)
		if err != nil {
			auditBufferFailure(err)
			t.Fatal("fresh buffer failed; no OAuth refresh attempted")
		}
		err = login(buffer)
	}
	if err != nil {
		auditOutput("manual_failed", map[string]any{"error_type": fmt.Sprintf("%T", err)})
		t.Fatal("manual audit failed; production database unchanged")
	}
	stateRaw, _ := json.Marshal(captured)
	file, err := os.OpenFile(statePath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		t.Fatal("private state creation failed")
	}
	_, writeErr := file.Write(stateRaw)
	closeErr := file.Close()
	if writeErr != nil || closeErr != nil {
		t.Fatal("private state save failed")
	}
	auditOutput("manual_keys", map[string]any{"auto_key_length": len(captured.AutoKey),
		"enc_key_length": len(captured.EncKey), "credentials_written": false})
	if os.Getenv("YYB_AUTOAUTH_AUDIT_SAME_CONNECTION") == "1" {
		// Successful AutoAuth already verified its replacement. Its key
		// rotation retires the original session; do not test that stale
		// session again or call its expected rejection a failed rotation.
		return
	}
	st := WmpfSession{Session: captured.Session, PSK: captured.PSK,
		ShortlinkTargets: getShortlinkTargets(ctx, 4*time.Second, time.Minute)}
	plain := buildJSAPIPlaintext(st.Session.UIN, "wx3bb18aff4f2fe45f", jsLoginURL, jsLoginCmdID, nil, st.Session.HostAppID, nil)
	envelope, err := buildTransferPacket(st.Session, plain)
	if err != nil {
		t.Fatal("audit code packet build failed")
	}
	code, _, err := pool.sendEnvelope(ctx, st, envelope)
	auditOutput("manual_code", map[string]any{"success": err == nil && len(code) > 0, "code_length": len(code)})
	if err != nil || len(code) == 0 {
		t.Fatal("audit code failed; credentials unchanged")
	}
}

// This candidate is deliberately confined to the opt-in audit build. Native
// PackRequest selects Hybrid=17 (0x174832); identity/header integration is
// tested here, not assumed to be a production-ready AutoAuth implementation.
func auditAutoAuth(t *testing.T, ctx context.Context, statePath string) {
	source := os.Getenv("YYB_AUTOAUTH_AUDIT_SOURCE")
	if !filepath.IsAbs(source) || source == statePath {
		t.Fatal("auto phase requires a separate absolute private source state")
	}
	raw, err := os.ReadFile(source)
	if err != nil {
		t.Fatal("private source state unavailable")
	}
	var saved nativeAuditState
	if json.Unmarshal(raw, &saved) != nil || len(saved.AutoKey) == 0 || len(saved.EncKey) != 24 || len(saved.AppDevice) == 0 {
		t.Fatal("private source state incomplete")
	}
	used, err := os.OpenFile(source+".used", os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal("source key already used or cannot be marked; refusing replay")
	}
	_ = used.Close()
	targets, err := getLonglinkTargets(ctx, 4*time.Second, time.Minute)
	if err != nil || len(targets) == 0 {
		t.Fatal("auto target discovery failed")
	}
	mc, err := connectMmtls(ctx, targets[0], 12*time.Second, "", false)
	if err != nil {
		t.Fatal("auto transport handshake failed")
	}
	defer mc.close()
	auditAutoAuthOnConnection(t, ctx, statePath, saved, mc)
}

func auditAutoAuthOnConnection(t *testing.T, ctx context.Context, statePath string, saved nativeAuditState, mc *mmtlsClient) {
	t.Helper()
	file, err := os.OpenFile(statePath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		t.Fatal("new private output state required before network request")
	}
	defer file.Close()
	request := append(pbLen(1, pbLen(1, saved.AppDevice)), pbVar(2, appVersion)...)
	request = append(request, pbLen(3, saved.AutoKey)...)
	// Native field 4 is optional login-context bytes, not the enc key.
	temp := &manualAuthTemp{}
	hybrid, err := auditHybridEncrypt(request, saved.EncKey, temp)
	if err != nil {
		t.Fatal("auto hybrid encryption failed")
	}
	ints, byts, _, err := decodeWpkgHead(sessionWpkgHead(saved.Session.UIN, saved.Session.F9, saved.Device))
	if err != nil {
		t.Fatal("candidate header decode failed")
	}
	ints[25] = 17
	// wpkg 2 reads separate storage, but the full UpdateAppSession caller
	// can set it from the application identity (0x10100d--0x101032).
	// Keep earlier zero-UIN candidates reproducible, then test that path
	// explicitly instead of assuming separate storage must imply zero.
	ints[2] = 0
	headerStyle := os.Getenv("YYB_AUTOAUTH_AUDIT_HEADER_STYLE")
	if headerStyle == "nativeapp" || headerStyle == "nativeidentity" {
		// Req2Buf's application-account branch sets 18=0 (0x1a2df0),
		// whereas its independent non-application branch sets 18=1.
		ints[18] = 0
	}
	if headerStyle == "nativeidentity" || headerStyle == "nativeinstance" || headerStyle == "nativeserverid" {
		ints[2] = uint64(saved.Session.UIN)
	} else if headerStyle != "" && headerStyle != "existing" && headerStyle != "nativeapp" {
		t.Fatal("unsupported audit header style")
	}
	// IlinkAppInstanceImpl explicitly clears IsAppAccount during its
	// constructor (YYB 0x190c46--0x190c4f). Req2Buf consequently selects
	// field 18=1 even though UpdateAppSession copies the application UIN
	// into the network identity. The device instance sets the flag true.
	if headerStyle == "nativeinstance" || headerStyle == "nativeserverid" {
		ints[18] = 1
	}
	transport := os.Getenv("YYB_AUTOAUTH_AUDIT_TRANSPORT")
	if transport != "" && transport != "long" && transport != "short" {
		t.Fatal("unsupported audit transport")
	}
	if transport == "" {
		transport = "long"
	}
	auditOutput("auto_request_shape", map[string]any{
		"network_identity_matches_application": ints[2] == uint64(saved.Session.UIN),
		"application_account_flag":             ints[18], "application_crypto": ints[25],
		"transport_crypto": ints[7], "credentials_written": false,
		"transport":             transport,
		"same_manual_transport": transport == "long" && os.Getenv("YYB_AUTOAUTH_AUDIT_SAME_CONNECTION") == "1"})
	// Preserve prior username candidates as negative controls. The actual
	// TDI getter at 0x3a9c0 reads/decodes kTdiKeyServerId, saved from auth
	// section field 9 at 0x7a0db/0x7a3bf. CommSession field 5 reaches
	// PackRequest's 0x1709a0 getter and ultimately wpkg bytes field 27.
	identity := pbParse(safeBytes(pbParse(safeBytes(pbParse(saved.Response)[3]))[3]))
	username := safeBytes(identity[2])
	if headerStyle == "nativeserverid" {
		if len(saved.Session.F9) == 0 {
			t.Fatal("source response server ID missing")
		}
		byts[27] = saved.Session.F9
	} else if len(username) == 0 {
		t.Fatal("source response username missing; header mapping needs review")
	} else {
		byts[27] = username
	}
	body := append(wpkgHead(ints, byts), hybrid...)
	// One request only: never replay a possibly consumed AutoKey on another node.
	var response shortlinkPacket
	if transport == "short" {
		response, err = auditAutoAuthShortlink(ctx, saved.PSK, body)
	} else {
		if mc.sendApp(3198, body) != nil {
			t.Fatal("auto request send failed")
		}
		response, err = mc.recvApp()
	}
	if err != nil {
		t.Fatal("auto response receive failed")
	}
	captured := saved
	captured.WireResponse = response.Body
	captured.WireCommand = response.Cmd
	captured.Response = nil
	captured.EphemeralPrivate = temp.Ephemeral.Bytes()
	captured.OKM, captured.Compressed = temp.OKM, temp.CompBody
	captured.RequestCipher = safeBytes(pbParse(hybrid)[5])
	// Preserve any server rotation in private storage even if parsing fails.
	defer func() {
		encoded, e := json.Marshal(captured)
		if e != nil {
			t.Error("private auto state encode failed")
			return
		}
		if _, e = file.Write(encoded); e != nil {
			t.Error("private auto state write failed")
		}
	}()
	ints, _, hlen, err := decodeWpkgHead(response.Body)
	if err != nil || hlen > len(response.Body) {
		t.Fatal("auto response header malformed")
	}
	transportCode, appCode := int64(ints[4]), int64(ints[23])
	if transportCode != 0 || appCode != 0 || hlen == len(response.Body) {
		auditOutput("auto_header_rejected", map[string]any{"command": response.Cmd,
			"transport_code": transportCode, "application_code": appCode, "body_length": len(response.Body)})
		t.Fatal("auto rejected before encrypted response")
	}
	fields := pbParse(response.Body[hlen:])
	credentialType, _ := fields[2].(int64)
	auditOutput("auto_response", map[string]any{"command": response.Cmd, "credential_type": credentialType,
		"header_crypto": ints[25], "body_length": len(response.Body), "credentials_written": false})
	if response.Cmd != 3198 || credentialType != 1 && credentialType != 2 {
		t.Fatal("auto audit response command or credential type unsupported")
	}
	if credentialType == 1 {
		if auditVerifyHybridSignature(captured.RequestCipher, response.Body[hlen:]) != nil {
			t.Fatal("auto response signature verification failed")
		}
		auditOutput("auto_signature", map[string]any{"verified": true, "request_bound": true})
	}
	decoded, err := hybridECDHDecrypt(response.Body[hlen:], temp)
	if err != nil {
		t.Fatal("auto authenticated response decrypt failed")
	}
	captured.Response = decoded
	parsed := pbParse(decoded)
	base := safeBytes(parsed[1])
	code, _ := pbParse(base)[1].(int64)
	auditOutput("auto_business", map[string]any{"code": code, "unified_auth_present": len(safeBytes(parsed[2])) > 0})
	if len(base) == 0 || code != 0 {
		t.Fatal("auto rejected; no OAuth refresh attempted")
	}
	unified := safeBytes(parsed[2])
	sect := pbParse(safeBytes(pbParse(unified)[2]))
	if next := safeBytes(sect[3]); len(next) > 0 {
		captured.AutoKey = next
	}
	if next := safeBytes(sect[4]); len(next) > 0 {
		captured.EncKey = next
	}
	session, err := extractSession(append(pbLen(1, base), pbLen(3, unified)...))
	if err != nil {
		t.Fatal("auto session parsing failed")
	}
	if session.UIN != saved.Session.UIN {
		t.Fatal("auto identity differs from source session")
	}
	session.DeviceID, session.HostAppID = saved.Session.DeviceID, saved.Session.HostAppID
	psk := saved.PSK
	if transport == "long" {
		keys, keyErr := mc.extractPSKs()
		if keyErr != nil {
			t.Fatal("auto transport keys unavailable")
		}
		var ok bool
		psk, ok = pickAccessPSK(keys)
		if !ok {
			t.Fatal("auto access PSK missing")
		}
	}
	captured.Session, captured.PSK = session, psk
	auditOutput("auto_keys", map[string]any{"auto_key_length": len(captured.AutoKey), "enc_key_length": len(captured.EncKey),
		"auto_key_changed": !bytes.Equal(saved.AutoKey, captured.AutoKey), "enc_key_changed": !bytes.Equal(saved.EncKey, captured.EncKey)})
	st := WmpfSession{Session: session, PSK: psk, ShortlinkTargets: getShortlinkTargets(ctx, 4*time.Second, time.Minute)}
	plain := buildJSAPIPlaintext(session.UIN, "wx3bb18aff4f2fe45f", jsLoginURL, jsLoginCmdID, nil, session.HostAppID, nil)
	envelope, err := buildTransferPacket(session, plain)
	if err != nil {
		t.Fatal("auto code packet build failed")
	}
	loginCode, _, err := NewPool(DefaultConfig(), nil).sendEnvelope(ctx, st, envelope)
	auditOutput("auto_code", map[string]any{"success": err == nil && len(loginCode) > 0, "code_length": len(loginCode)})
	if err != nil || len(loginCode) == 0 {
		t.Fatal("auto session code failed")
	}
}

// Native HashObj +0x90 first consumes request field 5 (PCMgr 0x17d26d),
// then response field 3 (0x17bc25). Type 1 verifies the resulting SHA256
// with the statically configured application ECDSA public key.
func auditVerifyHybridSignature(requestCipher, response []byte) error {
	return verifyHybridSignature(requestCipher, response)
}

func TestNativeAutoAuthOfflineAudit(t *testing.T) {
	path := os.Getenv("YYB_AUTOAUTH_OFFLINE_PATH")
	if path == "" {
		t.Skip("requires private captured response; no network requests")
	}
	if !filepath.IsAbs(path) {
		t.Fatal("private path must be absolute")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private capture unavailable")
	}
	var saved nativeAuditState
	if json.Unmarshal(raw, &saved) != nil {
		t.Fatal("private capture malformed")
	}
	_, _, hlen, err := decodeWpkgHead(saved.WireResponse)
	if err != nil || hlen >= len(saved.WireResponse) {
		t.Fatal("captured header malformed")
	}
	response := saved.WireResponse[hlen:]
	verified := auditVerifyHybridSignature(saved.RequestCipher, response) == nil
	key, err := ecdh.P256().NewPrivateKey(saved.EphemeralPrivate)
	if err != nil {
		t.Fatal("captured local ephemeral key unavailable")
	}
	decoded, err := hybridECDHDecrypt(response, &manualAuthTemp{Ephemeral: key, OKM: saved.OKM, CompBody: saved.Compressed})
	if err != nil {
		t.Fatal("offline response GCM authentication failed")
	}
	fields := pbParse(decoded)
	base := pbParse(safeBytes(fields[1]))
	message := safeBytes(base[2])
	unified := pbParse(safeBytes(fields[2]))
	sect := pbParse(safeBytes(unified[2]))
	auditOutput("offline_auto_response", map[string]any{"signature_verified": verified, "business_code": base[1], "unified_auth_present": len(safeBytes(fields[2])) > 0,
		"auto_key_length": len(safeBytes(sect[3])), "enc_key_length": len(safeBytes(sect[4])), "send_key_length": len(safeBytes(sect[1])),
		"error_message_present": len(message) > 0, "error_mentions_expiry": regexp.MustCompile(`(?i)expir|timeout|time.?out`).Match(message),
		"error_mentions_frequency": regexp.MustCompile(`(?i)frequen|rate.?limit|too.?many`).Match(message),
		"network_requests":         0, "credentials_written": false})
}

// Audit-only counterpart of the URL-based path advertised by native AutoAuth
// request metadata. One explicit target and no automatic request replay.
func auditAutoAuthShortlink(ctx context.Context, psk pskEntry, payload []byte) (shortlinkPacket, error) {
	url, host := []byte("/ilink/ilinkapp/sys/ilinkapp_autoauth"), []byte("shortcloud.weixin.com")
	packet := buildShortlink(3198, 0, payload, defaultVer)
	envelope := make([]byte, 4)
	envelope = binary.BigEndian.AppendUint16(envelope, uint16(len(url)))
	envelope = append(envelope, url...)
	envelope = binary.BigEndian.AppendUint16(envelope, uint16(len(host)))
	envelope = append(envelope, host...)
	envelope = binary.BigEndian.AppendUint32(envelope, uint32(len(packet)))
	envelope = append(envelope, packet...)
	binary.BigEndian.PutUint32(envelope, uint32(len(envelope)-4))
	body, key, hello, type8, err := build0RTTRequest(psk, envelope)
	if err != nil {
		return shortlinkPacket{}, err
	}
	targets := getShortlinkTargets(ctx, 4*time.Second, time.Minute)
	if len(targets) == 0 {
		return shortlinkPacket{}, errors.New("audit shortlink target unavailable")
	}
	conn, err := dialTCP(ctx, targets[0].IP, targets[0].Port, 12*time.Second, "", false)
	if err != nil {
		return shortlinkPacket{}, err
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	_ = conn.SetDeadline(ioDeadline(ctx, 12*time.Second))
	if _, err = conn.Write(httpPost(fmt.Sprintf("/mmtls/%08x", uint32(time.Now().Unix())), string(host), body)); err != nil {
		return shortlinkPacket{}, err
	}
	raw, err := io.ReadAll(io.LimitReader(conn, 2<<20))
	if err != nil {
		return shortlinkPacket{}, err
	}
	_, data := splitHTTP(raw)
	records, consumed := parseRecords(data)
	if consumed != len(data) {
		return shortlinkPacket{}, errors.New("audit shortlink records truncated")
	}
	var serverHello, appData []byte
	for _, record := range records {
		if record.ContentType == ctHandshake && serverHello == nil {
			serverHello = record.Payload
		}
		if record.ContentType == ctAppData && appData == nil {
			appData = record.Payload
		}
	}
	if serverHello == nil || appData == nil {
		return shortlinkPacket{}, errors.New("audit shortlink response missing authenticated data")
	}
	for _, transcript := range [][][]byte{{hello, serverHello}, {hello, type8, serverHello}, {hello, serverHello, type8}} {
		hash := sha256.Sum256(bytes.Join(transcript, nil))
		keys := derivePSKOneWayKeys(key, labelHandshakeKeys, hash[:])
		for _, seq := range []uint64{2, 1, 3} {
			plain, err := decryptRecordPayload(keys.Key, keys.IV, seq, ctAppData, appData)
			if err == nil {
				return parseShortlink(plain)
			}
		}
	}
	return shortlinkPacket{}, errors.New("audit shortlink authentication failed")
}

func auditHybridEncrypt(plain, encKey []byte, temp *manualAuthTemp) ([]byte, error) {
	return hybridECDHEncryptWithKey(plain, serverPub, encKey, temp)
}
