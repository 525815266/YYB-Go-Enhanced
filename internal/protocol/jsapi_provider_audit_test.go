//go:build protocolaudit

package protocol

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Explicit opt-in, private cached session only. Never refreshes credentials,
// opens a production DB or prints codes, identity, keys or raw responses.
func TestPCMgrCachedJSAPIComparison(t *testing.T) {
	path := os.Getenv("YYB_PCMGR_JSAPI_SESSION")
	if path == "" {
		t.Skip("requires an explicit private PCMgr lab session")
	}
	if !filepath.IsAbs(path) {
		t.Fatal("private session path must be absolute")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private session unavailable")
	}
	st, err := labReadSession("pcmgr", raw)
	if err != nil {
		t.Fatal("requires an unexpired PCMgr lab session")
	}
	pool := NewPool(DefaultConfig(), nil)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	const appID = "wx3bb18aff4f2fe45f"
	for _, name := range []string{"login_before", "key_legacy_host", "key_provider_host", "phone_provider", "login_after"} {
		var plain []byte
		switch name {
		case "key_legacy_host", "key_provider_host":
			host := st.Session.HostAppID
			if name == "key_legacy_host" {
				host = nil
			}
			plain, err = buildOperateRequest(st.Session.UIN, appID, map[string]any{"api_name": "webapi_getuserencryptkey", "data": map[string]any{}, "env": 1}, host)
		case "phone_provider":
			plain = buildPhoneRequest(st.Session.UIN, appID, st.Session.HostAppID)
		default:
			plain = buildJSAPIPlaintext(st.Session.UIN, appID, nil, 0, nil, st.Session.HostAppID, nil)
		}
		if err != nil {
			t.Fatal("request build failed")
		}
		envelope, buildErr := buildTransferPacket(st.Session, plain)
		if buildErr != nil {
			t.Fatal("packet build failed")
		}
		code, response, callErr := pool.sendEnvelope(ctx, st, envelope)
		result := map[string]any{"operation": name, "transport_ok": callErr == nil, "code_length": len(code)}
		var rejected *ShortlinkRejectedError
		if errors.As(callErr, &rejected) {
			result["shortlink_code"] = rejected.Code
		}
		if callErr == nil && name != "login_before" && name != "login_after" {
			for _, candidate := range [][]byte{code, safeBytes(pbParse(response)[2])} {
				var value map[string]any
				if json.Unmarshal(candidate, &value) != nil {
					continue
				}
				for _, field := range []string{"err_no", "errcode", "ret"} {
					if v, ok := value[field].(float64); ok {
						result[field] = v
					}
				}
			}
		}
		out, _ := json.Marshal(result)
		t.Log(string(out))
		if name == "login_before" && (callErr != nil || len(code) == 0) {
			t.Skip("baseline session rejected; operation tests not sent")
		}
	}
}
