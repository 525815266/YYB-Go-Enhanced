package httpapi

import (
	"context"
	"errors"
	"strings"
	"time"

	"yyb_go/internal/protocol"
)

const (
	loginBufferRetryWindow = 8 * time.Second
	loginBufferRetryEvery  = 500 * time.Millisecond
)

// getLoginBufferWithRetry absorbs the short delay between WeChat reporting an
// authorized QR code and the login-buffer endpoint becoming readable. The
// account link is not consumed until this succeeds, so a transient 409 cannot
// leave the user with a half-completed scan that needs a manual retry.
func (a *App) getLoginBufferWithRetry(ctx context.Context, login *qrLoginSession) (protocol.LoginBufferResult, error) {
	result, err := login.Client.GetLoginBuffer(ctx, login.Session)
	if err == nil || !retryLoginBufferError(err) {
		return result, err
	}
	retryCtx, cancel := context.WithTimeout(ctx, loginBufferRetryWindow)
	defer cancel()
	ticker := time.NewTicker(loginBufferRetryEvery)
	defer ticker.Stop()
	for {
		select {
		case <-retryCtx.Done():
			return result, err
		case <-ticker.C:
			result, err = login.Client.GetLoginBuffer(retryCtx, login.Session)
			if err == nil || !retryLoginBufferError(err) {
				return result, err
			}
		}
	}
}

func retryLoginBufferError(err error) bool {
	if err == nil || errors.Is(err, context.Canceled) {
		return false
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "not authorized yet") ||
		strings.Contains(message, "buffer not ready") ||
		strings.Contains(message, "timeout")
}
