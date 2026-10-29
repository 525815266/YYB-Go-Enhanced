//go:build protocolaudit

package protocol

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"testing"
	"time"

	"yyb_go/internal/pcmgr"
)

func TestLabRejectsCrossProviderTicketsBeforeNetwork(t *testing.T) {
	for _, provider := range []string{"yyb", "pcmgr"} {
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
