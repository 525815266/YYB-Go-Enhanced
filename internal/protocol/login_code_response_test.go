package protocol

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func TestLoginCodeResponsePreservesBusinessRejection(t *testing.T) {
	for _, code := range []int32{-12028, -14, 42} {
		for _, field := range []int{1, 2} {
			status := pbLen(field, append(pbVar(1, uint64(int64(code))), pbLen(2, []byte("private-upstream-message"))...))
			for _, withCode := range []bool{false, true} {
				body := append([]byte(nil), status...)
				if withCode {
					body = append(body, pbLen(3, []byte("private-code"))...)
				}
				got, err := parseLoginCodeResponse(pbLen(2, body))
				var rejected *LoginCodeRejectedError
				if len(got) != 0 || !errors.As(err, &rejected) || rejected.Code != code || errors.Is(err, ErrLoginCodeUnavailable) || IsApplicationRequestRejection(err) || strings.Contains(err.Error(), "private") {
					t.Fatalf("lost or unsafe business rejection: %v", err)
				}
				calls, resets := 0, 0
				_, _ = retryLoginCode(context.Background(), func() (map[string]any, error) { calls++; return nil, err }, func() error { resets++; return nil })
				if calls != 1 || resets != 0 {
					t.Fatal("business rejection replayed or invalidated session")
				}
			}
		}
	}
}

func TestLoginCodeResponseSuccessAndMalformed(t *testing.T) {
	code := []byte("synthetic-code")
	for _, status := range [][]byte{nil, pbLen(1, pbVar(1, 0)), append(pbLen(1, pbVar(1, 0)), pbLen(2, pbVar(1, 0))...)} {
		got, err := parseLoginCodeResponse(pbLen(2, append(status, pbLen(3, code)...)))
		if err != nil || string(got) != string(code) {
			t.Fatalf("compatible code response failed: %v", err)
		}
	}
	for _, raw := range [][]byte{nil, pbLen(2, nil), pbLen(2, pbLen(3, nil))} {
		if _, err := parseLoginCodeResponse(raw); !errors.Is(err, ErrLoginCodeUnavailable) {
			t.Fatalf("empty response misclassified: %v", err)
		}
	}
	for _, raw := range [][]byte{
		{0x12, 0xff}, pbVar(2, 1), pbLen(2, pbVar(3, 1)),
		pbLen(2, pbLen(1, pbLen(1, []byte("wrong-wire")))),
		pbLen(2, append(pbLen(3, code), pbLen(3, code)...)),
		append(pbLen(2, pbLen(3, code)), 0xff),
	} {
		if _, err := parseLoginCodeResponse(raw); !errors.Is(err, ErrLoginCodeMalformed) {
			t.Fatalf("malformed response accepted: %v", err)
		}
	}
}
