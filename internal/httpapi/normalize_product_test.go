package httpapi

import "testing"

func TestNormalizeLoginProductAcceptsGameAssist(t *testing.T) {
	p, err := normalizeLoginProduct(" GameAssist ")
	if err != nil || p != "gameassist" {
		t.Fatalf("gameassist channel must be accepted, got %q err %v", p, err)
	}
	if p, err := normalizeLoginProduct(""); err != nil || p != "appstore" {
		t.Fatalf("empty must default to appstore, got %q err %v", p, err)
	}
	if _, err := normalizeLoginProduct("bogus"); err == nil {
		t.Fatal("unknown channel must fail")
	}
}
