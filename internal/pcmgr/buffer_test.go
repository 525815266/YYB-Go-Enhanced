package pcmgr

import (
	"errors"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protowire"
)

func TestBufferResponseBoundaries(t *testing.T) {
	buffer := protowire.AppendTag(nil, 3, protowire.BytesType)
	buffer = protowire.AppendString(buffer, "synthetic-ticket")
	rejected := protowire.AppendTag(nil, 1, protowire.VarintType)
	rejected = protowire.AppendVarint(rejected, 7)
	overflow := protowire.AppendTag(nil, 1, protowire.VarintType)
	overflow = protowire.AppendVarint(overflow, 1<<32)
	for _, tc := range []struct {
		name string
		raw  []byte
		ok   bool
	}{
		{"proto3_omitted_zero", buffer, true},
		{"explicit_zero", append([]byte{8, 0}, buffer...), true},
		{"empty", nil, false},
		{"missing_buffer", []byte{8, 0}, false},
		{"truncated", buffer[:len(buffer)-1], false},
		{"wrong_wire", []byte{24, 1}, false},
		{"duplicate_buffer", append(append([]byte{}, buffer...), buffer...), false},
		{"result_wrong_wire", []byte{10, 0}, false},
		{"business_rejection", append(rejected, buffer...), false},
		{"result_must_not_wrap_to_success", append(overflow, buffer...), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseBufferResponse(tc.raw)
			if (err == nil) != tc.ok || (tc.ok && got != "synthetic-ticket") {
				t.Fatalf("unexpected parse result: success=%v", err == nil)
			}
			if err != nil && strings.Contains(err.Error(), "synthetic-ticket") {
				t.Fatal("error disclosed ticket")
			}
			if tc.name == "business_rejection" {
				var rejected *BufferRejectedError
				if !errors.As(err, &rejected) || rejected.Code != 7 {
					t.Fatal("lost business rejection")
				}
			}
		})
	}
}
