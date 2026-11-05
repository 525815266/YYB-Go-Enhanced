//go:build protocolaudit

package protocol

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"yyb_go/internal/pcmgr"
)

func TestLabRejectsCrossProviderTicketsBeforeNetwork(t *testing.T) {
	for _, provider := range []string{"yyb", "pcmgr", "gameassist", "ldplayer"} {
		host, _ := labHost(provider)
		makeInput := func(ticket []byte) []byte {
			buffer := []byte(base64.StdEncoding.EncodeToString(ticket))
			if provider == "pcmgr" {
				return pbLen(3, buffer)
			}
			return buffer
		}
		ticket := append(pbLen(1, []byte("synthetic-ticket")), pbLen(2, []byte("synthetic-device"))...)
		ticket = append(ticket, pbLen(3, host)...)
		if _, err := labBuffer(provider, makeInput(ticket)); err != nil {
			t.Fatal("own provider rejected")
		}
		otherHost := hostAppIDDefault
		if provider == "yyb" {
			otherHost = []byte(pcmgr.HostAppID)
		}
		cross := append(pbLen(1, []byte("synthetic-ticket")), pbLen(2, []byte("synthetic-device"))...)
		cross = append(cross, pbLen(3, otherHost)...)
		for _, input := range [][]byte{makeInput(cross), makeInput(ticket[:len(ticket)-1]), makeInput(append(ticket, pbLen(3, host)...))} {
			if _, _, err := ProbeLocalCode(context.Background(), provider, input, nil); err == nil {
				t.Fatal("cross-provider or malformed ticket accepted")
			}
		}
	}
}

func TestLabNativeRotationSavesBeforeBusinessProbe(t *testing.T) {
	for _, outcome := range []string{"success", "business_failed", "transport_incomplete", "save_failed", "transport_and_save_failed", "rejected"} {
		t.Run(outcome, func(t *testing.T) {
			st := syntheticNativeSession()
			st.Session.HostAppID, _ = labHost("ldplayer")
			// Native recovery must remain testable after access PSK expiry.
			st.PSK.ExpiredAt = time.Now().Add(-time.Minute).Unix()
			raw, _ := json.Marshal(labSavedSession{Schema: 1, Provider: "ldplayer", Session: st.toBlob()})
			consumed, persisted, probed := false, false, false
			failure := errors.New("synthetic failure")
			saveFailure := errors.New("synthetic save failure")
			exchange := func(ctx context.Context, old WmpfSession, consume func() error) (WmpfSession, error) {
				if err := consume(); err != nil {
					return WmpfSession{}, err
				}
				if outcome == "rejected" {
					return WmpfSession{}, failure
				}
				old.AutoAuth.Key = []byte("synthetic-rotated-key")
				if outcome == "transport_incomplete" || outcome == "transport_and_save_failed" {
					return old, failure
				}
				return old, nil
			}
			result, err := probeLocalNativeAutoAuth(context.Background(), "ldplayer", raw,
				func() error { consumed = true; return nil }, func(saved []byte) error {
					if !consumed {
						t.Fatal("state saved before intent")
					}
					if outcome == "save_failed" || outcome == "transport_and_save_failed" {
						return saveFailure
					}
					next, err := labReadSavedSession("ldplayer", saved, false)
					if err != nil || string(next.AutoAuth.Key) != "synthetic-rotated-key" {
						t.Fatal("replacement state was not saved")
					}
					persisted = true
					return nil
				}, exchange, func(ctx context.Context, next WmpfSession) (int, error) {
					if !persisted {
						t.Fatal("business request preceded durable rotation")
					}
					probed = true
					if outcome == "business_failed" {
						return 0, failure
					}
					return 32, nil
				})
			if !consumed || (err == nil) != (outcome == "success") || result.Success != (outcome == "success") {
				t.Fatal("incorrect result or missing intent")
			}
			if persisted != (outcome != "save_failed" && outcome != "transport_and_save_failed" && outcome != "rejected") || probed != (outcome == "success" || outcome == "business_failed") {
				t.Fatal("incorrect persistence or business request order")
			}
			if outcome == "transport_and_save_failed" && (!errors.Is(err, failure) || !errors.Is(err, saveFailure)) {
				t.Fatal("persistence failure discarded the first exchange failure")
			}
		})
	}
}

func TestLabRejectsCrossProviderSessionsBeforeNetwork(t *testing.T) {
	st := WmpfSession{Session: AppSession{UIN: 1, SendKey: []byte("send"), RecvKey: []byte("recv"), HostAppID: []byte(pcmgr.HostAppID)},
		PSK: pskEntry{PSKType: 1, PreSharedKey: "001122", TicketEntry: "112233", ExpiredAt: time.Now().Add(time.Hour).Unix()}}
	encode := func(provider string) []byte {
		raw, _ := json.Marshal(labSavedSession{Schema: 1, Provider: provider, Session: st.toBlob()})
		return raw
	}
	if _, err := labReadSession("pcmgr", encode("pcmgr")); err != nil {
		t.Fatal("own session rejected")
	}
	for _, saved := range [][]byte{encode("pcmgr"), encode("yyb")} {
		if _, _, err := ProbeLocalCode(context.Background(), "yyb", nil, saved); err == nil {
			t.Fatal("provider label or host mismatch accepted")
		}
	}
	st.PSK.ExpiredAt = time.Now().Add(-time.Minute).Unix()
	if _, err := labReadSession("pcmgr", encode("pcmgr")); err == nil {
		t.Fatal("expired session accepted")
	}
}
