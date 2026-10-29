package protocol

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"yyb_go/internal/store"
)

func TestSessionCacheUsesIssuedPSKDeadline(t *testing.T) {
	issued := time.Now().Add(7 * 24 * time.Hour).Unix()
	st := WmpfSession{PSK: pskEntry{ExpiredAt: issued}}
	if got := sessionExpiresAt(st, 30*time.Minute); got != issued {
		t.Fatalf("cache deadline = %d, want issued PSK deadline %d", got, issued)
	}
	before := time.Now().Unix()
	fallback := sessionExpiresAt(WmpfSession{}, time.Minute)
	if fallback < before+60 || fallback > time.Now().Unix()+60 {
		t.Fatal("missing PSK deadline did not retain the fallback TTL")
	}
}

func TestStateReusesLegacyCacheBeforePSKExpiry(t *testing.T) {
	for _, tc := range []struct {
		name      string
		pskExpiry int64
		cacheLive bool
		badKey    bool
		wantReuse bool
	}{
		{"legacy_local_deadline", time.Now().Add(time.Hour).Unix(), false, false, true},
		{"issued_deadline_expired", time.Now().Add(-time.Minute).Unix(), false, false, false},
		{"issued_deadline_missing", 0, false, false, false},
		{"corrupt_legacy_key", time.Now().Add(time.Hour).Unix(), false, true, false},
		{"local_deadline_cannot_extend_issued_deadline", time.Now().Add(-time.Minute).Unix(), true, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			ctx := context.Background()
			acc, err := db.UpsertAccount(ctx, "synthetic-expiry-account", "", nil, nil, nil, nil, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			st := WmpfSession{
				Session: AppSession{UIN: 1234, SendKey: []byte("synthetic-send"), RecvKey: []byte("synthetic-recv")},
				PSK:     pskEntry{PSKType: 1, PreSharedKey: "001122", TicketEntry: "aabbcc", ExpiredAt: tc.pskExpiry},
			}
			if tc.badKey {
				st.PSK.PreSharedKey = "malformed"
			}
			deadline := time.Now().Add(-time.Minute).Unix()
			if tc.cacheLive {
				deadline = time.Now().Add(time.Hour).Unix()
			}
			if err := db.PutSession(ctx, acc.ID, &st.Session.UIN, st.toBlob(), deadline, ""); err != nil {
				t.Fatal(err)
			}
			pool := NewPool(DefaultConfig(), db)
			// Empty loginBuffer makes a cache miss fail before any network work.
			got, err := pool.state(ctx, "", acc.ID, "", false)
			if !tc.wantReuse {
				if err == nil {
					t.Fatal("ineligible PSK was reused")
				}
				return
			}
			if err != nil || got.Session.UIN != st.Session.UIN || got.PSK.ExpiredAt != tc.pskExpiry {
				t.Fatalf("usable legacy session not reused: %v", err)
			}
			// On-demand ticket recovery uses a new buffer without changing the
			// saved OAuth snapshot. It must remain compatible with the CAS guard.
			if _, err := pool.state(ctx, "new-on-demand-buffer", acc.ID, "", false, acc); err != nil {
				t.Fatal("fresh buffer recovery blocked", err)
			}
			stale := *acc
			stale.OpenID = "previous-channel-identity"
			if _, err := pool.state(ctx, "", acc.ID, "", false, &stale); !errors.Is(err, store.ErrAccountAuthorizationChanged) {
				t.Fatal("stale caller used replacement session", err)
			}
			if _, err := pool.state(ctx, "", acc.ID, "socks5://example.invalid:1080", false); err == nil {
				t.Fatal("session crossed its proxy binding")
			}
			if _, err := pool.state(ctx, "", acc.ID+1, "", false); err == nil {
				t.Fatal("session crossed its account binding")
			}
			if err := pool.Invalidate(ctx, acc.ID, ""); err != nil {
				t.Fatal(err)
			}
			if _, err := pool.state(ctx, "", acc.ID, "", false); err == nil {
				t.Fatal("explicitly invalidated session was restored")
			}
		})
	}
}

func TestApplicationRejectionDoesNotInvalidateProxySession(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "yyb.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	acc, err := db.UpsertAccount(ctx, "synthetic-proxy-refusal", "", nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	proxy := "socks5://example.invalid:1080"
	st := WmpfSession{
		Session:  AppSession{UIN: 1234, SendKey: []byte("send"), RecvKey: []byte("recv")},
		PSK:      pskEntry{PSKType: 1, PreSharedKey: "001122", TicketEntry: "aabbcc", ExpiredAt: time.Now().Add(time.Hour).Unix()},
		TCPProxy: proxy,
	}
	if err := db.PutSession(ctx, acc.ID, nil, st.toBlob(), st.PSK.ExpiredAt, proxy); err != nil {
		t.Fatal(err)
	}
	pool := NewPool(DefaultConfig(), db)
	calls := 0
	_, err = pool.run(ctx, "", acc.ID, proxy, true, func(context.Context, WmpfSession) (map[string]any, error) {
		calls++
		return nil, &ShortlinkRejectedError{Code: -14}
	}, acc)
	if !IsApplicationRequestRejection(err) || calls != 1 || !pool.HasReusableSession(ctx, acc.ID, proxy) {
		t.Fatal("application refusal invalidated proxy session or fell back to direct")
	}
	if _, err := db.ApplyCredentialRefresh(ctx, acc, "", nil, store.StatusSessionOnly); err != nil {
		t.Fatal(err)
	}
	limited, _ := db.GetAccount(ctx, acc.ID)
	if _, err := pool.state(ctx, "must-not-be-used", acc.ID, "", false, limited); !errors.Is(err, ErrCachedSessionUnavailable) {
		t.Fatal("session-only cache miss attempted login")
	}
	if _, err := pool.state(ctx, "", acc.ID, proxy, false, limited); err != nil {
		t.Fatal("session-only mode blocked eligible proxy cache", err)
	}
}
