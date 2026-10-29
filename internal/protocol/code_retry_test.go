package protocol

import (
	"context"
	"errors"
	"fmt"
	"testing"
)

func TestLoginCodeRetry(t *testing.T) {
	for _, tc := range []struct {
		name  string
		first error
		code  string
		want  int
	}{
		{"stale session", fmt.Errorf("targets: %w", ErrShortlinkPayload), "", 2},
		{"empty code", nil, "", 1},
		{"success", nil, "ok", 1},
		{"network", errors.New("timeout"), "", 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls, resets := 0, 0
			_, _ = retryLoginCode(context.Background(), func() (map[string]any, error) {
				calls++
				return map[string]any{"code": tc.code}, tc.first
			}, func() error { resets++; return nil })
			if calls != tc.want || resets != tc.want-1 {
				t.Fatalf("calls=%d resets=%d", calls, resets)
			}
		})
	}
}

func TestEmptyLoginCodePreservesSessionForOtherApps(t *testing.T) {
	for _, empty := range []map[string]any{nil, {}, {"code": nil}, {"code": ""}} {
		sessionHealthy := true
		reset := func() error { sessionHealthy = false; return nil }
		_, err := retryLoginCode(context.Background(), func() (map[string]any, error) {
			return empty, nil
		}, reset)
		if !errors.Is(err, ErrLoginCodeUnavailable) || !sessionHealthy {
			t.Fatalf("application failure invalidated shared session: healthy=%v error=%v", sessionHealthy, err)
		}
		result, err := retryLoginCode(context.Background(), func() (map[string]any, error) {
			if !sessionHealthy {
				t.Fatal("other application lost its usable session")
			}
			return map[string]any{"code": "control-app-code"}, nil
		}, reset)
		if err != nil || result["code"] != "control-app-code" {
			t.Fatal("control application failed")
		}
	}
}

func TestLoginCodeRetryCanceled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := retryLoginCode(ctx, func() (map[string]any, error) { return nil, ErrShortlinkPayload }, func() error { t.Fatal("must not invalidate"); return nil })
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
