package httpapi

import (
	"context"
	"errors"
	"fmt"
	"log"
	"regexp"
	"strings"
	"sync"
	"time"

	"yyb_go/internal/protocol"
	"yyb_go/internal/qr"
	"yyb_go/internal/store"
)

const keepAliveRetryBackoff = 5 * time.Minute

func (a *App) startKeepAlive() {
	if a.cfg.KeepAliveInterval <= 0 {
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	a.keepAliveCancel = cancel
	a.keepAliveDone = make(chan struct{})
	log.Printf("keepalive: enabled interval=%s refresh_ahead=%s", a.cfg.KeepAliveInterval, a.cfg.KeepAliveAhead)
	go func() {
		defer close(a.keepAliveDone)
		a.keepAliveLoop(ctx)
	}()
}

func (a *App) keepAliveLoop(ctx context.Context) {
	a.refreshDueAccounts(ctx)
	ticker := time.NewTicker(a.cfg.KeepAliveInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			a.refreshDueAccounts(ctx)
		}
	}
}

func (a *App) refreshDueAccounts(ctx context.Context) {
	accounts, err := a.db.ListAccounts(ctx)
	if err != nil {
		if ctx.Err() == nil {
			log.Printf("keepalive: list accounts: %v", err)
		}
		return
	}
	const maxWorkers = 4
	sem := make(chan struct{}, maxWorkers)
	var wg sync.WaitGroup
accountsLoop:
	for _, acc := range accounts {
		if ctx.Err() != nil {
			break accountsLoop
		}
		if accountStatus(acc) == "expired" {
			continue
		}
		if a.keepAliveShouldSkip(ctx, acc, time.Now()) {
			continue
		}
		select {
		case sem <- struct{}{}:
		case <-ctx.Done():
			break accountsLoop
		}
		wg.Add(1)
		go func(acc *store.WechatAccount) {
			defer wg.Done()
			defer func() { <-sem }()
			accountCtx, cancel := context.WithTimeout(ctx, keepAliveAccountTimeout(a.cfg.RequestTimeout))
			defer cancel()
			status, refreshed, err := a.refreshAccount(accountCtx, acc, false)
			if err != nil {
				if status == "expired" {
					a.clearKeepAliveRetry(acc.ID)
				} else {
					now := time.Now()
					a.setKeepAliveRetry(acc.ID, now.Add(keepAliveFailureBackoff(protocol.CredentialsFromMap(acc.Credentials), now)))
				}
				if ctx.Err() == nil {
					log.Printf("keepalive: account id=%d refresh failed: %v", acc.ID, err)
				}
				return
			}
			a.clearKeepAliveRetry(acc.ID)
			if refreshed {
				log.Printf("keepalive: account id=%d credentials renewed", acc.ID)
			}
		}(acc)
	}
	wg.Wait()
}

func (a *App) keepAliveShouldSkip(ctx context.Context, acc *store.WechatAccount, now time.Time) bool {
	// Dynamic provider endpoints are short leases. Refreshing credentials from
	// a newly allocated IP cannot preserve the account session and only adds
	// provider/API traffic, so automatic keepalive skips them.
	if setting, err := a.db.AccountProxySettingOrDefault(ctx, acc.ID); err == nil && setting.Mode == "api" {
		return true
	}
	ahead, err := a.accountRefreshAhead(ctx, acc.ID)
	if err != nil {
		return false
	}
	if !credentialsDueForRefresh(protocol.CredentialsFromMap(acc.Credentials), now, ahead) {
		return true
	}
	a.keepAliveRetryMu.Lock()
	defer a.keepAliveRetryMu.Unlock()
	retryAt, ok := a.keepAliveRetryAt[acc.ID]
	if !ok {
		return false
	}
	if !now.Before(retryAt) {
		delete(a.keepAliveRetryAt, acc.ID)
		return false
	}
	return true
}

