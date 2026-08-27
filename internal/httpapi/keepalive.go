package httpapi

import (
	"context"
	"errors"
	"fmt"
	"log"
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
			_, refreshed, err := a.refreshAccount(accountCtx, acc, false)
			if err != nil {
				a.setKeepAliveRetry(acc.ID, time.Now().Add(keepAliveRetryBackoff))
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
	lock.Lock()
	defer lock.Unlock()

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
	result, err := a.refreshLoginBufferWithProxy(ctx, creds, proxyValue, fallbackDirect)
	if err != nil {
		status := refreshFailureStatus(accountStatus(latest), creds, err, time.Now())
		if setErr := a.db.SetAccountStatus(ctx, latest.ID, status); setErr != nil {
			err = fmt.Errorf("%v; update status: %w", err, setErr)
		}
		return status, false, err
	}
	if err = a.db.SetAccountCredentialStatus(ctx, latest.ID, result.LoginBuffer, result.Credentials.ToMap(), "alive"); err != nil {
		return "expired", false, err
	}
	return "alive", true, nil
}

func (a *App) refreshLockFor(accountID int64) *sync.Mutex {
	a.refreshLocksMu.Lock()
	defer a.refreshLocksMu.Unlock()
	if lock := a.refreshLocks[accountID]; lock != nil {
		return lock
	}
	lock := &sync.Mutex{}
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
	var rejected *protocol.RefreshRejectedError
	if errors.As(err, &rejected) {
		return definitiveRefreshMessage(rejected.Message)
	}
	return definitiveRefreshMessage(err.Error())
}

func definitiveRefreshMessage(raw string) bool {
	message := strings.ToLower(strings.TrimSpace(raw))
	if strings.Contains(message, "42007") && strings.Contains(message, "refresh_token") {
		return true
	}
	if strings.Contains(message, "40188") && strings.Contains(message, "invalid scope") {
		return true
	}
	invalid := strings.Contains(message, "invalid") || strings.Contains(message, "expired") ||
		strings.Contains(message, "expire") || strings.Contains(message, "无效") ||
		strings.Contains(message, "过期") || strings.Contains(message, "失效")
	token := strings.Contains(message, "token") || strings.Contains(message, "登录") ||
		strings.Contains(message, "凭证") || strings.Contains(message, "授权")
	relogin := strings.Contains(message, "relogin") || strings.Contains(message, "re-login") ||
		strings.Contains(message, "重新登录") || strings.Contains(message, "重新授权")
	return relogin || (invalid && token)
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

func (a *App) refreshLoginBufferWithProxy(ctx context.Context, creds protocol.LoginBufferCredentials, proxyValue string, fallbackDirect bool) (protocol.LoginBufferResult, error) {
	if proxyValue == "" {
		return a.refreshLoginBuffer(ctx, creds)
	}
	client, err := qr.NewClientWithProxy(a.cfg.RequestTimeout, proxyValue, fallbackDirect)
	if err != nil {
		return protocol.LoginBufferResult{}, err
	}
	return client.RefreshLoginBuffer(ctx, creds)
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
