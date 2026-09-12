package httpapi

import (
	"context"
	"database/sql"
	"errors"
	"net"
	"net/http"
	"net/url"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"yyb_go/internal/auth"
	"yyb_go/internal/store"
)

const sessionCookie = "yyb_session"

type loginAttempt struct {
	Failures int
	Start    time.Time
}

type authContextKey string

const authUserKey authContextKey = "user"
const authSessionKey authContextKey = "session"

func (a *App) requireBrowserSession() gin.HandlerFunc {
	return func(c *gin.Context) {
		// Let unmatched routes reach the JSON 404 handler instead of masking
		// a misspelled automation endpoint with a login redirect.
		if a.auth == nil || c.FullPath() == "" {
			c.Next()
			return
		}
		token, err := c.Cookie(sessionCookie)
		if err == nil {
			user, session, lookupErr := a.auth.UserBySession(c.Request.Context(), token)
			if lookupErr == nil {
				ctx := context.WithValue(c.Request.Context(), authUserKey, user)
				ctx = context.WithValue(ctx, authSessionKey, session)
				c.Request = c.Request.WithContext(ctx)
				c.Next()
				return
			}
		}
		clearSessionCookie(c.Writer, a.cfg.CookieSecure)
		if isManagementAPI(c.Request.URL.Path) {
			writeError(c.Writer, http.StatusUnauthorized, "请先登录")
			c.Abort()
			return
		}
		next := c.Request.URL.RequestURI()
		http.Redirect(c.Writer, c.Request, "/login?next="+url.QueryEscape(next), http.StatusSeeOther)
		c.Abort()
	}
}

func (a *App) requireAdminSession() gin.HandlerFunc {
	return func(c *gin.Context) {
		if a.auth == nil {
			c.Next()
			return
		}
		user, _ := c.Request.Context().Value(authUserKey).(*auth.User)
		if user != nil && user.Role == "admin" {
			c.Next()
			return
		}
		if isManagementAPI(c.Request.URL.Path) {
			writeError(c.Writer, http.StatusForbidden, "需要管理员权限")
			c.Abort()
			return
		}
		http.Redirect(c.Writer, c.Request, "/settings", http.StatusSeeOther)
		c.Abort()
	}
}

func isManagementAPI(path string) bool {
	for _, prefix := range []string{"/api/", "/accounts", "/qr", "/quick-login"} {
		if path == strings.TrimSuffix(prefix, "/") || strings.HasPrefix(path, prefix) {
			return true
		}
	}
	return false
}

func (a *App) handleLogin(w http.ResponseWriter, r *http.Request) {
	if a.auth == nil {
		http.Redirect(w, r, "/", http.StatusSeeOther)
		return
	}
	if r.Method == http.MethodGet {
		if token, _ := r.Cookie(sessionCookie); token != nil {
			if _, _, err := a.auth.UserBySession(r.Context(), token.Value); err == nil {
				http.Redirect(w, r, "/", http.StatusSeeOther)
				return
			}
		}
		serveFileOrText(w, r, filepath.Join(a.resources.Templates, "login.html"), fallbackLoginHTML)
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
		Next     string `json:"next"`
	}
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "请求格式错误")
		return
	}
	key := clientIP(r)
	if !a.allowLogin(key) {
		writeError(w, http.StatusTooManyRequests, "登录失败次数过多，请 15 分钟后重试")
		return
	}
	user, err := a.auth.Authenticate(r.Context(), body.Username, body.Password)
	if err != nil {
		a.recordLoginFailure(key)
		writeError(w, http.StatusUnauthorized, err.Error())
		return
	}
	a.clearLoginFailures(key)
	token, _, err := a.auth.CreateSession(r.Context(), user.ID, r.UserAgent(), clientIP(r), a.cfg.SessionDuration)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "创建登录会话失败")
		return
	}
	setSessionCookie(w, token, a.cfg.CookieSecure, a.cfg.SessionDuration)
	next := safeNext(body.Next)
	writeJSON(w, http.StatusOK, map[string]any{"user": user, "next": next})
}