func (a *App) setKeepAliveRetry(accountID int64, retryAt time.Time) {
	a.keepAliveRetryMu.Lock()
	a.keepAliveRetryAt[accountID] = retryAt
	a.keepAliveRetryMu.Unlock()
}

func (a *App) clearKeepAliveRetry(accountID int64) {
	a.keepAliveRetryMu.Lock()
	delete(a.keepAliveRetryAt, accountID)
	a.keepAliveRetryMu.Unlock()
}

func keepAliveFailureBackoff(creds protocol.LoginBufferCredentials, now time.Time) time.Duration {
	// A five-minute delay would skip the entire official four-minute window.
	remaining := time.Unix(creds.ExpiresAt, 0).Sub(now)
	return min(keepAliveRetryBackoff, max(30*time.Second, remaining/2))
}

func keepAliveAccountTimeout(requestTimeout time.Duration) time.Duration {
	if requestTimeout <= 0 {
		return 30 * time.Second
	}
	if timeout := requestTimeout * 4; timeout > 30*time.Second {
		return timeout
	}
	return 30 * time.Second
}

func (a *App) refreshLiveness(ctx context.Context, acc *store.WechatAccount) (string, error) {
	status, _, err := a.refreshAccount(ctx, acc, false)
	return a.finishLivenessRefresh(ctx, acc, status), err
}

func (a *App) refreshLivenessWithProxy(ctx context.Context, acc *store.WechatAccount, proxyValue string, fallbackDirect bool) (string, error) {
	status, _, err := a.refreshAccountWithPolicy(ctx, acc, true, proxyValue, fallbackDirect, true)
	return a.finishLivenessRefresh(ctx, acc, status), err
}

func (a *App) finishLivenessRefresh(ctx context.Context, acc *store.WechatAccount, status string) string {
	if status == "alive" {
		if avatar := a.resolveAvatar(ctx, acc.OpenID, acc.UserInfo); avatar != "" {
			_ = a.db.SetAccountProfile(ctx, acc.ID, acc.Nickname, &avatar, acc.UserInfo)
		}
	}
	return status
}

func (a *App) refreshAccount(ctx context.Context, acc *store.WechatAccount, force bool) (string, bool, error) {
	return a.refreshAccountWithPolicy(ctx, acc, force, "", false, false)
}

