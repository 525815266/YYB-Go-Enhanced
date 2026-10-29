//go:build protocolaudit

package protocol

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Separates MMTLS authentication from application decryption against one
// explicitly supplied private snapshot. No production database is opened.
func TestNativeCachedSessionWireAudit(t *testing.T) {
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
	if err != nil || st.TCPProxy != "" || st.PSK.ExpiredAt <= time.Now().Unix() || len(st.ShortlinkTargets) == 0 {
		t.Fatal("requires an unexpired saved direct session")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	plain := buildJSAPIPlaintext(st.Session.UIN, "wx3bb18aff4f2fe45f", jsLoginURL, jsLoginCmdID, nil, st.Session.HostAppID, nil)
	envelope, err := buildTransferPacket(st.Session, plain)
	if err != nil {
		t.Fatal("audit packet build failed")
	}
	body, psk, hello, type8, err := build0RTTRequest(st.PSK, envelope)
	if err != nil {
		t.Fatal("audit transport build failed")
	}
	target := st.ShortlinkTargets[0]
	conn, err := dialTCP(ctx, target.IP, target.Port, 12*time.Second, "", false)
	if err != nil {
		t.Fatal("audit transport unavailable")
	}
	defer conn.Close()
	_ = conn.SetDeadline(ioDeadline(ctx, 12*time.Second))
	if _, err = conn.Write(httpPost(fmt.Sprintf("/mmtls/%08x", uint32(time.Now().Unix())), "shortcloud.weixin.com", body)); err != nil {
		t.Fatal("audit transport write failed")
	}
	raw, err = io.ReadAll(conn)
	if err != nil {
		t.Fatal("audit transport read failed")
	}
	_, responseBody := splitHTTP(raw)
	records, _ := parseRecords(responseBody)
	var serverHello, appData []byte
	for _, record := range records {
		if record.ContentType == ctHandshake && serverHello == nil {
			serverHello = record.Payload
		}
		if record.ContentType == ctAppData && appData == nil {
			appData = record.Payload
		}
	}
	result := map[string]any{"at": time.Now().Unix(), "record_count": len(records),
		"server_hello_length": len(serverHello), "app_data_length": len(appData),
		"mmtls_authenticated": false, "application_authenticated": false,
		"credentials_written": false, "new_handshake_requested": false}
	defer func() { auditOutput("saved_session_wire", result) }()
	if serverHello == nil || appData == nil {
		return
	}
	for _, transcript := range [][][]byte{{hello, serverHello}, {hello, type8, serverHello}, {hello, serverHello, type8}} {
		hash := sha256.Sum256(bytes.Join(transcript, nil))
		keys := derivePSKOneWayKeys(psk, labelHandshakeKeys, hash[:])
		for _, sequence := range []uint64{2, 1, 3} {
			decrypted, err := decryptRecordPayload(keys.Key, keys.IV, sequence, ctAppData, appData)
			if err != nil {
				continue
			}
			result["mmtls_authenticated"] = true
			result["decrypted_length"] = len(decrypted)
			payload := decrypted
			if packet, err := parseShortlink(decrypted); err == nil {
				result["command"] = packet.Cmd
				payload = packet.Body
			}
			if fields, _, size, err := decodeWpkgHead(payload); err == nil {
				result["transport_code"] = int64(fields[4])
				result["application_code"] = int64(fields[23])
				result["body_length"] = len(payload) - size
				result["application_crypto"] = fields[25]
			}
			response, err := sessionDecrypt(payload, st.Session.RecvKey)
			result["application_authenticated"] = err == nil
			if err == nil {
				result["code_length"] = len(extractCode(response))
			}
			return
		}
	}
}
