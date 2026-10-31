package protocol

import (
	"context"
	"fmt"
	"net/url"
	"strings"

	"yyb_go/internal/eventlog"
	"yyb_go/internal/store"
)

const oauthAuthorizeCmdID = 1254

var oauthAuthorizeRoute = []byte("/cgi-bin/mmbiz-bin/oauth_authorize")

// OAuthAuthorize obtains an official-account OAuth code through the authenticated
// WeChat session. The redirect URI is passed to WeChat unchanged: it is the
// OAuth callback target, not an article URL and must not go through geta8key.
//
// The shortlink ticket that backs the session is consumed by oauth_authorize,
// so the next call on the same session is always rejected with an application
// level -14. Callers that need repeated codes should use RecoverOAuthSession
// to rebuild the session in between.
func (p *Pool) OAuthAuthorize(ctx context.Context, loginBuffer, appID, redirectURI, scope, state, componentAppID string, accountID int64, tcpProxy string, fallbackDirect bool) (map[string]any, error) {
	return p.run(ctx, loginBuffer, accountID, tcpProxy, fallbackDirect, func(ctx context.Context, session WmpfSession) (map[string]any, error) {
		return p.oauthAuthorizeWithSession(ctx, session, appID, redirectURI, scope, state, componentAppID)
	})
}

// RecoverOAuthSession rebuilds the protocol session when an official-account
// OAuth request is rejected with an application level -14.
//
// The mini-program code path already does this through RecoverCodeSession.
// The OAuth path had no equivalent, so every account could obtain exactly one
// code and then failed with -14 forever. This method reuses the same saved
// ticket machinery: it logs in again with the saved ticket and only replaces
// the cached session after the new one produces a non-empty code for the same
// AppID. A failed recovery keeps the original cache and the original error.
func (p *Pool) RecoverOAuthSession(ctx context.Context, acc *store.WechatAccount, appID, redirectURI, scope, state, componentAppID, proxy string, fallbackDirect bool, original error, fetch func(context.Context) (string, error)) (map[string]any, error) {
	return p.recoverCodeSession(ctx, acc, appID, proxy, original, func(ctx context.Context) (WmpfSession, map[string]any, error) {
		if acc.SessionOnly() {
			saved, err := p.nativeRecoveryState(ctx, acc.ID, proxy)
			if err != nil {
				return WmpfSession{}, nil, ErrCachedSessionUnavailable
			}
			st, err := p.restoreNativeSession(ctx, acc, saved)
			if err != nil {
				return WmpfSession{}, nil, err
			}
			result, err := p.oauthAuthorizeWithSession(ctx, st, appID, redirectURI, scope, state, componentAppID)
			return st, result, err
		}
		return recoverCodeWithTickets(ctx, acc.LoginBuffer, func(ctx context.Context, buffer string) (WmpfSession, error) {
			if err := acquire(ctx, p.loginSem); err != nil {
				return WmpfSession{}, err
			}
			defer release(p.loginSem)
			return p.loginAndSession(ctx, buffer, proxy, fallbackDirect)
		}, func(ctx context.Context) (string, error) {
			p.events.Emit("protocol.ticket_requested", eventlog.ForAccount(acc))
			return fetch(ctx)
		}, func(ctx context.Context, st WmpfSession) (map[string]any, error) {
			return p.oauthAuthorizeWithSession(ctx, st, appID, redirectURI, scope, state, componentAppID)
		})
	})
}

// oauthAuthorizeWithSession runs one oauth_authorize round on an explicit session.
func (p *Pool) oauthAuthorizeWithSession(ctx context.Context, session WmpfSession, appID, redirectURI, scope, state, componentAppID string) (map[string]any, error) {
	authorizeURL := buildOAuthAuthorizeURL(appID, redirectURI, defaultOAuthScope(scope), defaultOAuthState(state), componentAppID)
	request := buildOAuthAuthorizeRequest(authorizeURL)

	plain := buildOAuthTransferPlaintext(appID, request, session.Session.HostAppID)
	envelope, err := buildTransferPacket(session.Session, plain)
	if err != nil {
		return nil, err
	}
	_, response, err := p.sendEnvelope(ctx, session, envelope)
	if err != nil {
		return nil, err
	}

	payload := findOAuthPayload(response, 0)
	if len(payload) == 0 {
		return nil, fmt.Errorf("oauth_authorize returned an empty payload")
	}
	if ret, message := parseOAuthBaseResponse(oauthFirstBytes(payload, 1)); ret != 0 {
		return nil, fmt.Errorf("oauth_authorize ret=%d: %s", ret, message)
	}

	fullURL := string(oauthFirstBytes(payload, 6))
	code := oauthQueryValue(fullURL, "code")
	return map[string]any{
		"code":     code,
		"url":      authorizeURL,
		"full_url": fullURL,
	}, nil
}

func defaultOAuthScope(scope string) string {
	if strings.TrimSpace(scope) == "" {
		return "snsapi_base"
	}
	return strings.TrimSpace(scope)
}

func defaultOAuthState(state string) string {
	if strings.TrimSpace(state) == "" {
		return "STATE"
	}
	return strings.TrimSpace(state)
}