func (a *App) handleRegister(w http.ResponseWriter, r *http.Request) {
	if a.auth == nil {
		http.Redirect(w, r, "/", http.StatusSeeOther)
		return
	}
	enabled, err := a.auth.RegistrationEnabled(r.Context())
	if err != nil {
		writeError(w, 500, "读取注册设置失败")
		return
	}
	if r.Method == http.MethodGet {
		if !enabled {
			http.Redirect(w, r, "/login?registration=disabled", http.StatusSeeOther)
			return
		}
		serveFileOrText(w, r, filepath.Join(a.resources.Templates, "register.html"), fallbackRegisterHTML)
		return
	}
	if r.Method != http.MethodPost {
		writeError(w, 405, "method not allowed")
		return
	}
	if !enabled {
		writeError(w, 403, "注册已关闭")
		return
	}
	var body struct{ Username, DisplayName, Password string }
	if err := decodeOptionalJSON(r, &body); err != nil {
		writeError(w, 400, "请求格式错误")
		return
	}
	user, err := a.auth.RegisterUser(r.Context(), body.Username, body.DisplayName, body.Password)
	if err != nil {
		writeError(w, 400, err.Error())
		return
	}
	token, _, err := a.auth.CreateSession(r.Context(), user.ID, r.UserAgent(), clientIP(r), a.cfg.SessionDuration)
	if err != nil {
		writeError(w, 500, "创建登录会话失败")
		return
	}
	setSessionCookie(w, token, a.cfg.CookieSecure, a.cfg.SessionDuration)
	writeJSON(w, 201, map[string]any{"user": user, "next": "/"})
}

func (a *App) handleLogout(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, 405, "method not allowed")
		return
	}
	if a.auth != nil {
		if cookie, err := r.Cookie(sessionCookie); err == nil {
			_ = a.auth.DeleteSession(r.Context(), cookie.Value)
		}
	}
	clearSessionCookie(w, a.cfg.CookieSecure)
	writeJSON(w, 200, map[string]any{"logged_out": true})
}

func (a *App) handleSettingsPage(w http.ResponseWriter, r *http.Request) {
	if a.auth == nil {
		http.Redirect(w, r, "/", http.StatusSeeOther)
		return
	}
	if r.Method != http.MethodGet {
		writeError(w, 405, "method not allowed")
		return
	}
	serveFileOrText(w, r, filepath.Join(a.resources.Templates, "settings.html"), fallbackSettingsHTML)
}
func (a *App) handleUsersPage(w http.ResponseWriter, r *http.Request) {
	if a.auth == nil {
		http.Redirect(w, r, "/", http.StatusSeeOther)
		return
	}
	if !requireAdmin(w, r) {
		return
	}
	if r.Method != http.MethodGet {
		writeError(w, 405, "method not allowed")
		return
	}
	serveFileOrText(w, r, filepath.Join(a.resources.Templates, "users.html"), fallbackUsersHTML)
}

