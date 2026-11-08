package httpapi

import (
	"context"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"

	"yyb_go/internal/protocol"
	"yyb_go/internal/qr"
	"yyb_go/internal/store"
)

func TestLDPlayerConfirmationPreparesSessionAndReportsFailure(t *testing.T) {
	for _, outcome := range []string{"success", "ticket_rejected", "network"} {
		t.Run(outcome, func(t *testing.T) {
			app := newKeepAliveTestApp(t)
			defer app.Close()
			creds := &protocol.LoginBufferCredentials{Provider: "ldplayer", OpenID: "ldplayer:synthetic-new-ticket"}
			app.qrSessions["activation"] = &qrLoginSession{Session: &qr.Session{ID: "activation", Product: "ldplayer", LoginBuffer: "synthetic-ticket", Credentials: creds}, Client: app.qr}
			calls := 0
			app.prepareScanSession = func(ctx context.Context, acc *store.WechatAccount, proxy string, fallback bool) error {
				calls++
				stored, err := app.db.GetAccount(ctx, acc.ID)
				if err != nil || stored.LoginBuffer != "synthetic-ticket" || proxy != "" {
					t.Fatal("session prepared before account/proxy commit")
				}
				switch outcome {
				case "ticket_rejected":
					return &protocol.ManualAuthRejectedError{Code: -10}
				case "network":
					return context.DeadlineExceeded
				}
				return nil
			}
			w := httptest.NewRecorder()
			app.handleQR(w, httptest.NewRequest("POST", "/qr/activation/confirm", strings.NewReader("{}")))
			wantStatus, wantHTTP := "alive", 200
			if outcome == "ticket_rejected" {
				wantStatus, wantHTTP = "expired", 502
			}
			if outcome == "network" {
				wantStatus, wantHTTP = "unknown", 502
			}
			rows, _ := app.db.ListAccounts(context.Background())
			if w.Code != wantHTTP || len(rows) != 1 || accountStatus(rows[0]) != wantStatus || rows[0].ScanProduct() != "ldplayer" || calls != 1 {
				t.Fatalf("wrong activation outcome: HTTP=%d calls=%d rows=%d", w.Code, calls, len(rows))
			}
			if outcome == "ticket_rejected" && rows[0].LoginBuffer != "" {
				t.Fatal("rejected ticket retained")
			}
			w = httptest.NewRecorder()
			app.handleQR(w, httptest.NewRequest("POST", "/qr/activation/confirm", strings.NewReader("{}")))
			if w.Code != 404 || calls != 1 {
				t.Fatal("confirmation replayed scan ticket")
			}
		})
	}
}

func TestLDPlayerActivationCannotOverwriteNewScan(t *testing.T) {
	app := newKeepAliveTestApp(t)
	defer app.Close()
	ctx := context.Background()
	acc, err := app.storeFromScan(ctx, "old-ticket", protocol.LoginBufferCredentials{Provider: "ldplayer", OpenID: "ldplayer:synthetic"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	app.prepareScanSession = func(context.Context, *store.WechatAccount, string, bool) error {
		_, err := app.storeFromScan(ctx, "new-ticket", protocol.LoginBufferCredentials{Provider: "ldplayer", OpenID: acc.OpenID, AccessToken: "new-authorization"}, nil)
		if err != nil {
			t.Fatal(err)
		}
		return &protocol.ManualAuthRejectedError{Code: -10}
	}
	if err := app.activateScannedAccount(ctx, acc); !errors.Is(err, store.ErrAccountAuthorizationChanged) {
		t.Fatal(err)
	}
	saved, _ := app.db.GetAccount(ctx, acc.ID)
	if saved.LoginBuffer != "new-ticket" || accountStatus(saved) != "alive" {
		t.Fatal("old activation expired new authorization")
	}
}
