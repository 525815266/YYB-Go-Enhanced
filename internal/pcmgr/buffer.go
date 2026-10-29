// Package pcmgr describes the PC Manager account service, independently of YYB OAuth.
package pcmgr

import (
	"fmt"

	"google.golang.org/protobuf/encoding/protowire"
)

const HostAppID = "wx5cd60c5d4817a188"

// BufferRejectedError contains only the business code, never the upstream text.
type BufferRejectedError struct{ Code int32 }

func (e *BufferRejectedError) Error() string {
	return fmt.Sprintf("PCMgr GetLoginBuffer rejected: code=%d", e.Code)
}

// ParseBufferResponse decodes the native command 20138 business response.
// Proto3 omits result=0; an empty/malformed response still cannot succeed.
func ParseBufferResponse(raw []byte) (string, error) {
	if len(raw) == 0 || len(raw) > 2_000_000 {
		return "", fmt.Errorf("invalid PCMgr response size")
	}
	var code int32
	var buffer string
	seen := map[protowire.Number]bool{}
	for len(raw) > 0 {
		num, typ, n := protowire.ConsumeTag(raw)
		if n < 0 {
			return "", fmt.Errorf("malformed PCMgr response tag")
		}
		raw = raw[n:]
		if num <= 5 {
			if seen[num] || (num == 1 && typ != protowire.VarintType) || (num != 1 && typ != protowire.BytesType) {
				return "", fmt.Errorf("invalid PCMgr response field")
			}
			seen[num] = true
		}
		if num == 1 {
			v, n := protowire.ConsumeVarint(raw)
			if n < 0 {
				return "", fmt.Errorf("malformed PCMgr result")
			}
			if v != uint64(int64(int32(v))) {
				return "", fmt.Errorf("PCMgr result outside int32 range")
			}
			code, raw = int32(v), raw[n:]
		} else if num == 3 {
			v, n := protowire.ConsumeBytes(raw)
			if n < 0 {
				return "", fmt.Errorf("malformed PCMgr buffer")
			}
			buffer, raw = string(v), raw[n:]
		} else {
			n := protowire.ConsumeFieldValue(num, typ, raw)
			if n < 0 {
				return "", fmt.Errorf("malformed PCMgr response value")
			}
			raw = raw[n:]
		}
	}
	if code != 0 {
		return "", &BufferRejectedError{Code: code}
	}
	if buffer == "" {
		return "", fmt.Errorf("PCMgr response has no login buffer")
	}
	return buffer, nil
}