func (a *App) handleAuthMe(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, 405, "method not allowed")
		return
	}
	if a.auth == nil {
		writeJSON(w, 200, map[string]any{
			"auth_enabled": false,
			"session_id":   "",
			"user": map[string]any{
				"username":     "local",
				"display_name": "本机管理员",
				"role":         "admin",
				"enabled":      true,
			},
		})
		return
	}
	user, session := currentAuth(r)
	if user == nil || session == nil {
		writeError(w, http.StatusUnauthorized, "请先登录")
		return
	}
	writeJSON(w, 200, map[string]any{"auth_enabled": true, "user": user, "session_id": session.ID})
}
func (a *App) handleProfile(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		writeError(w, 405, "method not allowed")
		return
	}
	user, _ := currentAuth(r)
	var body struct {
		DisplayName string `json:"display_name"`
	}
	if decodeOptionalJSON(r, &body) != nil {
		writeError(w, 400, "请求格式错误")
		return
	}
	updated, err := a.auth.UpdateProfile(r.Context(), user.ID, body.DisplayName)
	if err != nil {
		writeError(w, 400, err.Error())
		return
	}
	writeJSON(w, 200, updated)
}
func (a *App) handlePassword(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		writeError(w, 405, "method not allowed")
		return
	}
	user, session := currentAuth(r)
	var body struct {
		Current string `json:"current_password"`
		Next    string `json:"new_password"`
	}
	if decodeOptionalJSON(r, &body) != nil {
		writeError(w, 400, "请求格式错误")
		return
	}
	if err := a.auth.ChangePassword(r.Context(), user.ID, body.Current, body.Next, session.ID); err != nil {
		writeError(w, 400, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"updated": true})
}
func (a *App) handleSessions(w http.ResponseWriter, r *http.Request) {
	user, session := currentAuth(r)
	if r.Method == http.MethodGet {
		items, err := a.auth.ListSessions(r.Context(), user.ID)
		if err != nil {
			writeError(w, 500, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{"current_session_id": session.ID, "sessions": items})
		return
	}
	if r.Method == http.MethodDelete {
		if err := a.auth.DeleteOtherSessions(r.Context(), user.ID, session.ID); err != nil {
			writeError(w, 500, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{"deleted": true})
		return
	}
	writeError(w, 405, "method not allowed")
}

func (a *App) handleUsers(w http.ResponseWriter, r *http.Request) {
	if !requireAdmin(w, r) {
		return
	}
	if r.Method == http.MethodGet {
		users, err := a.auth.ListUsers(r.Context())
		if err != nil {
			writeError(w, 500, err.Error())
			return
		}
		counts, countErr := a.auth.AccountCounts(r.Context())
		if countErr != nil {
			writeError(w, 500, countErr.Error())
			return
		}
		out := make([]map[string]any, 0, len(users))
		for _, user := range users {
			out = append(out, map[string]any{
				"id": user.ID, "username": user.Username, "display_name": user.DisplayName,
				"role": user.Role, "enabled": user.Enabled, "last_login_at": user.LastLoginAt,
				"created_at": user.CreatedAt, "updated_at": user.UpdatedAt,
				"login_count": user.LoginCount, "account_count": counts[user.ID],
				"points": map[string]any{"enabled": false, "label": "无限制"},
			})
		}
		writeJSON(w, 200, out)
		return
	}
	if r.Method == http.MethodPost {
		var body struct {
			Username    string `json:"username"`
			DisplayName string `json:"display_name"`
			Password    string `json:"password"`
			Role        string `json:"role"`
		}
		if decodeOptionalJSON(r, &body) != nil {
			writeError(w, 400, "请求格式错误")
			return
		}
		user, err := a.auth.CreateUser(r.Context(), body.Username, body.DisplayName, body.Password, body.Role)
		if err != nil {
			writeError(w, 400, err.Error())
			return
		}
		writeJSON(w, 201, user)
		return
	}
	writeError(w, 405, "method not allowed")
}
func (a *App) handleUserAction(w http.ResponseWriter, r *http.Request) {
	if !requireAdmin(w, r) {
		return
	}
	actor, _ := currentAuth(r)
	parts := strings.Split(strings.Trim(r.URL.Path[len("/api/auth/users/"):], "/"), "/")
	if len(parts) != 2 {
		writeError(w, 404, "not found")
		return
	}
	id, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		writeError(w, 400, "无效用户 ID")
		return
	}
	switch parts[1] {
	case "accounts":
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		if _, err := a.auth.GetUser(r.Context(), id); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				writeError(w, http.StatusNotFound, "用户不存在")
			} else {
				writeError(w, http.StatusInternalServerError, err.Error())
			}
			return
		}
		accounts, err := a.db.ListAccounts(r.Context())
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		accounts, err = a.accountsOwnedBy(r.Context(), id, accounts)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		out := make([]store.AccountPublic, 0, len(accounts))
		for _, account := range accounts {
			out = append(out, account.Public())
		}
		writeJSON(w, http.StatusOK, map[string]any{"user_id": id, "accounts": out})
		return
	case "state":
		var body struct {
			Role    string `json:"role"`
			Enabled bool   `json:"enabled"`
		}
		if r.Method != http.MethodPut || decodeOptionalJSON(r, &body) != nil {
			writeError(w, 400, "请求格式错误")
			return
		}
		updated, err := a.auth.UpdateUser(r.Context(), actor.ID, id, body.Role, body.Enabled)
		if err != nil {
			writeError(w, 400, err.Error())
			return
		}
		writeJSON(w, 200, updated)
	case "password":
		var body struct {
			Password string `json:"password"`
		}
		if r.Method != http.MethodPut || decodeOptionalJSON(r, &body) != nil {
			writeError(w, 400, "请求格式错误")
			return
		}
		if err := a.auth.AdminResetPassword(r.Context(), id, body.Password); err != nil {
			writeError(w, 400, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{"updated": true})
	case "delete":
		if r.Method != http.MethodDelete {
			writeError(w, 405, "method not allowed")
			return
		}
		if err := a.auth.DeleteUser(r.Context(), actor.ID, id); err != nil {
			writeError(w, 400, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{"deleted": true})
	default:
		writeError(w, 404, "not found")
	}
}
func (a *App) handleRegistrationSetting(w http.ResponseWriter, r *http.Request) {
	if !requireAdmin(w, r) {
		return
	}
	if r.Method == http.MethodGet {
		enabled, err := a.auth.RegistrationEnabled(r.Context())
		if err != nil {
			writeError(w, 500, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{"enabled": enabled})
		return
	}
	if r.Method == http.MethodPut {
		var body struct {
			Enabled bool `json:"enabled"`
		}
		if decodeOptionalJSON(r, &body) != nil {
			writeError(w, 400, "请求格式错误")
			return
		}
		if err := a.auth.SetRegistrationEnabled(r.Context(), body.Enabled); err != nil {
			writeError(w, 500, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{"enabled": body.Enabled})
		return
	}
	writeError(w, 405, "method not allowed")
}

func currentAuth(r *http.Request) (*auth.User, *auth.Session) {
	user, _ := r.Context().Value(authUserKey).(*auth.User)
	session, _ := r.Context().Value(authSessionKey).(*auth.Session)
	return user, session
}
func requireAdmin(w http.ResponseWriter, r *http.Request) bool {
	user, _ := currentAuth(r)
	if user == nil || user.Role != "admin" {
		writeError(w, 403, "需要管理员权限")
		return false
	}
	return true
}
func setSessionCookie(w http.ResponseWriter, token string, secure bool, ttl time.Duration) {
	http.SetCookie(w, &http.Cookie{Name: sessionCookie, Value: token, Path: "/", MaxAge: int(ttl.Seconds()), HttpOnly: true, Secure: secure, SameSite: http.SameSiteLaxMode})
}
func clearSessionCookie(w http.ResponseWriter, secure bool) {
	http.SetCookie(w, &http.Cookie{Name: sessionCookie, Path: "/", MaxAge: -1, HttpOnly: true, Secure: secure, SameSite: http.SameSiteLaxMode})
}
func clientIP(r *http.Request) string {
	if value := strings.TrimSpace(strings.Split(r.Header.Get("X-Forwarded-For"), ",")[0]); value != "" {
		return value
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err == nil {
		return host
	}
	return r.RemoteAddr
}

func (a *App) allowLogin(key string) bool {
	a.loginMu.Lock()
	defer a.loginMu.Unlock()
	attempt, ok := a.loginAttempts[key]
	if !ok || time.Since(attempt.Start) >= 15*time.Minute {
		delete(a.loginAttempts, key)
		return true
	}
	return attempt.Failures < 8
}

func (a *App) recordLoginFailure(key string) {
	a.loginMu.Lock()
	defer a.loginMu.Unlock()
	attempt := a.loginAttempts[key]
	if attempt.Start.IsZero() || time.Since(attempt.Start) >= 15*time.Minute {
		attempt = loginAttempt{Start: time.Now()}
	}
	attempt.Failures++
	a.loginAttempts[key] = attempt
}

func (a *App) clearLoginFailures(key string) {
	a.loginMu.Lock()
	delete(a.loginAttempts, key)
	a.loginMu.Unlock()
}
func safeNext(value string) string {
	if strings.HasPrefix(value, "/") && !strings.HasPrefix(value, "//") {
		return value
	}
	return "/"
}
