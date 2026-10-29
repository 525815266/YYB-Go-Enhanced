package protocol

import (
	"bytes"
	"encoding/json"
	"testing"

	"yyb_go/internal/pcmgr"
)

func TestJSAPIRequestsKeepProviderIdentityAndNativeLayout(t *testing.T) {
	const app = "wx0000000000000000"
	for _, provider := range []struct {
		name string
		host []byte
	}{
		{"legacy", nil}, {"yyb", hostAppIDDefault}, {"pcmgr", []byte(pcmgr.HostAppID)},
	} {
		t.Run(provider.name, func(t *testing.T) {
			wantHost := provider.host
			if len(wantHost) == 0 {
				wantHost = hostAppIDDefault
			}
			payload := map[string]any{"api_name": "webapi_getuserencryptkey", "data": map[string]any{"kept": true}, "env": 1}
			operate, err := buildOperateRequest(1234, app, payload, provider.host)
			if err != nil {
				t.Fatal(err)
			}
			for _, request := range []struct {
				name string
				raw  []byte
				url  []byte
				cmd  int64
			}{
				{"login", buildJSAPIPlaintext(1234, app, nil, 0, nil, provider.host, nil), jsLoginURL, 1029},
				{"phone", buildPhoneRequest(1234, app, provider.host), phoneURL, map[bool]int64{false: 1020, true: 1141}[provider.name == "pcmgr"]},
				{"operate", operate, operateURL, 1133},
			} {
				fields := pbParse(request.raw)
				if !bytes.Equal(safeBytes(fields[3]), wantHost) || !bytes.Equal(safeBytes(fields[2]), request.url) || fields[7] != request.cmd || string(safeBytes(fields[6])) != app {
					t.Fatalf("%s lost provider or routing fields", request.name)
				}
				body := pbParse(safeBytes(fields[5]))
				if string(safeBytes(body[2])) != app {
					t.Fatal("target AppID changed")
				}
				if request.name == "operate" || request.name == "phone" && provider.name == "pcmgr" {
					var forwarded map[string]any
					if json.Unmarshal(safeBytes(body[3]), &forwarded) != nil {
						t.Fatal("missing native JSON data field")
					}
					if request.name == "phone" {
						if forwarded["api_name"] != "webapi_getuserwxphone" || body[4] != nil || body[7] != nil {
							t.Fatal("phone request still uses the legacy login layout")
						}
					} else if forwarded["api_name"] != payload["api_name"] || forwarded["data"].(map[string]any)["kept"] != true || body[6] != map[bool]int64{false: 0, true: 1}[provider.name == "pcmgr"] {
						t.Fatal("operate data or provider versionType changed")
					}
				}
			}
		})
	}
}