func buildOAuthAuthorizeURL(appID, redirectURI, scope, state, componentAppID string) string {
	pairs := [][2]string{
		{"appid", appID},
		{"redirect_uri", redirectURI},
		{"response_type", "code"},
		{"scope", scope},
		{"state", state},
	}
	if strings.TrimSpace(componentAppID) != "" {
		pairs = append(pairs, [2]string{"component_appid", strings.TrimSpace(componentAppID)})
	}
	var query strings.Builder
	for index, pair := range pairs {
		if index > 0 {
			query.WriteByte('&')
		}
		query.WriteString(url.QueryEscape(pair[0]))
		query.WriteByte('=')
		query.WriteString(url.QueryEscape(pair[1]))
	}
	return "https://open.weixin.qq.com/connect/oauth2/authorize?" + query.String() + "#wechat_redirect"
}

func buildOAuthAuthorizeRequest(authorizeURL string) []byte {
	body := pbLen(1, oauthBaseRequest())
	body = append(body, pbLen(2, []byte(authorizeURL))...)
	body = append(body, pbLen(3, nil)...)
	body = append(body, pbVar(4, 4)...)
	return body
}

// oauthBaseRequest is the placeholder identity expected by oauth_authorize.
// The authenticated identity is carried by the surrounding WMPF transfer.
func oauthBaseRequest() []byte {
	body := pbLen(1, nil)
	body = append(body, pbVar(2, 0)...)
	body = append(body, pbLen(3, nil)...)
	body = append(body, pbVar(4, 0)...)
	body = append(body, pbLen(5, []byte("Windows"))...)
	body = append(body, pbVar(6, 0)...)
	return body
}

func oauthOuterSessionContext() []byte {
	body := pbLen(1, sessionKeyLiteral)
	body = append(body, pbVar(2, 145715353)...)
	body = append(body, pbLen(3, []byte("34-5A-60-63-65-E6"))...)
	body = append(body, pbVar(4, 1661404927)...)
	body = append(body, pbLen(5, windowsName)...)
	body = append(body, pbVar(6, 0)...)
	return body
}

func buildOAuthTransferPlaintext(appID string, request, hostAppID []byte) []byte {
	if len(hostAppID) == 0 {
		hostAppID = hostAppIDDefault
	}
	body := pbLen(1, oauthOuterSessionContext())
	body = append(body, pbLen(2, oauthAuthorizeRoute)...)
	body = append(body, pbLen(3, hostAppID)...)
	body = append(body, pbVar(4, 5)...)
	body = append(body, pbLen(5, request)...)
	body = append(body, pbLen(6, []byte(appID))...)
	body = append(body, pbVar(7, oauthAuthorizeCmdID)...)
	body = append(body, pbVar(8, 1610627409)...)
	body = append(body, pbLen(9, windowsPlugin)...)
	body = append(body, pbVar(10, 573651281)...)
	return body
}

type oauthPBField struct {
	number int
	wire   int
	value  uint64
	bytes  []byte
}

func scanOAuthPB(data []byte) []oauthPBField {
	var fields []oauthPBField
	for offset := 0; offset < len(data); {
		tag, next, err := readVarint(data, offset)
		if err != nil {
			break
		}
		offset = next
		field := oauthPBField{number: int(tag >> 3), wire: int(tag & 7)}
		switch field.wire {
		case 0:
			field.value, offset, err = readVarint(data, offset)
		case 2:
			var length uint64
			length, offset, err = readVarint(data, offset)
			if err == nil && uint64(len(data)-offset) >= length {
				field.bytes = append([]byte(nil), data[offset:offset+int(length)]...)
				offset += int(length)
			} else if err == nil {
				err = fmt.Errorf("protobuf field exceeds payload")
			}
		default:
			err = fmt.Errorf("unsupported protobuf wire type %d", field.wire)
		}
		if err != nil {
			break
		}
		fields = append(fields, field)
	}
	return fields
}

func oauthFirstBytes(data []byte, number int) []byte {
	for _, field := range scanOAuthPB(data) {
		if field.number == number && field.wire == 2 {
			return field.bytes
		}
	}
	return nil
}

func oauthFirstVar(data []byte, number int) uint64 {
	for _, field := range scanOAuthPB(data) {
		if field.number == number && field.wire == 0 {
			return field.value
		}
	}
	return 0
}

func parseOAuthBaseResponse(data []byte) (int32, string) {
	if len(data) == 0 {
		return 0, ""
	}
	return int32(oauthFirstVar(data, 1)), string(oauthFirstBytes(oauthFirstBytes(data, 2), 1))
}

func findOAuthPayload(data []byte, depth int) []byte {
	if len(data) == 0 || depth > 8 {
		return nil
	}
	base := oauthFirstBytes(data, 1)
	ret, _ := parseOAuthBaseResponse(base)
	redirectURL := string(oauthFirstBytes(data, 6))
	if strings.HasPrefix(redirectURL, "http://") || strings.HasPrefix(redirectURL, "https://") || (len(base) > 0 && ret != 0) {
		return data
	}
	fields := scanOAuthPB(data)
	for index := len(fields) - 1; index >= 0; index-- {
		if fields[index].wire != 2 {
			continue
		}
		if payload := findOAuthPayload(fields[index].bytes, depth+1); len(payload) > 0 {
			return payload
		}
	}
	return nil
}

func oauthQueryValue(rawURL, key string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	return parsed.Query().Get(key)
}
