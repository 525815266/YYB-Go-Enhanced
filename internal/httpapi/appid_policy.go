package httpapi

import (
	"errors"
	"fmt"
	"net/http"
	"strings"
)

// builtinDeniedAppIDs are AppIDs of website applications (the open platform
// kind behind qrconnect logins), not mini programs. A wx.login code request for
// one of them reaches the upstream and comes back without a code, and the
// account OAuth layer is refused within seconds:
//   - wx5cd60c5d4817a188 (Tencent PC Manager): on 2026-11-03 two healthy
//     accounts lost their authorization right after one such request, while the
//     four accounts the circuit breaker kept off the upstream stayed alive; one
//     request on a fresh authorization reproduced it, one mini program request
//     on another did not. See docs/website-appid-getcode-20261103.md.
//   - wx2f5d8f9715c59d10 (JD passport): the same kind of AppID. It never
//     returned a code and has not been tried on its own.
//
// YYB_CODE_ALLOW_APPIDS lifts a refusal for operators who know better.
var builtinDeniedAppIDs = map[string]string{
	"wx5cd60c5d4817a188": "腾讯电脑管家网站应用",
	"wx2f5d8f9715c59d10": "京东网页登录护照",
}

// appIDPolicy decides which AppIDs a code request may carry before anything is
// sent upstream. It is traffic policy only: a refusal never changes account
// status, clears credentials or touches the database.
type appIDPolicy struct {
	denied map[string]string // lower-case AppID to the reason shown to the caller
}

// newAppIDPolicy starts from the built-in list, adds the denials the operator
// configured and then removes everything the operator allows, so an allowance
// always wins.
func newAppIDPolicy(deny, allow []string) *appIDPolicy {
	p := &appIDPolicy{denied: map[string]string{}}
	for id, label := range builtinDeniedAppIDs {
		p.denied[id] = fmt.Sprintf("是网站应用而不是小程序（%s），用它取码会让账号授权失效；确需使用请在服务端设置 YYB_CODE_ALLOW_APPIDS", label)
	}
	for _, id := range deny {
		if id = normalizeAppID(id); id != "" {
			if _, builtin := p.denied[id]; !builtin {
				p.denied[id] = "已在服务端 YYB_CODE_DENY_APPIDS 中禁用"
			}
		}
	}
	for _, id := range allow {
		delete(p.denied, normalizeAppID(id))
	}
	return p
}

func normalizeAppID(appID string) string {
	return strings.ToLower(strings.TrimSpace(appID))
}

// check returns a non-nil error when a code request for appID must not reach
// the upstream. A nil policy allows everything.
func (p *appIDPolicy) check(appID string) *appIDDeniedError {
	if p == nil {
		return nil
	}
	why, denied := p.denied[normalizeAppID(appID)]
	if !denied {
		return nil
	}
	return &appIDDeniedError{AppID: strings.TrimSpace(appID), Why: why}
}

// appIDDeniedError is a local decision, never an upstream rejection: it carries
// no WeChat code and must not be mapped to account expiry or fed to the breaker.
type appIDDeniedError struct {
	AppID string
	Why   string
}

func (e *appIDDeniedError) Error() string {
	return fmt.Sprintf("取码已拒绝：%s %s，请求未发往上游", e.AppID, e.Why)
}

// writeIfAppIDDenied renders a refused AppID as HTTP 403, distinct from a
// held-off 429, an upstream 502 and a re-scan 409. It reports whether it
// handled the error.
func writeIfAppIDDenied(w http.ResponseWriter, err error) bool {
	var denied *appIDDeniedError
	if !errors.As(err, &denied) {
		return false
	}
	writeRawJSON(w, http.StatusForbidden, apiEnvelope{
		Code: http.StatusForbidden,
		Msg:  "call refused: " + denied.Error(),
		Data: map[string]any{"appid_denied": true, "app_id": denied.AppID, "request_rejected": false},
	})
	return true
}
