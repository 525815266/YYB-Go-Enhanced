package protocol

import (
	"bytes"
	"context"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/pcmgr"
	"yyb_go/internal/store"
)

func TestRefreshPSKSurvivesDatabaseReload(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(filepath.Join(t.TempDir(), "refresh.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	acc, err := db.UpsertAccount(ctx, "synthetic-refresh-psk", "", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	fresh := syntheticNativeSession()
	fresh.RefreshPSK = pskEntry{PSKType: 2, PreSharedKey: "aabb", TicketEntry: "ccdd", Lifetime: 3600, ExpiredAt: time.Now().Unix() + 3600}
	if err := db.PutSessionForAccount(ctx, acc, fresh.Session.UIN, fresh.toBlob(), fresh.PSK.ExpiredAt, ""); err != nil {
		t.Fatal(err)
	}
	row, err := db.GetSessionForRecovery(ctx, acc.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	got, err := sessionFromBlob(row.SessionBlob)
	if err != nil || got.RefreshPSK != fresh.RefreshPSK || got.PSK != fresh.PSK {
		t.Fatal("refresh/access PSK lost or interchanged during reload")
	}
	if _, ok := pickAccessPSK([]pskEntry{fresh.RefreshPSK}); ok {
		t.Fatal("refresh PSK selected for application traffic")
	}
}

// A fresh login uses an int64 directly. All later JSAPI calls reload its JSON
// cache. They must send exactly the same identity, including the low 32 bits
// carried in both the inner and outer JSAPI session info.
func TestCachedRequestsPreserveFreshLoginIdentity(t *testing.T) {
	ctx := context.Background()
	for _, host := range []string{string(hostAppIDDefault), pcmgr.HostAppID} {
		t.Run(host, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "requests.db")
			db, err := store.Open(path)
			if err != nil {
				t.Fatal(err)
			}
			defer func() { db.Close() }()
			acc, err := db.UpsertAccount(ctx, "synthetic-session-precision", "", nil, nil, nil, nil, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			fresh := syntheticNativeSession()
			fresh.Session.UIN = 1<<60 + 12345
			fresh.Session.HostAppID = []byte(host)
			fresh.PSK.ExpiredAt = time.Now().Add(time.Hour).Unix()
			if err := db.PutSessionForAccount(ctx, acc, fresh.Session.UIN, fresh.toBlob(), fresh.PSK.ExpiredAt, ""); err != nil {
				t.Fatal(err)
			}
			device := []byte("02-11-22-33-44-55")
			payload := map[string]any{"api_name": "webapi_getuserencryptkey", "data": map[string]any{}, "env": 1}
			requests := func(st WmpfSession) map[string][]byte {
				t.Helper()
				operate, err := buildOperateRequest(st.Session.UIN, "wx0000000000000001", payload, st.Session.HostAppID, device)
				if err != nil {
					t.Fatal(err)
				}
				return map[string][]byte{
					"wpkg":             sessionWpkgHead(st.Session.UIN, st.Session.F9, st.Session.DeviceID),
					"get_code":         buildJSAPIPlaintext(st.Session.UIN, "wx0000000000000001", jsLoginURL, jsLoginCmdID, nil, st.Session.HostAppID, device),
					"get_phone_number": buildPhoneRequest(st.Session.UIN, "wx0000000000000001", st.Session.HostAppID, device),
					"operate_wx_data":  operate,
				}
			}
			want := requests(fresh)
			for call := 0; call < 3; call++ {
				if call == 2 {
					if err := db.Close(); err != nil {
						t.Fatal(err)
					}
					db, err = store.Open(path)
					if err != nil {
						t.Fatal(err)
					}
				}
				pool := NewPool(DefaultConfig(), db)
				got, err := pool.state(ctx, "", acc.ID, "", false, acc)
				if err != nil {
					t.Fatal(err)
				}
				for operation, plain := range requests(got) {
					if !bytes.Equal(plain, want[operation]) {
						t.Errorf("%s changed after cache read %d", operation, call+1)
					}
				}
			}
		})
	}
}
