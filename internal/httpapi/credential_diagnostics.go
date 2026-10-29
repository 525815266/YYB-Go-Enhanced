package httpapi

import (
	"log"
	"net/http"
	"regexp"
	"strconv"

	"yyb_go/internal/protocol"
)

// Only typed business rejections produce public diagnostics. Never copy an
// error string: it may include credentials, an OpenID or proxy authentication.
type upstreamDiagnostic struct {
	Stage        string `json:"stage"`
	UpstreamCode int64  `json:"upstream_code"`
	WeChatCode   int    `json:"wechat_code,omitempty"`
}

var diagnosticWeChatCode = regexp.MustCompile(`(?i)(?:^|(?:\berror\b|\bcode\b)\s*[:=]?\s*|\[)(40030|42002|42007|40188)(?:$|[\s\]|,;])`)

func credentialDiagnostics(err error) []upstreamDiagnostic {
	out := make([]upstreamDiagnostic, 0)
	seen := make(map[upstreamDiagnostic]bool)
	var visit func(error)
	visit = func(err error) {
		if err == nil {
			return
		}
		var item upstreamDiagnostic
		var message string
		switch e := err.(type) {
		case *protocol.RefreshRejectedError:
			item.Stage = "refresh_token"
			if e.Operation == "check_login" {
				item.Stage = "check_login"
			}
			item.UpstreamCode, message = int64(e.Code), e.Message
		case *protocol.LoginBufferRejectedError:
			item.Stage, item.UpstreamCode, message = "login_buffer", int64(e.Code), e.Message
		case *protocol.ManualAuthRejectedError:
			item.Stage, item.UpstreamCode = "manual_auth", e.Code
		case *protocol.ShortlinkRejectedError:
			item.Stage, item.UpstreamCode = "shortlink_application", e.Code
			if e.Transport {
				item.Stage = "shortlink_transport"
			}
		}
		if item.Stage != "" {
			if match := diagnosticWeChatCode.FindStringSubmatch(message); len(match) > 1 {
				item.WeChatCode, _ = strconv.Atoi(match[1])
			}
			if !seen[item] {
				seen[item] = true
				out = append(out, item)
			}
		}
		switch e := err.(type) {
		case interface{ Unwrap() []error }:
			for _, child := range e.Unwrap() {
				visit(child)
			}
		case interface{ Unwrap() error }:
			visit(e.Unwrap())
		}
	}
	visit(err)
	return out
}

func writeAccountExpired(w http.ResponseWriter, err accountExpiredError) {
	diagnostics := credentialDiagnostics(err.cause)
	if len(diagnostics) > 0 {
		log.Printf("protocol: account id=%d credential recovery rejected: %+v", err.accountID, diagnostics)
	}
	writeRawJSON(w, http.StatusConflict, apiEnvelope{
		Code: http.StatusConflict,
		Msg:  "account login_buffer expired (refresh failed); re-scan required",
		Data: map[string]any{"rescan_required": true, "upstream_errors": diagnostics},
	})
}
