package qr

import (
	"context"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestScanChannelsUseIndependentOAuthIdentities(t *testing.T) {
	for _, product := range []string{"appstore", "pcmgr"} {
		address, err := OAuthURL(product)
		if err != nil {
			t.Fatal(err)
		}
		u, _ := url.Parse(address)
		q := u.Query()
		if !strings.Contains(q.Get("scope"), "snsapi_runtime_pcsdk") {
			t.Fatal("missing mini-program authorization")
		}
		if (q.Get("appid") == AppID) != (product == "appstore") {
			t.Fatal("crossed channel identity")
		}
		c := NewClient(time.Second)
		c.transport = roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.URL.Query().Get("appid") != q.Get("appid") {
				t.Fatal("wrong QR provider")
			}
			return &http.Response{StatusCode: 200, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`<img src="/connect/qrcode/test-only">`))}, nil
		})
		a, err := c.CreateSessionForProduct(context.Background(), product)
		if err != nil {
			t.Fatal(err)
		}
		b, err := c.CreateSessionForProduct(context.Background(), product)
		if err != nil {
			t.Fatal(err)
		}
		if a.Product != product || len(a.ClientGUID) != 32 || a.ClientGUID == b.ClientGUID {
			t.Fatal("QR identity not isolated")
		}
	}
	if _, err := OAuthURL("unknown"); err == nil {
		t.Fatal("unknown provider accepted")
	}
}
