package httpapi

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	httpSwagger "github.com/swaggo/http-swagger/v2"

	"yyb_go/internal/auth"
	"yyb_go/internal/protocol"
	"yyb_go/internal/proxysource"
	"yyb_go/internal/qr"
	"yyb_go/internal/store"
)

type Config struct {
	ResourceRoot      string
	DBFilename        string
	TCPProxy          string
	SessionTTL        time.Duration
	RequestTimeout    time.Duration
	AvatarTimeout     time.Duration
	ScanTimeout       time.Duration
	QRSessionTTL      time.Duration
	KeepAliveInterval time.Duration
	KeepAliveAhead    time.Duration
	QingLongType      string
	QingLongURL       string
	QingLongClientID  string
	QingLongSecret    string
	QingLongServer    string
	QingLongRepo      string
	AuthDriver        string
	AuthDSN           string
	AuthMySQLDSN      string
	IntegrationToken  string
	AdminUser         string
	AdminPassword     string
	CookieSecure      bool
	EnablePCLogin     bool
	SessionDuration   time.Duration
}

type App struct {
	cfg                Config
	resources          resources
	db                 *store.DB
	pool               *protocol.Pool
	qr                 *qr.Client
	refreshLoginBuffer func(context.Context, protocol.LoginBufferCredentials) (protocol.LoginBufferResult, error)
	exchangeAuthCode   func(context.Context, string) (protocol.LoginBufferResult, error)
	fetchUserInfo      func(context.Context, protocol.LoginBufferCredentials) (map[string]any, error)
	qinglong           *qingLongClient
	auth               *auth.Store

	mu                sync.Mutex
	qrSessions        map[string]*qrLoginSession
	quickSessions     map[string]quickLoginSession
	refreshLocksMu    sync.Mutex
	refreshLocks      map[int64]*sync.Mutex
	loginMu           sync.Mutex
	loginAttempts     map[string]loginAttempt
	proxyMu           sync.Mutex
	proxyLeases       map[int64]accountProxyLease
	proxyLeaseLocksMu sync.Mutex
	proxyLeaseLocks   map[int64]*sync.Mutex
	keepAliveRetryMu  sync.Mutex
	keepAliveRetryAt  map[int64]time.Time

	keepAliveCancel context.CancelFunc
	keepAliveDone   chan struct{}
}

var swaggerDocsHandler = httpSwagger.Handler(
	httpSwagger.URL("/openapi.json"),
	httpSwagger.DocExpansion("list"),
	httpSwagger.DeepLinking(true),
	httpSwagger.DefaultModelsExpandDepth(httpSwagger.ShowModel),
)

func NewApp(cfg Config) (*App, error) {
	if cfg.ResourceRoot == "" {
		cfg.ResourceRoot = filepath.Join(".", "resource")
	}
	if cfg.DBFilename == "" {
		cfg.DBFilename = DefaultDBFilename
	}
	if cfg.RequestTimeout == 0 {
		cfg.RequestTimeout = 8 * time.Second
	}
	if cfg.AvatarTimeout == 0 {
		cfg.AvatarTimeout = 10 * time.Second
	}
	if cfg.SessionTTL == 0 {
		cfg.SessionTTL = 30 * time.Minute
	}
	if cfg.QRSessionTTL == 0 {
		cfg.QRSessionTTL = 5 * time.Minute
	}
	if cfg.KeepAliveInterval > 0 && cfg.KeepAliveAhead <= 0 {
		cfg.KeepAliveAhead = 45 * time.Minute
	}
	if cfg.QingLongServer == "" {
		cfg.QingLongServer = "yyb-go:8000"
	}
	if cfg.QingLongRepo == "" {
		cfg.QingLongRepo = "SuperNaiBA_YYB-GO-Script,525815266_YYB-Go-Enhanced/scripts"
	}
	if cfg.SessionDuration <= 0 {
		cfg.SessionDuration = 7 * 24 * time.Hour
	}
	res, err := ensureResources(cfg.ResourceRoot)
	if err != nil {
		return nil, err
	}
	dbPath, err := prepareDBPath(res.DB, cfg.DBFilename)
	if err != nil {
		return nil, err
	}
	db, err := store.Open(dbPath)
	if err != nil {
		return nil, err
	}
	loadSetting := func(key, fallback string) string {
		value, settingErr := db.GetSetting(context.Background(), key)
		if settingErr == nil {
			return value
		}
		return fallback
	}
	cfg.QingLongType = normalizePanelType(loadSetting(qingLongTypeSetting, cfg.QingLongType))
	loadPanelSetting := func(panelType, key, fallback string) string {
		value, settingErr := db.GetSetting(context.Background(), panelSettingKey(panelType, key))
		if settingErr == nil {
			return value
		}
		return loadSetting(key, fallback)
	}
	cfg.QingLongURL = loadPanelSetting(cfg.QingLongType, qingLongURLSetting, cfg.QingLongURL)
	cfg.QingLongClientID = loadPanelSetting(cfg.QingLongType, qingLongClientIDSetting, cfg.QingLongClientID)
	cfg.QingLongSecret = loadPanelSetting(cfg.QingLongType, qingLongSecretSetting, cfg.QingLongSecret)
	if normalizePanelType(cfg.QingLongType) == PanelTypeArcadia {
		cfg.QingLongClientID = "api-token"
	}
	poolCfg := protocol.DefaultConfig()
	poolCfg.SessionTTL = cfg.SessionTTL
	poolCfg.ShortlinkTimeout = cfg.RequestTimeout
	pool := protocol.NewPool(poolCfg, db)
	qrClient := qr.NewClient(cfg.RequestTimeout)
	app := &App{
		cfg:                cfg,
		resources:          res,
		db:                 db,
		pool:               pool,
		qr:                 qrClient,
		refreshLoginBuffer: qrClient.RefreshLoginBuffer,
		exchangeAuthCode:   qrClient.GetLoginBufferFromCode,
		fetchUserInfo:      qrClient.LoginBuffers().FetchUserInfo,
		qinglong:           newQingLongClient(cfg.QingLongType, cfg.QingLongURL, cfg.QingLongClientID, cfg.QingLongSecret, cfg.RequestTimeout),
		qrSessions:         map[string]*qrLoginSession{},
		quickSessions:      map[string]quickLoginSession{},
		loginAttempts:      map[string]loginAttempt{},
		refreshLocks:       map[int64]*sync.Mutex{},
		proxyLeases:        map[int64]accountProxyLease{},
		proxyLeaseLocks:    map[int64]*sync.Mutex{},
		keepAliveRetryAt:   map[int64]time.Time{},
	}
	authDriver := strings.ToLower(strings.TrimSpace(cfg.AuthDriver))
	authDSN := strings.TrimSpace(cfg.AuthDSN)
	if authDriver == "" && cfg.AuthMySQLDSN != "" {
		authDriver = "mysql"
		authDSN = cfg.AuthMySQLDSN
	}
	if authDriver != "" && authDriver != "none" {
		if authDriver == "mysql" && authDSN == "" {
			authDSN = cfg.AuthMySQLDSN
		}
		if authDriver == "sqlite" && authDSN == "" {
			authDSN = filepath.Join(res.DB, "auth.db")
		}
		if authDSN == "" {
			_ = db.Close()
			return nil, fmt.Errorf("auth %s DSN is empty", authDriver)
		}
		authCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		authStore, authErr := auth.Open(authCtx, authDriver, authDSN)
		if authErr != nil {
			_ = db.Close()
			return nil, authErr
		}
		if authErr = authStore.BootstrapAdmin(authCtx, cfg.AdminUser, cfg.AdminPassword); authErr != nil {
			_ = authStore.Close()
			_ = db.Close()
			return nil, fmt.Errorf("bootstrap admin: %w", authErr)
		}
		app.auth = authStore
	}
	app.startKeepAlive()
	return app, nil
}

