package httpapi

import (
	"net/http/httptest"
	"testing"
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
