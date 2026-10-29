package httpapi

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"strconv"

	"yyb_go/internal/auth"
)

const scanDefaultKey = "scan_default_product"

func scanUserKey(id int64) string { return "scan_default_user_" + strconv.FormatInt(id, 10) }
func (a *App) scanDefaults(ctx context.Context, userID int64) (string, string, error) {
	global, err := a.db.GetSetting(ctx, scanDefaultKey)
	if errors.Is(err, sql.ErrNoRows) {
		global = "appstore"
	} else if err != nil {
		return "", "", err
	}
	global, err = normalizeLoginProduct(global)
	if err != nil {
		return "", "", err
	}
	personal := ""
	if userID > 0 {
		personal, err = a.db.GetSetting(ctx, scanUserKey(userID))
		if errors.Is(err, sql.ErrNoRows) {
			personal = ""
		} else if err != nil {
			return "", "", err
		}
		if personal != "" {
			if _, err = normalizeLoginProduct(personal); err != nil {
				return "", "", err
			}
		}
	}
	return global, personal, nil
}
func (a *App) scanProduct(r *http.Request, explicit string) (string, error) {
	if explicit != "" {
		return normalizeLoginProduct(explicit)
	}
	var id int64
	if u, _ := r.Context().Value(authUserKey).(*auth.User); u != nil {
		id = u.ID
	}
	global, personal, err := a.scanDefaults(r.Context(), id)
	if err != nil {
		return "", err
	}
	if personal != "" {
		return personal, nil
	}
	return global, nil
}
func (a *App) handleScanSettings(w http.ResponseWriter, r *http.Request) {
	user, _ := r.Context().Value(authUserKey).(*auth.User)
	id := int64(0)
	admin := a.auth == nil
	if user != nil {
		id = user.ID
		admin = user.Role == "admin"
	}
	if r.Method == http.MethodPut {
		var body struct {
			Product string `json:"product"`
			Scope   string `json:"scope"`
		}
		if err := decodeOptionalJSON(r, &body); err != nil {
			writeError(w, 400, "请求格式错误")
			return
		}
		if body.Scope != "personal" && body.Scope != "global" {
			writeError(w, 400, "请选择个人或系统默认")
			return
		}
		if body.Scope == "global" && !admin {
			writeError(w, 403, "只有管理员可以设置系统默认渠道")
			return
		}
		if body.Scope == "personal" && id <= 0 {
			writeError(w, 400, "本机模式请设置系统默认渠道")
			return
		}
		value := body.Product
		if body.Scope == "global" || value != "" {
			var err error
			value, err = normalizeLoginProduct(value)
			if err != nil {
				writeError(w, 422, err.Error())
				return
			}
		}
		key := scanDefaultKey
		if body.Scope == "personal" {
			key = scanUserKey(id)
		}
		if err := a.db.SetSetting(r.Context(), key, value); err != nil {
			writeError(w, 500, "保存默认渠道失败")
			return
		}
	} else if r.Method != http.MethodGet {
		writeError(w, 405, "method not allowed")
		return
	}
	global, personal, err := a.scanDefaults(r.Context(), id)
	if err != nil {
		writeError(w, 500, fmt.Sprint(err))
		return
	}
	effective := global
	if personal != "" {
		effective = personal
	}
	writeJSON(w, 200, map[string]any{"global": global, "personal": personal, "effective": effective, "can_manage_global": admin, "has_personal_account": id > 0})
}