func (a *App) Close() error {
	if a.keepAliveCancel != nil {
		a.keepAliveCancel()
		<-a.keepAliveDone
		a.keepAliveCancel = nil
	}
	if a.db != nil {
		if a.auth != nil {
			_ = a.auth.Close()
		}
		return a.db.Close()
	}
	return nil
}

func (a *App) Handler() http.Handler {
	if os.Getenv(gin.EnvGinMode) == "" {
		gin.SetMode(gin.ReleaseMode)
	}

	router := gin.New()
	router.Use(gin.Logger(), gin.Recovery())

	router.Any("/login", gin.WrapF(a.handleLogin))
	router.Any("/register", gin.WrapF(a.handleRegister))
	router.Any("/logout", gin.WrapF(a.handleLogout))
	router.Any("/health", func(c *gin.Context) {
		writeJSON(c.Writer, http.StatusOK, gin.H{"ok": true})
	})
	router.Use(func(c *gin.Context) {
		if strings.HasPrefix(c.Request.URL.Path, "/static/") {
			c.Header("Cache-Control", "no-cache")
		}
		c.Next()
	})
	router.StaticFS("/static", http.Dir(a.resources.Static))
	// Existing automation clients must remain independent from browser sessions.
	router.Any("/wx/oauth", gin.WrapF(a.handlePublicOAuth))
	router.Any("/wxapp/getCode", gin.WrapF(a.handleGetCode))
	router.Any("/wxapp/getPhoneNumber", gin.WrapF(a.handleGetPhoneNumber))
	router.Any("/wxapp/operateWxData", gin.WrapF(a.handleOperateWXData))
	router.Any("/wx/code", gin.WrapF(a.handleWXCodeAlias))
	router.Any("/wx/getuserinfo", gin.WrapF(a.handleWXGetUserInfo))
	router.Any("/wx/encryptkey", gin.WrapF(a.handleWXEncryptKey))
	router.Any("/wx/getlatestuserkey", gin.WrapF(a.handleWXLatestUserKey))
	router.Any("/wx/getphonenumber", gin.WrapF(a.handleWXPhoneAlias))
	router.Any("/wx/cloud", gin.WrapF(a.handleWXCloud))
	router.Any("/wx/qrcodeauth", gin.WrapF(a.handleQRRoot))
	router.Any("/wx/qrcodeauth/*path", gin.WrapF(a.handleQR))
	router.Any("/wx/mpgeta8key", gin.WrapF(a.handleWXMPGetA8Key))
	router.Any("/wx/appmsgext", gin.WrapF(a.handleWXAppMsgExt))
	router.Any("/wx/appmsglike", gin.WrapF(a.handleWXAppMsgLike))
	router.Any("/openapi.json", gin.WrapF(a.handleOpenAPI))

	router.Use(a.requireBrowserSession())
	router.Any("/settings", gin.WrapF(a.handleSettingsPage))
	router.Any("/users", gin.WrapF(a.handleUsersPage))
	router.Any("/api/auth/me", gin.WrapF(a.handleAuthMe))
	router.Any("/api/auth/profile", gin.WrapF(a.handleProfile))
	router.Any("/api/auth/password", gin.WrapF(a.handlePassword))
	router.Any("/api/auth/sessions", gin.WrapF(a.handleSessions))
	router.Any("/api/auth/users", gin.WrapF(a.handleUsers))
	router.Any("/api/auth/users/*path", gin.WrapF(a.handleUserAction))
	router.Any("/api/auth/registration", gin.WrapF(a.handleRegistrationSetting))
	router.Any("/", gin.WrapF(a.handleIndex))
	router.Any("/scan", gin.WrapF(a.handleScan))
	router.Any("/proxies", gin.WrapF(a.handleProxiesPage))
	router.Any("/runs", gin.WrapF(a.handleRuns))
	router.Any("/docs", func(c *gin.Context) {
		c.Redirect(http.StatusMovedPermanently, "/docs/index.html")
	})
	router.Any("/docs/*path", gin.WrapF(a.handleDocs))
	router.Any("/qr", gin.WrapF(a.handleQRRoot))
	router.Any("/qr/*path", gin.WrapF(a.handleQR))
	router.Any("/quick-login", gin.WrapF(a.handleQuickLoginRoot))
	router.Any("/quick-login/*path", gin.WrapF(a.handleQuickLogin))
	router.Any("/accounts", gin.WrapF(a.handleAccountsRoot))
	router.Any("/accounts/repair", gin.WrapF(a.handleAccountRepair))
	router.Any("/accounts/compact", gin.WrapF(a.handleAccountCompact))
	router.Any("/accounts/avatar", gin.WrapF(a.handleAccountAvatar))
	router.Any("/accounts/refresh", gin.WrapF(a.handleAccountRefresh))
	router.Any("/accounts/resync", gin.WrapF(a.handleAccountResync))
	router.Any("/accounts/remark", gin.WrapF(a.handleAccountRemark))
	router.Any("/accounts/status", gin.WrapF(a.handleAccountStatus))
	router.Any("/accounts/proxy", gin.WrapF(a.handleAccountProxy))
	router.Any("/accounts/proxy/test", gin.WrapF(a.handleAccountProxyTest))
	router.Any("/api/proxy-profiles", gin.WrapF(a.handleProxyProfiles))
	router.Any("/api/proxy-profiles/*path", gin.WrapF(a.handleProxyProfiles))
	router.Any("/api/proxy-location/recommend", gin.WrapF(a.handleProxyLocationRecommend))
	router.Any("/api/qinglong/status", gin.WrapF(a.handleQingLongStatus))
	router.Any("/api/qinglong/config", gin.WrapF(a.handleQingLongConfig))
	router.Any("/api/qinglong/sync", gin.WrapF(a.handleQingLongSync))
	router.Any("/api/qinglong/sync-all", gin.WrapF(a.handleQingLongSyncAll))
	router.Any("/api/qinglong/jobs", gin.WrapF(a.handleQingLongJobs))
	router.Any("/api/qinglong/jobs/enable", gin.WrapF(a.handleQingLongJobEnable))
	router.Any("/api/qinglong/jobs/run", gin.WrapF(a.handleQingLongJobRun))
	router.Any("/api/qinglong/jobs/log", gin.WrapF(a.handleQingLongJobLog))
	router.Any("/api/qinglong/runs", gin.WrapF(a.handleQingLongRuns))
	router.Any("/api/qinglong/runs/log", gin.WrapF(a.handleQingLongRunLog))
	router.Any("/api/qinglong/push", gin.WrapF(a.handleQingLongPush))
	// Keep the shorter /wx/* names used by existing YYB clients. The handlers
	// share the same session and retry logic as the canonical /wxapp/* routes.
	router.NoRoute(func(c *gin.Context) {
		writeError(c.Writer, http.StatusNotFound, "not found")
	})

	return router
}

