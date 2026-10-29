//go:build protocolaudit

package protocol

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"google.golang.org/protobuf/encoding/protowire"
	"yyb_go/internal/pcmgr"
)

// Local lab profiles share only the ILink transport implementation. Neither
// profile opens a production database or falls back to the other provider.
func labHost(provider string) ([]byte, error) {
	switch provider {
	case "yyb":
		return hostAppIDDefault, nil
	case "pcmgr":
		return []byte(pcmgr.HostAppID), nil
	default:
		return nil, fmt.Errorf("unknown lab provider")
	}
}

func labBuffer(provider string, input []byte) (string, error) {
	host, err := labHost(provider)
	if err != nil {
		return "", err
	}
	buffer := string(bytes.TrimSpace(input))
	if provider == "pcmgr" {
		buffer, err = pcmgr.ParseBufferResponse(input)
		if err != nil {
			return "", err
		}
	}
	raw, err := base64.StdEncoding.Strict().DecodeString(buffer)
	if err != nil || len(raw) == 0 || len(raw) > 65536 {
		return "", fmt.Errorf("invalid lab ticket encoding")
	}
	fields := map[protowire.Number][]byte{}
	seen := map[protowire.Number]bool{}
	for len(raw) > 0 {
		num, typ, n := protowire.ConsumeTag(raw)
		if n < 0 || typ != protowire.BytesType || seen[num] {
			return "", fmt.Errorf("invalid lab ticket field")
		}
		v, m := protowire.ConsumeBytes(raw[n:])
		if m < 0 {
			return "", fmt.Errorf("truncated lab ticket")
		}
		fields[num], raw = v, raw[n+m:]
		seen[num] = true
	}
	if len(fields[1]) == 0 || len(fields[2]) == 0 || !bytes.Equal(fields[3], host) {
		return "", fmt.Errorf("lab ticket belongs to a different provider or is incomplete")
	}
	return buffer, nil
}

type labSavedSession struct {
	Schema   int            `json:"schema"`
	Provider string         `json:"provider"`
	Session  map[string]any `json:"session_blob"`
}

func labReadSession(provider string, saved []byte) (WmpfSession, error) {
	host, err := labHost(provider)
	if err != nil {
		return WmpfSession{}, err
	}
	var state labSavedSession
	if json.Unmarshal(saved, &state) != nil || state.Schema != 1 || state.Provider != provider {
		return WmpfSession{}, fmt.Errorf("lab session provider mismatch or invalid schema")
	}
	st, err := sessionFromBlob(state.Session)
	key, keyErr := hex.DecodeString(st.PSK.PreSharedKey)
	ticket, ticketErr := hex.DecodeString(st.PSK.TicketEntry)
	if err != nil || !bytes.Equal(st.Session.HostAppID, host) || st.TCPProxy != "" ||
		st.PSK.ExpiredAt <= time.Now().Unix() || st.PSK.PSKType != 1 ||
		keyErr != nil || len(key) == 0 || ticketErr != nil || len(ticket) == 0 {
		return WmpfSession{}, fmt.Errorf("lab session invalid, expired or belongs to another provider")
	}
	return st, nil
}

// LabResult intentionally exposes no code, identity, credentials or raw errors.
type LabResult struct {
	Provider     string `json:"provider"`
	Success      bool   `json:"success"`
	CodeLength   int    `json:"code_length"`
	NewSession   bool   `json:"new_session"`
	HostAppID    string `json:"host_appid"`
	PSKExpiresAt int64  `json:"psk_expires_at"`
}

// ProbeLocalCode requires either a fresh provider ticket or a previously saved
// provider session. A failed cached request never triggers a fresh login.
func ProbeLocalCode(ctx context.Context, provider string, input, saved []byte) (LabResult, []byte, error) {
	result := LabResult{Provider: provider, NewSession: len(input) > 0}
	if (len(input) == 0) == (len(saved) == 0) {
		return result, nil, fmt.Errorf("provide exactly one lab ticket or saved session")
	}
	pool := NewPool(DefaultConfig(), nil)
	var st WmpfSession
	var err error
	if len(input) > 0 {
		var buffer string
		buffer, err = labBuffer(provider, input)
		if err == nil {
			st, err = pool.loginAndSession(ctx, buffer, "", false)
		}
	} else {
		st, err = labReadSession(provider, saved)
	}
	if err != nil {
		return result, nil, err
	}
	result.HostAppID, result.PSKExpiresAt = string(st.Session.HostAppID), st.PSK.ExpiredAt
	plain := buildJSAPIPlaintext(st.Session.UIN, "wx3bb18aff4f2fe45f", jsLoginURL, jsLoginCmdID, nil, st.Session.HostAppID, nil)
	envelope, err := buildTransferPacket(st.Session, plain)
	if err != nil {
		return result, nil, err
	}
	code, _, err := pool.sendEnvelope(ctx, st, envelope)
	result.CodeLength, result.Success = len(code), err == nil && len(code) > 0
	if err != nil {
		return result, nil, err
	}
	if len(code) == 0 {
		return result, nil, fmt.Errorf("lab received no code")
	}
	raw, err := json.Marshal(labSavedSession{Schema: 1, Provider: provider, Session: st.toBlob()})
	return result, raw, err
}
