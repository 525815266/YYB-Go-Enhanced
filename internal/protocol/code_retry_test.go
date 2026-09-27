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
		{"empty code", nil, "", 2},
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

func TestLoginCodeRetryCanceled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := retryLoginCode(ctx, func() (map[string]any, error) { return nil, ErrShortlinkPayload }, func() error { t.Fatal("must not invalidate"); return nil })
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