func (a *App) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	serveFileOrText(w, r, filepath.Join(a.resources.Templates, "index.html"), fallbackIndexHTML)
}

func (a *App) handleScan(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	path := filepath.Join(a.resources.Templates, "scan.html")
	data, err := os.ReadFile(path)
	if err != nil {
		serveFileOrText(w, r, path, fallbackScanHTML)
		return
	}
	// Desktop-WeChat authorization is experimental and remains opt-in.
	html := strings.ReplaceAll(string(data), "__YYB_PC_LOGIN_ENABLED__", fmt.Sprintf("%t", a.cfg.EnablePCLogin))
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(html))
}

func (a *App) handleProxiesPage(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	serveFileOrText(w, r, filepath.Join(a.resources.Templates, "proxies.html"), fallbackProxiesHTML)
}

func (a *App) handleDocs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if r.URL.Path == "/docs/" {
		http.Redirect(w, r, "/docs/index.html", http.StatusMovedPermanently)
		return
	}
	swaggerDocsHandler.ServeHTTP(w, r)
}

func (a *App) handleOpenAPI(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writeRawJSON(w, http.StatusOK, openAPISpec)
}

func (a *App) handleQRRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/qr" && r.URL.Path != "/wx/qrcodeauth" {
		writeError(w, http.StatusNotFound, "qr session not found")
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	a.pruneQR()
	var body accountProxyIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if _, err := normalizeLoginProduct(body.Product); err != nil {
		writeError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.RequestTimeout+35*time.Second)
	defer cancel()
	normalizedBody, proxySpec, err := a.normalizeAccountProxyInput(ctx, body)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	client, resolvedProxy, err := a.qrClientForSpec(ctx, proxySpec)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	img, err := client.GetQRCodeImage(ctx)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	a.mu.Lock()
	a.qrSessions[img.Session.ID] = &qrLoginSession{
		Session: img.Session, Client: client, ProxySpec: proxySpec, ProxyIn: normalizedBody,
		ImageBytes: append([]byte(nil), img.ImageBytes...),
	}
	keep := make(map[string]bool, len(a.qrSessions))
	for sid := range a.qrSessions {
		keep[sid] = true
	}
	a.mu.Unlock()
	path := a.resources.qrPath(img.Session.ID)
	// The in-memory copy above is authoritative. The file is only a cache for
	// deployments that serve the image endpoint after a process restart.
	_ = os.WriteFile(path, img.ImageBytes, 0o644)
	a.cleanupQR(keep)
	basePath := "/qr"
	if r.URL.Path == "/wx/qrcodeauth" {
		basePath = "/wx/qrcodeauth"
	}
	out := map[string]any{
		"session_id": img.Session.ID,
		"status":     img.Session.Status,
		"image_url":  basePath + "/" + img.Session.ID + "/image",
		"proxy":      proxysource.Mask(resolvedProxy),
	}
	if r.URL.Query().Get("as_base64") == "true" {
		out["image_base64"] = qr.DataURIJPEG(img.ImageBytes)
	} else {
		out["image_base64"] = nil
	}
	writeJSON(w, http.StatusOK, out)
}

