package httpapi

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"yyb_go/internal/auth"
	"yyb_go/internal/pcmgr"
	"yyb_go/internal/protocol"
	"yyb_go/internal/store"
)

func TestPCMgrRenewScheduleDoesNotUseOAuthLeadTime(t *testing.T) {
	now := time.Now().Truncate(time.Second)
	c := protocol.LoginBufferCredentials{Provider: "pcmgr", PCMgr: pcmgr.Credentials{RenewedAt: now.Unix(), RenewInterval: 60}}
	if credentialsDueForRefresh(c, now, 45*time.Minute) {
		t.Fatal("OAuth lead time triggered immediate native key rotation")
	}
	if !credentialsDueForRefresh(c, now.Add(time.Minute), 45*time.Minute) {
		t.Fatal("native key renewal schedule was skipped")
	}
}

func TestLocalModeCanManageScanSettings(t *testing.T) {
	a, err := NewApp(Config{ResourceRoot: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	w := httptest.NewRecorder()
	a.Handler().ServeHTTP(w, httptest.NewRequest("GET", "/settings", nil))
	if w.Code != 200 {
		t.Fatal("settings unavailable in local mode", w.Code)
	}
	w = httptest.NewRecorder()
	a.Handler().ServeHTTP(w, httptest.NewRequest("PUT", "/api/scan-settings", strings.NewReader(`{"scope":"global","product":"pcmgr"}`)))
	if w.Code != 200 {
		t.Fatal(w.Code)
	}
}

func TestScanDefaultsPriorityAndPermissions(t *testing.T) {
	a, err := NewApp(Config{ResourceRoot: t.TempDir(), AuthDriver: "sqlite"})
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	ctx := context.Background()
	request := httptest.NewRequest("GET", "/qr", nil)
	if p, err := a.scanProduct(request, ""); err != nil || p != "appstore" {
		t.Fatal(p, err)
	}
	put := func(role string, id int64, body string) int {
		r := httptest.NewRequest("PUT", "/api/scan-settings", strings.NewReader(body))
		r = r.WithContext(context.WithValue(ctx, authUserKey, &auth.User{ID: id, Role: role}))
		w := httptest.NewRecorder()
		a.handleScanSettings(w, r)
		return w.Code
	}
	if got := put("user", 2, `{"scope":"global","product":"pcmgr"}`); got != 403 {
		t.Fatal(got)
	}
	if got := put("admin", 1, `{"scope":"global","product":"pcmgr"}`); got != 200 {
		t.Fatal(got)
	}
	if got := put("user", 2, `{"scope":"personal","product":"appstore"}`); got != 200 {
		t.Fatal(got)
	}
	r := request.WithContext(context.WithValue(ctx, authUserKey, &auth.User{ID: 2, Role: "user"}))
	if p, err := a.scanProduct(r, ""); err != nil || p != "appstore" {
		t.Fatal(p, err)
	}
	if p, err := a.scanProduct(r, "pcmgr"); err != nil || p != "pcmgr" {
		t.Fatal(p, err)
	}
	if got := put("user", 2, `{"scope":"personal","product":""}`); got != 200 {
		t.Fatal(got)
	}
	if p, err := a.scanProduct(r, ""); err != nil || p != "pcmgr" {
		t.Fatal(p, err)
	}
	if p, err := a.scanProduct(request.WithContext(context.WithValue(ctx, authUserKey, &auth.User{ID: 3})), ""); err != nil || p != "pcmgr" {
		t.Fatal(p, err)
	}
	if got := put("admin", 1, `{"scope":"global","product":"other"}`); got != 422 {
		t.Fatal(got)
	}
	w := httptest.NewRecorder()
	a.Handler().ServeHTTP(w, httptest.NewRequest("PUT", "/api/scan-settings", strings.NewReader(`{"scope":"global","product":"pcmgr"}`)))
	if w.Code != http.StatusUnauthorized {
		t.Fatal("anonymous settings write", w.Code)
	}
}

func TestUpdateLinkCannotSwitchProductWithoutConsentOrAllocateQR(t *testing.T) {
	a, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	ctx := context.Background()
	acc, err := a.db.UpsertAccount(ctx, "pcmgr:42", "old", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	link := &store.AccountLink{Kind: accountLinkKindUpdate, AccountID: acc.ID}
	w := httptest.NewRecorder()
	a.handleAccountLinkQRCreate(w, httptest.NewRequest("POST", "/account-link/test/qr", strings.NewReader(`{"product":"appstore"}`)), link)
	if w.Code != 422 || len(a.qrSessions) != 0 {
		t.Fatal("wrong channel allocated QR", w.Code)
	}
	if p, err := a.linkScanProduct(ctx, link, acc, ""); err != nil || p != "pcmgr" {
		t.Fatal(p, err)
	}
	link.Kind = accountLinkKindAdd
	owner := int64(42)
	link.OwnerUserID = &owner
	if err := a.db.SetSetting(ctx, scanUserKey(owner), "pcmgr"); err != nil {
		t.Fatal(err)
	}
	if p, err := a.linkScanProduct(ctx, link, acc, ""); err != nil || p != "pcmgr" {
		t.Fatal(p, err)
	}
	if p, err := a.linkScanProduct(ctx, link, acc, "appstore"); err != nil || p != "appstore" {
		t.Fatal(p, err)
	}
}
