//go:build protocolaudit || issue75probe

package protocol

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"testing"
)

// Private session snapshots must preserve the same identity as database reads.
// Keep this test-only helper available to both opt-in audit build tags.
func decodeAuditSessionJSON(raw []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if decoder.Decode(new(any)) != io.EOF {
		return fmt.Errorf("trailing session data")
	}
	return nil
}

func TestAuditSessionJSONPreservesUINPrecision(t *testing.T) {
	fresh := syntheticNativeSession()
	fresh.Session.UIN = 1<<60 + 12345
	for _, wrapper := range []string{"", "session_blob", "session"} {
		t.Run(wrapper, func(t *testing.T) {
			blob := fresh.toBlob()
			if wrapper != "" {
				blob = map[string]any{wrapper: blob}
			}
			raw, err := json.Marshal(blob)
			if err != nil {
				t.Fatal(err)
			}
			var saved map[string]any
			if err := decodeAuditSessionJSON(raw, &saved); err != nil {
				t.Fatal(err)
			}
			if wrapper != "" {
				saved = saved[wrapper].(map[string]any)
			}
			got, err := sessionFromBlob(saved)
			if err != nil || got.Session.UIN != fresh.Session.UIN {
				t.Fatal("audit snapshot changed the session identity")
			}
			for _, suffix := range []string{"{}", "trailing"} {
				if decodeAuditSessionJSON(append(append([]byte(nil), raw...), []byte(suffix)...), &saved) == nil {
					t.Fatal("audit snapshot accepted trailing session data")
				}
			}
		})
	}
}