func (a *App) handleQR(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/qr/")
	if path == r.URL.Path {
		path = strings.TrimPrefix(r.URL.Path, "/wx/qrcodeauth/")
	}
	parts := strings.Split(path, "/")
	if len(parts) != 2 {
		writeError(w, http.StatusNotFound, "qr session not found")
		return
	}
	sessionID, action := parts[0], parts[1]
	switch action {
	case "image":
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		if login := a.getQRSession(sessionID); login != nil && len(login.ImageBytes) > 0 {
			w.Header().Set("Content-Type", http.DetectContentType(login.ImageBytes))
			w.Header().Set("Cache-Control", "no-store")
			_, _ = w.Write(login.ImageBytes)
			return
		}
		path := a.resources.qrPath(sessionID)
		if _, err := os.Stat(path); err != nil {
			writeError(w, http.StatusNotFound, "qr image not found; the QR session may have expired")
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		http.ServeFile(w, r, path)
	case "poll":
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		login := a.getQRSession(sessionID)
		if login == nil {
			writeError(w, http.StatusNotFound, "qr session not found")
			return
		}
		result, err := login.Client.PollQRCode(r.Context(), login.Session)
		if err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		if terminalQR(result.Status) {
			a.dropQRSession(sessionID)
		}
		writeJSON(w, http.StatusOK, result)
	case "cancel":
		if r.Method != http.MethodPost && r.Method != http.MethodDelete {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		// Refreshing the QR code must retire the previous session. It never
		// touched the account database, but dropping it prevents a delayed poll
		// or confirm request from completing an old scan unexpectedly.
		a.dropQRSession(sessionID)
		writeJSON(w, http.StatusOK, map[string]any{"status": "cancelled", "session_id": sessionID})
	case "confirm":
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		login := a.getQRSession(sessionID)
		if login == nil {
			writeError(w, http.StatusNotFound, "qr session not found")
			return
		}
		result, err := login.Client.GetLoginBuffer(r.Context(), login.Session)
		if err != nil {
			writeError(w, http.StatusConflict, "buffer not ready: "+err.Error())
			return
		}
		// A refresh/cancel may have removed this session while the OAuth
		// exchange was in flight. Serialize the final write with cancellation
		// so an old QR request cannot create an account after it was retired.
		login.mu.Lock()
		dropAfterConfirm := false
		defer func() {
			login.mu.Unlock()
			if dropAfterConfirm {
				a.dropQRSession(sessionID)
			}
		}()
		if login.cancelled {
			writeError(w, http.StatusConflict, "qr session cancelled")
			return
		}
		var userInfo map[string]any
		if ui, err := login.Client.LoginBuffers().FetchUserInfo(r.Context(), result.Credentials); err == nil {
			userInfo = ui
		}
		existed, err := a.accountExistsBeforeScan(r.Context(), result.Credentials.OpenID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		if err := a.ensureScannedAccountAllowed(r, result.Credentials.OpenID); err != nil {
			writeError(w, http.StatusForbidden, err.Error())
			return
		}
		acc, err := a.storeFromScan(r.Context(), result.LoginBuffer, result.Credentials, userInfo)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		if err := a.claimScannedAccount(r, acc.ID); err != nil {
			if !existed {
				_ = a.db.DeleteAccount(r.Context(), acc.ID)
			}
			writeError(w, http.StatusForbidden, err.Error())
			return
		}
		if err := a.saveNewAccountProxy(r.Context(), acc.ID, existed, login.ProxyIn, login.ProxySpec); err != nil {
			if !existed {
				// Do not leave a phantom account when the optional proxy save fails.
				_ = a.db.DeleteAccount(r.Context(), acc.ID)
			}
			writeError(w, http.StatusInternalServerError, "保存账号代理失败: "+err.Error())
			return
		}
		dropAfterConfirm = true
		writeJSON(w, http.StatusOK, acc.Public())
	default:
		writeError(w, http.StatusNotFound, "qr session not found")
	}
}

func (a *App) handleAccountsRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/accounts" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	switch r.Method {
	case http.MethodGet:
		accounts, err := a.visibleAccounts(r)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		out := make([]store.AccountPublic, 0, len(accounts))
		for _, acc := range accounts {
			out = append(out, acc.Public())
		}
		writeJSON(w, http.StatusOK, out)
	case http.MethodDelete:
		acc, ok := a.resolveAccountFromQuery(w, r)
		if !ok {
			return
		}
		cleanup, err := a.cleanupAccountFromQingLong(r.Context(), acc)
		if err != nil {
			writeError(w, http.StatusBadGateway, "青龙关联数据清理失败，本地账号未删除："+err.Error())
			return
		}
		if err := a.db.DeleteAccount(r.Context(), acc.ID); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		a.invalidateProxyLease(acc.ID)
		a.clearKeepAliveRetry(acc.ID)
		writeJSON(w, http.StatusOK, map[string]any{
			"deleted": acc.ID, "openid": acc.OpenID,
			"qinglong_cleanup": cleanup.Status, "env_entries_removed": cleanup.EnvEntriesRemoved,
			"tasks_deleted": cleanup.TasksDeleted,
		})
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (a *App) handleAccountAvatar(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/accounts/avatar" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	acc, ok := a.resolveAccountFromQuery(w, r)
	if !ok {
		return
	}
	a.serveAvatar(w, r, acc)
}

func (a *App) handleAccountRefresh(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/accounts/refresh" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body accountRefIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if body.Ref == "" {
		a.refreshAll(w, r)
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	status, refreshErr := a.refreshLiveness(r.Context(), acc)
	writeJSON(w, http.StatusOK, refreshOut(acc, status, refreshErr))
}

func (a *App) handleAccountResync(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/accounts/resync" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body accountRefIn
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if body.Ref == "" {
		a.resyncAll(w, r)
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	updated, err := a.resyncProfile(r.Context(), acc)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, updated.Public())
}

func (a *App) handleGetCode(w http.ResponseWriter, r *http.Request) {
	if !acceptWXAppRoute(w, r, "/wxapp/getCode") {
		return
	}
	a.callWXApp(w, r, false, a.invokeGetCode)
}

func (a *App) handleWXCodeAlias(w http.ResponseWriter, r *http.Request) {
	if !acceptWXAppRoute(w, r, "/wx/code") {
		return
	}
	a.callWXApp(w, r, false, a.invokeGetCode)
}

func (a *App) handleGetPhoneNumber(w http.ResponseWriter, r *http.Request) {
	if !acceptWXAppRoute(w, r, "/wxapp/getPhoneNumber") {
		return
	}
	a.callWXApp(w, r, false, a.invokeGetPhoneNumber)
}

func (a *App) handleWXPhoneAlias(w http.ResponseWriter, r *http.Request) {
	if !acceptWXAppRoute(w, r, "/wx/getphonenumber") {
		return
	}
	a.callWXApp(w, r, false, a.invokeGetPhoneNumber)
}

func (a *App) handleOperateWXData(w http.ResponseWriter, r *http.Request) {
	if !acceptWXAppRoute(w, r, "/wxapp/operateWxData") {
		return
	}
	a.callWXApp(w, r, true, a.invokeOperateWXData)
}

func (a *App) handleWXEncryptKey(w http.ResponseWriter, r *http.Request) {
	a.handleNamedWXOperation(w, r, "/wx/encryptkey", "getUserEncryptKey", true)
}

// handleWXLatestUserKey exposes the client-side getLatestUserKey name while
// forwarding the corresponding server-side getUserEncryptKey operation.
func (a *App) handleWXLatestUserKey(w http.ResponseWriter, r *http.Request) {
	a.handleNamedWXOperation(w, r, "/wx/getlatestuserkey", "getUserEncryptKey", true)
}

func (a *App) handleWXCloud(w http.ResponseWriter, r *http.Request) {
	a.handleNamedWXOperation(w, r, "/wx/cloud", "cloud.callFunction", true)
}

func (a *App) handleWXMPGetA8Key(w http.ResponseWriter, r *http.Request) {
	a.handleNamedWXOperation(w, r, "/wx/mpgeta8key", "mpGetA8Key", true)
}

func (a *App) handleWXAppMsgExt(w http.ResponseWriter, r *http.Request) {
	a.handleNamedWXOperation(w, r, "/wx/appmsgext", "appmsgext", true)
}

func (a *App) handleWXAppMsgLike(w http.ResponseWriter, r *http.Request) {
	a.handleNamedWXOperation(w, r, "/wx/appmsglike", "appmsglike", true)
}

// handleNamedWXOperation adapts named /wx/* compatibility calls to the
// generic operateWxData transport. Callers may provide a complete payload;
// otherwise a minimal api_name/data envelope is generated.
func (a *App) handleNamedWXOperation(w http.ResponseWriter, r *http.Request, path, apiName string, requirePayload bool) {
	if !acceptWXAppRoute(w, r, path) {
		return
	}
	var body wxappRequest
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if body.Payload == nil {
		if requirePayload {
			writeError(w, http.StatusBadRequest, "payload is required for "+apiName)
			return
		}
		body.Payload = map[string]any{"api_name": apiName, "data": map[string]any{}, "env": 1}
	}
	result, err := a.invokeNamedWXOperation(r.Context(), body, apiName)
	if err != nil {
		var expired accountExpiredError
		switch {
		case errors.Is(err, sql.ErrNoRows):
			writeError(w, http.StatusNotFound, "account not found: "+body.Ref)
			return
		case errors.As(err, &expired):
			writeError(w, http.StatusConflict, "account login_buffer expired (refresh failed); re-scan required")
			return
		case strings.Contains(err.Error(), " is required"):
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		writeError(w, http.StatusBadGateway, "call failed: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (a *App) invokeNamedWXOperation(ctx context.Context, body wxappRequest, apiName string) (map[string]any, error) {
	if body.Ref == "" {
		return nil, fmt.Errorf("ref is required")
	}
	if body.AppID == "" {
		return nil, fmt.Errorf("app_id is required")
	}
	acc, err := a.db.ResolveAccount(ctx, strings.TrimSpace(body.Ref))
	if err != nil {
		return nil, err
	}
	if body.Payload == nil {
		body.Payload = map[string]any{"api_name": apiName, "data": map[string]any{}, "env": 1}
	}
	if apiName == "getUserEncryptKey" {
		body.Payload = normalizeEncryptKeyPayload(body.Payload)
	}
	result, err := a.invokeWXApp(ctx, acc, body.AppID, body.Payload, a.invokeOperateWXData)
	if err != nil {
		return nil, err
	}
	return map[string]any{"openid": acc.OpenID, "result": result}, nil
}

// normalizeEncryptKeyPayload accepts the client API spelling used by
// wx.getUserCryptoManager().getLatestUserKey(). The iLink server operation is
// named getUserEncryptKey; only the operation name is adapted and all business
// data is preserved unchanged.
func normalizeEncryptKeyPayload(payload map[string]any) map[string]any {
	if payload == nil {
		return nil
	}
	out := make(map[string]any, len(payload))
	for key, value := range payload {
		out[key] = value
	}
	if name, ok := out["api_name"].(string); ok && name == "getLatestUserKey" {
		out["api_name"] = "getUserEncryptKey"
	}
	if nested, ok := out["data"].(map[string]any); ok {
		copyNested := make(map[string]any, len(nested))
		for key, value := range nested {
			copyNested[key] = value
		}
		if name, ok := copyNested["api_name"].(string); ok && name == "getLatestUserKey" {
			copyNested["api_name"] = "getUserEncryptKey"
		}
		out["data"] = copyNested
	}
	return out
}

func (a *App) handleWXGetUserInfo(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/wx/getuserinfo" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body accountRefIn
	if r.Method == http.MethodPost {
		if err := decodeOptionalJSON(r, &body); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
	} else {
		body.Ref = r.URL.Query().Get("ref")
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	if len(acc.Credentials) == 0 {
		writeError(w, http.StatusConflict, "account has no login credentials")
		return
	}
	if accountStatus(acc) == "expired" {
		writeError(w, http.StatusConflict, "account login_buffer expired; re-scan required")
		return
	}
	proxyValue, fallbackDirect, err := a.resolveAccountProxy(r.Context(), acc.ID)
	if err != nil {
		writeError(w, http.StatusBadGateway, "resolve account proxy failed: "+err.Error())
		return
	}
	creds := protocol.CredentialsFromMap(acc.Credentials)
	info, err := a.fetchUserInfoWithProxy(r.Context(), creds, proxyValue, fallbackDirect)
	if err != nil {
		if status, _ := a.refreshLivenessWithProxy(r.Context(), acc, proxyValue, fallbackDirect); status == "alive" {
			if fresh, getErr := a.db.GetAccount(r.Context(), acc.ID); getErr == nil {
				acc = fresh
				info, err = a.fetchUserInfoWithProxy(r.Context(), protocol.CredentialsFromMap(acc.Credentials), proxyValue, fallbackDirect)
			}
		}
	}
	if err != nil {
		writeError(w, http.StatusBadGateway, "getuserinfo failed: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"openid": acc.OpenID, "user_info": info})
}

func acceptWXAppRoute(w http.ResponseWriter, r *http.Request, path string) bool {
	if r.URL.Path != path {
		writeError(w, http.StatusNotFound, "not found")
		return false
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return false
	}
	return true
}

type accountRefIn struct {
	Ref string `json:"ref"`
}

type wxappRequest struct {
	Ref     string         `json:"ref"`
	AppID   string         `json:"app_id"`
	Payload map[string]any `json:"payload"`
}

type wxappCall func(ctx context.Context, acc *store.WechatAccount, appID string, payload map[string]any, proxyValue string, fallbackDirect bool) (map[string]any, error)

func (a *App) callWXApp(w http.ResponseWriter, r *http.Request, requirePayload bool, call wxappCall) {
	var body wxappRequest
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if body.Ref == "" {
		writeError(w, http.StatusBadRequest, "ref is required")
		return
	}
	if body.AppID == "" {
		writeError(w, http.StatusBadRequest, "app_id is required")
		return
	}
	if requirePayload && body.Payload == nil {
		writeError(w, http.StatusBadRequest, "payload is required")
		return
	}
	acc, ok := a.resolveAccountRef(w, r, body.Ref)
	if !ok {
		return
	}
	result, err := a.invokeWXApp(r.Context(), acc, body.AppID, body.Payload, call)
	if err != nil {
		var expired accountExpiredError
		switch {
		case errors.As(err, &expired):
			writeError(w, http.StatusConflict, "account login_buffer expired (refresh failed); re-scan required")
		default:
			writeError(w, http.StatusBadGateway, "call failed: "+err.Error())
		}
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"openid": acc.OpenID,
		"account": map[string]any{
			"id":       acc.ID,
			"alias":    acc.Alias,
			"nickname": acc.Nickname,
			"remark":   acc.Remark,
		},
		"result": result,
	})
}

func decodeOptionalJSON(r *http.Request, dst any) error {
	err := json.NewDecoder(r.Body).Decode(dst)
	if errors.Is(err, io.EOF) {
		return nil
	}
	return err
}

func (a *App) resolveAccountFromQuery(w http.ResponseWriter, r *http.Request) (*store.WechatAccount, bool) {
	ref := strings.TrimSpace(r.URL.Query().Get("ref"))
	if ref == "" {
		writeError(w, http.StatusBadRequest, "ref query param is required")
		return nil, false
	}
	return a.resolveAccountRef(w, r, ref)
}

func (a *App) resolveAccountRef(w http.ResponseWriter, r *http.Request, ref string) (*store.WechatAccount, bool) {
	ref = strings.TrimSpace(ref)
	if ref == "" {
		writeError(w, http.StatusBadRequest, "ref is required")
		return nil, false
	}
	acc, err := a.db.ResolveAccount(r.Context(), ref)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeError(w, http.StatusNotFound, "account not found: "+ref)
		} else {
			writeError(w, http.StatusInternalServerError, err.Error())
		}
		return nil, false
	}
	if !a.accountVisible(r, acc.ID) {
		writeError(w, http.StatusNotFound, "账号不存在")
		return nil, false
	}
	return acc, true
}

func (a *App) browserUser(r *http.Request) *auth.User {
	user, _ := currentAuth(r)
	if user != nil || a.auth == nil {
		return user
	}
	cookie, err := r.Cookie(sessionCookie)
	if err != nil || cookie.Value == "" {
		return nil
	}
	user, _, err = a.auth.UserBySession(r.Context(), cookie.Value)
	if err != nil {
		return nil
	}
	return user
}

func (a *App) accountVisible(r *http.Request, accountID int64) bool {
	user := a.browserUser(r)
	if a.auth == nil || user == nil || user.Role == "admin" {
		return true
	}
	owner, err := a.auth.AccountOwner(r.Context(), accountID)
	return err == nil && owner == user.ID
}

func (a *App) visibleAccounts(r *http.Request) ([]*store.WechatAccount, error) {
	accounts, err := a.db.ListAccounts(r.Context())
	if err != nil || a.auth == nil {
		return accounts, err
	}
	user := a.browserUser(r)
	if user == nil || user.Role == "admin" {
		return accounts, nil
	}
	return a.accountsOwnedBy(r.Context(), user.ID, accounts)
}

func (a *App) accountsOwnedBy(ctx context.Context, userID int64, accounts []*store.WechatAccount) ([]*store.WechatAccount, error) {
	owned, err := a.auth.AccountIDs(ctx, userID)
	if err != nil {
		return nil, err
	}
	visible := make([]*store.WechatAccount, 0, len(accounts))
	for _, account := range accounts {
		if _, ok := owned[account.ID]; ok {
			visible = append(visible, account)
		}
	}
	return visible, nil
}

func (a *App) refreshAll(w http.ResponseWriter, r *http.Request) {
	accounts, err := a.visibleAccounts(r)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]map[string]any, 0, len(accounts))
	for _, acc := range accounts {
		status, refreshErr := a.refreshLiveness(r.Context(), acc)
		out = append(out, refreshOut(acc, status, refreshErr))
	}
	writeJSON(w, http.StatusOK, out)
}

func (a *App) claimScannedAccount(r *http.Request, accountID int64) error {
	if a.auth == nil {
		return nil
	}
	user := a.browserUser(r)
	if user == nil || user.Role == "admin" {
		return nil
	}
	owner, err := a.auth.AccountOwner(r.Context(), accountID)
	if err == nil && owner != user.ID {
		return errors.New("该 YYB 账号已属于其他用户")
	}
	return a.auth.ClaimAccount(r.Context(), accountID, user.ID)
}

func (a *App) ensureScannedAccountAllowed(r *http.Request, openID string) error {
	if a.auth == nil {
		return nil
	}
	user := a.browserUser(r)
	if user == nil || user.Role == "admin" {
		return nil
	}
	existing, err := a.db.GetAccountByOpenID(r.Context(), openID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	owner, ownerErr := a.auth.AccountOwner(r.Context(), existing.ID)
	if ownerErr == nil && owner == user.ID {
		return nil
	}
	if ownerErr != nil && errors.Is(ownerErr, sql.ErrNoRows) {
		return errors.New("该账号已存在但尚未分配给普通用户，请联系管理员")
	}
	return errors.New("该 YYB 账号已属于其他用户")
}

func (a *App) resyncAll(w http.ResponseWriter, r *http.Request) {
	accounts, err := a.visibleAccounts(r)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]store.AccountPublic, 0, len(accounts))
	for _, acc := range accounts {
		updated, err := a.resyncProfile(r.Context(), acc)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		out = append(out, updated.Public())
	}
	writeJSON(w, http.StatusOK, out)
}

func (a *App) serveAvatar(w http.ResponseWriter, r *http.Request, acc *store.WechatAccount) {
	if acc.Avatar != nil && *acc.Avatar != "" {
		if _, err := os.Stat(*acc.Avatar); err == nil {
			w.Header().Set("Content-Type", "image/jpeg")
			http.ServeFile(w, r, *acc.Avatar)
			return
		}
		if strings.HasPrefix(*acc.Avatar, "http://") || strings.HasPrefix(*acc.Avatar, "https://") {
			http.Redirect(w, r, *acc.Avatar, http.StatusFound)
			return
		}
	}
	writeError(w, http.StatusNotFound, "no avatar")
}

func (a *App) storeFromScan(ctx context.Context, loginBuffer string, creds protocol.LoginBufferCredentials, userInfo map[string]any) (*store.WechatAccount, error) {
	openid := creds.OpenID
	nick := pickNickname(userInfo, creds.Nickname)
	avatar := a.resolveAvatar(ctx, openid, userInfo)
	status := "alive"
	return a.db.UpsertAccount(ctx, openid, loginBuffer, stringPtrMaybe(nick), stringPtrMaybe(nick), stringPtrMaybe(avatar), userInfo, creds.ToMap(), &status)
}

func (a *App) resyncProfile(ctx context.Context, acc *store.WechatAccount) (*store.WechatAccount, error) {
	nick := pickNickname(acc.UserInfo, deref(acc.Nickname))
	avatar := a.resolveAvatar(ctx, acc.OpenID, acc.UserInfo)
	if avatar == "" {
		avatar = deref(acc.Avatar)
	}
	if err := a.db.SetAccountProfile(ctx, acc.ID, stringPtrMaybe(nick), stringPtrMaybe(avatar), acc.UserInfo); err != nil {
		return nil, err
	}
	return a.db.GetAccount(ctx, acc.ID)
}

type accountExpiredError struct{ openid string }

func (e accountExpiredError) Error() string { return "account expired: " + e.openid }

func (a *App) invokeWXApp(ctx context.Context, acc *store.WechatAccount, appID string, payload map[string]any, call wxappCall) (map[string]any, error) {
	if accountStatus(acc) == "expired" {
		return nil, accountExpiredError{openid: acc.OpenID}
	}
	if accountNeedsRescan(acc) {
		return nil, accountExpiredError{openid: acc.OpenID}
	}
	proxyValue, fallbackDirect, err := a.resolveAccountProxy(ctx, acc.ID)
	if err != nil {
		return nil, fmt.Errorf("resolve account proxy: %w", err)
	}
	result, callErr := call(ctx, acc, appID, payload, proxyValue, fallbackDirect)
	if callErr == nil {
		return result, nil
	}
	_ = a.db.InvalidateSession(ctx, acc.ID, proxyValue)
	status, refreshErr := a.refreshLivenessWithProxy(ctx, acc, proxyValue, fallbackDirect)
	if status != "alive" {
		if status == "expired" {
			return nil, accountExpiredError{openid: acc.OpenID}
		}
		return nil, fmt.Errorf("refresh account credentials: %w", refreshErr)
	}
	if refreshErr != nil {
		return nil, fmt.Errorf("refresh account credentials: %w", refreshErr)
	}
	fresh, err := a.db.GetAccount(ctx, acc.ID)
	if err == nil && fresh != nil {
		acc = fresh
	}
	return call(ctx, acc, appID, payload, proxyValue, fallbackDirect)
}

// accountNeedsRescan covers accounts whose refresh attempt was inconclusive
// but whose cached credential has already expired. They must not be sent into
// every script invocation until the user scans again.
func accountNeedsRescan(acc *store.WechatAccount) bool {
	if acc == nil || accountStatus(acc) != "unknown" || acc.Credentials == nil {
		return false
	}
	expiresAt := protocol.CredentialsFromMap(acc.Credentials).ExpiresAt
	return expiresAt > 0 && expiresAt <= time.Now().Unix()
}

func (a *App) invokeGetCode(ctx context.Context, acc *store.WechatAccount, appID string, _ map[string]any, proxyValue string, fallbackDirect bool) (map[string]any, error) {
	return a.pool.GetCode(ctx, acc.LoginBuffer, appID, acc.ID, proxyValue, fallbackDirect)
}

func (a *App) invokeGetPhoneNumber(ctx context.Context, acc *store.WechatAccount, appID string, _ map[string]any, proxyValue string, fallbackDirect bool) (map[string]any, error) {
	return a.pool.GetPhoneNumber(ctx, acc.LoginBuffer, appID, acc.ID, proxyValue, fallbackDirect)
}

func (a *App) invokeOperateWXData(ctx context.Context, acc *store.WechatAccount, appID string, payload map[string]any, proxyValue string, fallbackDirect bool) (map[string]any, error) {
	return a.pool.OperateWXData(ctx, acc.LoginBuffer, appID, payload, acc.ID, proxyValue, fallbackDirect)
}

func refreshOut(acc *store.WechatAccount, status string, refreshErr error) map[string]any {
	out := map[string]any{
		"id": acc.ID, "openid": acc.OpenID, "uin": acc.UIN, "nickname": acc.Nickname,
		"status": status, "rescan_required": status == "expired",
	}
	if refreshErr != nil {
		out["refresh_error"] = refreshErr.Error()
	}
	return out
}

func pickNickname(userInfo map[string]any, fallback string) string {
	if s := stringFromAny(userInfo["nick_name"]); s != "" {
		return s
	}
	return fallback
}

func pickAvatarURL(userInfo map[string]any) string {
	for _, k := range []string{"head_img_url", "head_url", "headimgurl", "avatar"} {
		if s := stringFromAny(userInfo[k]); s != "" {
			return s
		}
	}
	return ""
}

func (a *App) resolveAvatar(ctx context.Context, openid string, userInfo map[string]any) string {
	u := pickAvatarURL(userInfo)
	if u == "" {
		return ""
	}
	dest := a.resources.avatarPath(openid)
	if downloadAvatar(ctx, u, dest, a.cfg.AvatarTimeout) {
		return dest
	}
	return u
}

func downloadAvatar(ctx context.Context, url, dest string, timeout time.Duration) bool {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false
	}
	client := &http.Client{Timeout: timeout}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil || resp.StatusCode != 200 || !looksLikeImage(data) {
		return false
	}
	_ = os.MkdirAll(filepath.Dir(dest), 0o755)
	return os.WriteFile(dest, data, 0o644) == nil
}

func looksLikeImage(data []byte) bool {
	if len(data) < 64 {
		return false
	}
	magics := [][]byte{{0xff, 0xd8, 0xff}, {0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}, []byte("GIF87a"), []byte("GIF89a")}
	for _, m := range magics {
		if strings.HasPrefix(string(data), string(m)) {
			return true
		}
	}
	return false
}

func (a *App) getQRSession(id string) *qrLoginSession {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.qrSessions[id]
}

func (a *App) dropQRSession(id string) {
	a.mu.Lock()
	login := a.qrSessions[id]
	if login != nil {
		login.mu.Lock()
		login.cancelled = true
		login.mu.Unlock()
	}
	delete(a.qrSessions, id)
	a.mu.Unlock()
	_ = os.Remove(a.resources.qrPath(id))
}

func (a *App) pruneQR() {
	a.mu.Lock()
	var drop []string
	for sid, sess := range a.qrSessions {
		if sess.Session.Age() > a.cfg.QRSessionTTL {
			sess.mu.Lock()
			sess.cancelled = true
			sess.mu.Unlock()
			drop = append(drop, sid)
		}
	}
	for _, sid := range drop {
		delete(a.qrSessions, sid)
	}
	a.mu.Unlock()
	for _, sid := range drop {
		_ = os.Remove(a.resources.qrPath(sid))
	}
}

func (a *App) cleanupQR(keep map[string]bool) {
	files, _ := filepath.Glob(filepath.Join(a.resources.QR, "*.jpg"))
	for _, f := range files {
		sid := strings.TrimSuffix(filepath.Base(f), ".jpg")
		if !keep[sid] {
			_ = os.Remove(f)
		}
	}
}

func terminalQR(status string) bool {
	return status == "expired" || status == "cancelled" || status == "unknown"
}

type apiEnvelope struct {
	Code int    `json:"code"`
	Msg  string `json:"msg"`
	Data any    `json:"data"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	writeRawJSON(w, status, apiEnvelope{
		Code: 0,
		Msg:  "success",
		Data: v,
	})
}

func writeRawJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, detail string) {
	writeRawJSON(w, status, apiEnvelope{
		Code: status,
		Msg:  detail,
		Data: nil,
	})
}

func requestLogger(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(w, r)
	})
}

func serveFileOrText(w http.ResponseWriter, r *http.Request, path, fallback string) {
	if _, err := os.Stat(path); err == nil {
		http.ServeFile(w, r, path)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(fallback))
}

func stringFromAny(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func stringPtrMaybe(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func safeName(s string) string {
	var b strings.Builder
	for _, r := range s {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '-' || r == '_' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

func sortedKeys[M ~map[string]V, V any](m M) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
