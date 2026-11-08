package store

import (
	"context"
	"path/filepath"
	"testing"
)

func TestCredentialWipePreservesEveryChannel(t *testing.T) {
	for _, provider := range []string{"gameassist", "pcmgr", "qbbrowser", "ldplayer"} {
		for _, method := range []string{"expired", StatusSessionOnly, "manual-expired"} {
			t.Run(provider+"/"+method, func(t *testing.T) {
				path := filepath.Join(t.TempDir(), "accounts.db")
				db, err := Open(path)
				if err != nil {
					t.Fatal(err)
				}
				defer func() { db.Close() }()
				ctx := context.Background()
				status := "alive"
				// Deliberately omit a prefix: the marker must survive independently.
				acc, err := db.UpsertAccount(ctx, "synthetic-identity", "ticket", nil, nil, nil, nil,
					map[string]any{"provider": provider, "accesstoken": "secret", "refreshtoken": "secret", "qbbrowser": map[string]any{"token": "secret"}}, &status)
				if err != nil {
					t.Fatal(err)
				}
				if err = db.SetAccountFleet(ctx, acc.ID, acc.ID); err != nil {
					t.Fatal(err)
				}
				if method == "manual-expired" {
					err = db.SetAccountStatus(ctx, acc.ID, "expired")
				} else {
					var applied bool
					applied, err = db.ApplyCredentialRefresh(ctx, acc, "ticket", nil, method)
					if !applied {
						t.Fatal("wipe was not applied", err)
					}
				}
				if err != nil {
					t.Fatal(err)
				}
				db.Close()
				db, err = Open(path)
				if err != nil {
					t.Fatal(err)
				}
				saved, err := db.GetAccount(ctx, acc.ID)
				if err != nil {
					t.Fatal(err)
				}
				wantStatus := method
				if method == "manual-expired" {
					wantStatus = "expired"
				}
				if saved.Public().ScanProduct != provider || saved.Status == nil || *saved.Status != wantStatus || saved.LoginBuffer != "" {
					t.Fatal("wipe changed channel/status or retained a rejected ticket")
				}
				if len(saved.Credentials) != 1 || saved.Credentials["provider"] != provider {
					t.Fatal("wipe must keep only the non-secret channel marker")
				}
				if saved.FleetPrimaryID == nil || *saved.FleetPrimaryID != acc.ID {
					t.Fatal("wipe changed fleet membership")
				}
			})
		}
	}
}

func TestHistoricalWipedChannelUsesNamespacedIdentity(t *testing.T) {
	for _, provider := range []string{"pcmgr", "qbbrowser", "ldplayer"} {
		acc := &WechatAccount{OpenID: provider + ":synthetic"}
		if got := acc.Public().ScanProduct; got != provider {
			t.Errorf("historical %s record displayed as %s", provider, got)
		}
	}
	// An unmarked native OpenID cannot prove a historical GameAssist identity.
	if got := (&WechatAccount{OpenID: "synthetic"}).Public().ScanProduct; got != "appstore" {
		t.Fatal("guessed a provider for an ambiguous record")
	}
}