func (a *App) refreshAccountWithPolicy(ctx context.Context, acc *store.WechatAccount, force bool, proxyValue string, fallbackDirect, proxyResolved bool) (string, bool, error) {
	lock := a.refreshLockFor(acc.ID)
	select {
	case lock <- struct{}{}:
	case <-ctx.Done():
		return accountStatus(acc), false, ctx.Err()
	}
	defer func() { <-lock }()

	latest, err := a.db.GetAccount(ctx, acc.ID)
	if err != nil {
		return "unknown", false, err
	}
	if accountStatus(latest) == "expired" {
		return "expired", false, nil
	}
	if latest.Credentials == nil {
		err = fmt.Errorf("credentials are missing")
		if setErr := a.db.SetAccountStatus(ctx, latest.ID, "unknown"); setErr != nil {
			err = fmt.Errorf("%v; update status: %w", err, setErr)
		}
		return "unknown", false, err
	}

	creds := protocol.CredentialsFromMap(latest.Credentials)
	refreshAhead, err := a.accountRefreshAhead(ctx, latest.ID)
	if err != nil {
		return accountStatus(latest), false, err
	}
	if !force && !credentialsDueForRefresh(creds, time.Now(), refreshAhead) {
		return accountStatus(latest), false, nil
	}

	if !proxyResolved {
		proxyValue, fallbackDirect, err = a.resolveAccountProxy(ctx, latest.ID)
		if err != nil {
			return accountStatus(latest), false, fmt.Errorf("resolve account proxy: %w", err)
		}
	}
	refreshed, err := a.refreshCredentialsWithProxy(ctx, creds, proxyValue, fallbackDirect)
	if err != nil {
		status := refreshFailureStatus(accountStatus(latest), creds, err, time.Now())
		applied, setErr := a.db.ApplyCredentialRefresh(ctx, latest, latest.LoginBuffer, latest.Credentials, status)
		if setErr != nil {
			err = fmt.Errorf("%v; update status: %w", err, setErr)
		} else if !applied {
			return "unknown", false, fmt.Errorf("account changed during refresh; result discarded: %w", err)
		} else if status == "expired" {
			a.invalidateProxyLease(latest.ID)
			a.clearKeepAliveRetry(latest.ID)
		}
		return status, false, err
	}
	// Save a rotated token before any subsequent upstream request. A cancelled
	// browser request must not discard a refresh already accepted by the server.
	saveCtx, cancelSave := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	applied, err := a.db.ApplyCredentialRefresh(saveCtx, latest, latest.LoginBuffer, refreshed.ToMap(), "alive")
	if err != nil || !applied {
		cancelSave()
		if err == nil {
			err = errors.New("account changed during refresh; result discarded")
		}
		return "unknown", false, err
	}
	if !force {
		cancelSave()
		// Official token keepalive does not obtain a new ILink login buffer.
		// Buffer recovery happens on demand, when a protocol call needs it.
		return "alive", true, nil
	}
	saved, err := a.db.GetAccount(saveCtx, latest.ID)
	cancelSave()
	if err != nil {
		return "alive", true, err
	}
	if saved.OpenID != latest.OpenID || protocol.CredentialsFromMap(saved.Credentials) != refreshed {
		return "unknown", true, errors.New("account changed after refresh; buffer request skipped")
	}
	buffer, err := a.fetchLoginBufferWithProxy(ctx, refreshed, proxyValue, fallbackDirect)
	if err != nil {
		return "alive", true, fmt.Errorf("credentials renewed; fetch login buffer: %w", err)
	}
	saveCtx, cancelSave = context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancelSave()
	applied, err = a.db.ApplyCredentialRefresh(saveCtx, saved, buffer, saved.Credentials, "alive")
	if err != nil {
		return "alive", true, err
	}
	if !applied {
		return "unknown", true, errors.New("account changed while fetching login buffer; result discarded")
	}
	return "alive", true, nil
}

// setAccountStatus applies the lifecycle side effects for an account status
// transition. Expiration is terminal for the current credentials: remove
// cached sessions and leases so the keepalive loop and scripts stop using it.
func (a *App) setAccountStatus(ctx context.Context, accountID int64, status string) error {
	if err := a.db.SetAccountStatus(ctx, accountID, status); err != nil {
		return err
	}
	if status == "expired" {
		_ = a.db.InvalidateAccountSessions(ctx, accountID)
		a.invalidateProxyLease(accountID)
		a.clearKeepAliveRetry(accountID)
	}
	return nil
}

func (a *App) refreshLockFor(accountID int64) chan struct{} {
	a.refreshLocksMu.Lock()
	defer a.refreshLocksMu.Unlock()
	if lock := a.refreshLocks[accountID]; lock != nil {
		return lock
	}
	lock := make(chan struct{}, 1)
	a.refreshLocks[accountID] = lock
	return lock
}

func refreshFailureStatus(current string, creds protocol.LoginBufferCredentials, err error, now time.Time) string {
	if definitiveCredentialFailure(err) {
		return "expired"
	}
	if creds.ExpiresAt > now.Unix() {
		return current
	}
	return "unknown"
}

func definitiveCredentialFailure(err error) bool {
	if errors.Is(err, protocol.ErrMissingRefreshToken) {
		return true
	}
	switch rejected := err.(type) {
	case *protocol.RefreshRejectedError:
		return definitiveRefreshMessage(rejected.Code, rejected.Message)
	case *protocol.LoginBufferRejectedError:
		return definitiveRefreshMessage(rejected.Code, rejected.Message)
	}
	// A refresh rejection and its check-login fallback are separate causes.
	// Inspect both: errors.As alone stops at the first ambiguous rejection.
	switch wrapped := err.(type) {
	case interface{ Unwrap() []error }:
		for _, cause := range wrapped.Unwrap() {
			if definitiveCredentialFailure(cause) {
				return true
			}
		}
	case interface{ Unwrap() error }:
		return definitiveCredentialFailure(wrapped.Unwrap())
	}
	// Transport errors may contain a proxy provider's "token expired" or an
	// HTTP/JSON error body. They are not evidence that WeChat revoked a session.
	return false
}

