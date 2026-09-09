package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestAccountLinkURLUsesForwardedPublicAddress(t *testing.T) {
	app := &App{}
	req := httptest.NewRequest("POST", "http://yyb-go:8000/api/account-links", nil)
	req.Host = "192.168.9.83:8000"
	req.Header.Set("X-Forwarded-Host", "yyb.example.com")
	req.Header.Set("X-Forwarded-Proto", "https")
	got := app.accountLinkURL(req, "abc123")
	want := "https://yyb.example.com/account-link/abc123"
	if got != want {
		t.Fatalf("accountLinkURL() = %q, want %q", got, want)
	}
}

func TestAccountLinkURLUsesForwardedParameter(t *testing.T) {
	app := &App{}
	req := httptest.NewRequest("POST", "http://yyb-go:8000/api/account-links", nil)
	req.Host = "192.168.9.83:8000"
	req.Header.Set("Forwarded", `for=192.0.2.10;proto=https;host="public.example:443"`)
	got := app.accountLinkURL(req, "abc123")
	want := "https://public.example:443/account-link/abc123"
	if got != want {
		t.Fatalf("accountLinkURL() = %q, want %q", got, want)
	}
}

func TestAccountLinkURLRejectsMalformedForwardedHost(t *testing.T) {
	app := &App{}
	req := httptest.NewRequest("POST", "http://yyb-go:8000/api/account-links", nil)
	req.Host = "192.168.9.83:8000"
	req.Header.Set("X-Forwarded-Host", "https://evil.example/path")
	got := app.accountLinkURL(req, "abc123")
	want := "http://192.168.9.83:8000/account-link/abc123"
	if got != want {
		t.Fatalf("accountLinkURL() = %q, want %q", got, want)
	}
}

func TestCreateUpdateLinkRejectsExistingLinkBeforeInsert(t *testing.T) {
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	defer app.Close()
	account, err := app.db.UpsertAccount(context.Background(), "link-api-openid", "buffer", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatalf("UpsertAccount() error = %v", err)
	}
	request := func() *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/account-links", strings.NewReader(`{"kind":"update","ref":"1","ttl_seconds":600}`))
		req.Header.Set("Content-Type", "application/json")
		response := httptest.NewRecorder()
		app.handleAccountLinksAPI(response, req)
		return response
	}
	first := request()
	if first.Code != http.StatusOK {
		t.Fatalf("first create status = %d body=%s", first.Code, first.Body.String())
	}
	second := request()
	if second.Code != http.StatusConflict {
		t.Fatalf("second create status = %d body=%s", second.Code, second.Body.String())
	}
	var envelope struct {
		Code int `json:"code"`
		Data struct {
			ExistingCount int `json:"existing_count"`
		} `json:"data"`
	}
	if err := json.Unmarshal(second.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode conflict response: %v", err)
	}
	if envelope.Code != http.StatusConflict || envelope.Data.ExistingCount != 1 {
		t.Fatalf("conflict envelope = %#v, want 409 and one existing link", envelope)
	}
	links, err := app.db.ListAccountLinks(context.Background(), nil)
	if err != nil {
		t.Fatalf("ListAccountLinks() error = %v", err)
	}
	if len(links) != 1 || links[0].AccountID != account.ID {
		t.Fatalf("stored links = %#v, want exactly the first link", links)
	}
}