var rejectedRefreshCode = regexp.MustCompile(`(?i)(?:^|(?:\berror\b|\bcode\b)\s*[:=]?\s*|\[)(?:40030|42002|42007)(?:$|[\s\]|,;])`)

func definitiveRefreshMessage(code int, raw string) bool {
	// Official check-login sometimes returns only "error 42007 | 42007"
	// without spelling out refresh_token. These known WeChat business codes
	// are conclusive, but only inside a typed credential rejection, never
	// in an HTTP/proxy response or transport error.
	if code == 40030 || code == 42002 || code == 42007 || rejectedRefreshCode.MatchString(raw) {
		return true
	}
	message := strings.ToLower(strings.TrimSpace(raw))
	message = strings.NewReplacer("_", " ", "-", " ", "refreshtoken", "refresh token").Replace(message)
	refreshToken := strings.Contains(message, "refresh token") || strings.Contains(message, "刷新令牌")
	if (code == 40188 || strings.Contains(message, "40188")) && strings.Contains(message, "invalid scope") {
		return true
	}
	invalid := strings.Contains(message, "invalid") || strings.Contains(message, "expired") ||
		strings.Contains(message, "expire") || strings.Contains(message, "无效") ||
		strings.Contains(message, "过期") || strings.Contains(message, "失效")
	relogin := strings.Contains(message, "relogin") || strings.Contains(message, "re login") ||
		strings.Contains(message, "重新登录") || strings.Contains(message, "重新授权")
	// Access-token expiry alone is recoverable. Ambiguous "token expired"
	// responses also remain retryable until a refresh rejection is explicit.
	return relogin || (invalid && refreshToken)
}

func (a *App) accountRefreshAhead(ctx context.Context, accountID int64) (time.Duration, error) {
	setting, err := a.db.AccountProxySettingOrDefault(ctx, accountID)
	if err != nil {
		return 0, fmt.Errorf("read account proxy setting: %w", err)
	}
	if setting.Mode == "direct" {
		return a.cfg.KeepAliveAhead, nil
	}
	return time.Duration(setting.RefreshAheadSeconds) * time.Second, nil
}

func (a *App) refreshCredentialsWithProxy(ctx context.Context, creds protocol.LoginBufferCredentials, proxyValue string, fallbackDirect bool) (protocol.LoginBufferCredentials, error) {
	if proxyValue == "" {
		return a.refreshCredentials(ctx, creds)
	}
	client, err := qr.NewClientWithProxy(a.cfg.RequestTimeout, proxyValue, fallbackDirect)
	if err != nil {
		return protocol.LoginBufferCredentials{}, err
	}
	return client.LoginBuffers().RefreshCredentials(ctx, creds)
}

func (a *App) fetchUserInfoWithProxy(ctx context.Context, creds protocol.LoginBufferCredentials, proxyValue string, fallbackDirect bool) (map[string]any, error) {
	if proxyValue == "" {
		return a.fetchUserInfo(ctx, creds)
	}
	client, err := qr.NewClientWithProxy(a.cfg.RequestTimeout, proxyValue, fallbackDirect)
	if err != nil {
		return nil, err
	}
	return client.LoginBuffers().FetchUserInfo(ctx, creds)
}

func credentialsDueForRefresh(creds protocol.LoginBufferCredentials, now time.Time, ahead time.Duration) bool {
	return creds.ExpiresAt <= 0 || now.Add(ahead).Unix() >= creds.ExpiresAt
}

func accountStatus(acc *store.WechatAccount) string {
	if acc.Status == nil || *acc.Status == "" {
		return "unknown"
	}
	return *acc.Status
}
