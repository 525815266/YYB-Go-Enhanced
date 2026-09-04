package httpapi

func wxAliasOperation(summary, requestSchema, responseSchema string) map[string]any {
	return map[string]any{
		"post": openAPIOperation(
			[]string{"wx"}, summary, nil,
			jsonRequestBody(refSchema(requestSchema)),
			defaulted(map[string]any{"200": jsonResponse(summary+"。", refSchema(responseSchema))}),
		),
	}
}

func newOpenAPISpec() map[string]any {
	return map[string]any{
		"openapi": "3.0.3",
		"info": map[string]any{
			"title":       "YYB Go 接口文档",
			"description": "用于微信扫码登录、账号管理和 wxapp 接口调用的 API。",
			"version":     "1.0.0",
		},
		"servers": []map[string]any{
			{"url": "/"},
		},
		"tags": []map[string]any{
			{"name": "health", "description": "服务健康检查"},
			{"name": "qr", "description": "微信扫码登录"},
			{"name": "quick-login", "description": "桌面微信快速授权"},
			{"name": "accounts", "description": "已保存的微信账号"},
			{"name": "proxy-profiles", "description": "可复用的品赞代理配置与地区"},
			{"name": "qinglong", "description": "账号级自动化面板任务与推送管理（兼容青龙、呆呆和 Arcadia）"},
			{"name": "wxapp", "description": "wxapp 业务接口调用"},
			{"name": "wx", "description": "兼容 /wx/* 的微信业务接口"},
			{"name": "oauth", "description": "微信公众号网页授权链接"},
		},
		"paths": map[string]any{
			"/health": map[string]any{
				"get": openAPIOperation(
					[]string{"health"},
					"检查服务状态",
					nil,
					nil,
					defaulted(map[string]any{
						"200": jsonResponse("服务正常。", refSchema("HealthResponse")),
					}),
				),
			},
			"/qr": map[string]any{
				"post": openAPIOperation(
					[]string{"qr"},
					"创建扫码登录会话",
					[]map[string]any{
						boolQueryParam("as_base64", "是否同时返回二维码图片的 data URI。"),
					},
					jsonOptionalRequestBody(refSchema("ProxySpec")),
					defaulted(map[string]any{
						"200": jsonResponse("二维码会话创建成功。", refSchema("QRCreateResponse")),
					}),
				),
			},
			"/qr/{session_id}/image": map[string]any{
				"get": openAPIOperation(
					[]string{"qr"},
					"获取二维码图片",
					[]map[string]any{pathStringParam("session_id", "二维码会话 ID。")},
					nil,
					defaulted(map[string]any{
						"200": imageResponse("二维码图片。"),
					}),
				),
			},
			"/qr/{session_id}/poll": map[string]any{
				"get": openAPIOperation(
					[]string{"qr"},
					"轮询扫码登录状态",
					[]map[string]any{pathStringParam("session_id", "二维码会话 ID。")},
					nil,
					defaulted(map[string]any{
						"200": jsonResponse("当前扫码状态。", refSchema("QRPollResponse")),
					}),
				),
			},
			"/qr/{session_id}/cancel": map[string]any{
				"post": openAPIOperation(
					[]string{"qr"},
					"取消扫码登录会话",
					[]map[string]any{pathStringParam("session_id", "二维码会话 ID。")},
					nil,
					defaulted(map[string]any{
						"200": jsonResponse("扫码会话已取消。", freeFormObjectSchema("取消结果。")),
					}),
				),
			},
			"/qr/{session_id}/confirm": map[string]any{
				"post": openAPIOperation(
					[]string{"qr"},
					"确认已授权的扫码会话并保存账号",
					[]map[string]any{pathStringParam("session_id", "二维码会话 ID。")},
					nil,
					defaulted(map[string]any{
						"200": jsonResponse("已保存的账号信息。", refSchema("AccountPublic")),
					}),
				),
			},
			"/quick-login": map[string]any{
				"post": openAPIOperation(
					[]string{"quick-login"},
					"创建桌面微信快速授权会话",
					nil,
					jsonOptionalRequestBody(refSchema("ProxySpec")),
					defaulted(map[string]any{
						"200": jsonResponse("快速授权参数。", refSchema("QuickLoginCreateResponse")),
					}),
				),
			},
			"/quick-login/{session_id}/confirm": map[string]any{
				"post": openAPIOperation(
					[]string{"quick-login"},
					"确认桌面微信授权并保存账号",
					[]map[string]any{pathStringParam("session_id", "快速授权会话 ID。")},
					jsonRequestBody(refSchema("QuickLoginConfirmRequest")),
					defaulted(map[string]any{
						"200": jsonResponse("已保存的账号信息。", refSchema("AccountPublic")),
					}),
				),
			},
			"/accounts": map[string]any{
				"get": openAPIOperation(
					[]string{"accounts"},
					"获取账号列表",
					nil,
					nil,
					defaulted(map[string]any{
						"200": jsonResponse("已保存的账号列表。", arraySchema(refSchema("AccountPublic"))),
					}),
				),
				"delete": openAPIOperation(
					[]string{"accounts"},
					"删除账号",
					[]map[string]any{queryStringParam("ref", "账号 ID、UIN 或 openid。", true)},
					nil,
					defaulted(map[string]any{
						"200": jsonResponse("删除结果。", refSchema("DeleteAccountResponse")),
					}),
				),
			},
			"/accounts/repair": map[string]any{
				"post": openAPIOperation(
					[]string{"accounts"},
					"预览或清理未完成扫码残留",
					nil,
					jsonOptionalRequestBody(freeFormObjectSchema("confirm=true 执行清理；省略或 false 仅预览。")),
					defaulted(map[string]any{
						"200": jsonResponse("账号整理结果。", freeFormObjectSchema("清理结果。")),
					}),
				),
			},
			"/accounts/refresh": map[string]any{
				"post": openAPIOperation(
					[]string{"accounts"},
					"刷新账号存活状态",
					nil,
					jsonOptionalRequestBody(refSchema("AccountRefRequest")),
					defaulted(map[string]any{
						"200": jsonResponse("刷新结果。未传 ref 时返回数组。", refSchema("RefreshResponse")),
					}),
				),
			},
			"/accounts/resync": map[string]any{
				"post": openAPIOperation(
					[]string{"accounts"},
					"重新同步账号资料",
					nil,
					jsonOptionalRequestBody(refSchema("AccountRefRequest")),
					defaulted(map[string]any{
						"200": jsonResponse("同步后的账号信息。未传 ref 时返回数组。", refSchema("ResyncResponse")),
					}),
				),
			},
			"/accounts/avatar": map[string]any{
				"get": openAPIOperation(
					[]string{"accounts"},
					"获取账号头像",
					[]map[string]any{queryStringParam("ref", "账号 ID、UIN 或 openid。", true)},
					nil,
					defaulted(map[string]any{
						"200": imageResponse("头像图片。"),
						"302": map[string]any{"description": "跳转到远程头像地址。"},
					}),
				),
			},
			"/accounts/remark": map[string]any{
				"put": openAPIOperation(
					[]string{"accounts"}, "保存账号备注", nil,
					jsonRequestBody(refSchema("AccountRemarkRequest")),
					defaulted(map[string]any{"200": jsonResponse("备注保存结果。", freeFormObjectSchema("账号及青龙任务名称更新状态。"))}),
				),
			},
			"/accounts/proxy": map[string]any{
				"get": openAPIOperation(
					[]string{"accounts"}, "读取账号代理配置",
					[]map[string]any{queryStringParam("ref", "账号 ID、UIN 或 openid。", true)}, nil,
					defaulted(map[string]any{"200": jsonResponse("账号代理配置。", refSchema("AccountProxySetting"))}),
				),
				"put": openAPIOperation(
					[]string{"accounts"}, "保存账号代理配置", nil,
					jsonRequestBody(refSchema("AccountProxyRequest")),
					defaulted(map[string]any{"200": jsonResponse("保存后的账号代理配置。", refSchema("AccountProxySetting"))}),
				),
			},
			"/accounts/proxy/test": map[string]any{
				"post": openAPIOperation(
					[]string{"accounts"}, "提取并校验代理配置", nil,
					jsonRequestBody(refSchema("AccountProxyTestRequest")),
					defaulted(map[string]any{"200": jsonResponse("代理解析结果，不返回认证信息。", refSchema("AccountProxyTestResponse"))}),
				),
			},
			"/api/proxy-profiles": map[string]any{
				"get": openAPIOperation(
					[]string{"proxy-profiles"}, "列出品赞代理配置", nil, nil,
					defaulted(map[string]any{"200": jsonResponse("品赞代理配置列表。", arraySchema(refSchema("ProxyProviderProfile")))}),
				),
				"post": openAPIOperation(
					[]string{"proxy-profiles"}, "添加品赞代理配置", nil,
					jsonRequestBody(refSchema("ProxyProviderProfileRequest")),
					defaulted(map[string]any{"201": jsonResponse("新建的品赞代理配置。", refSchema("ProxyProviderProfile"))}),
				),
			},
			"/api/proxy-profiles/{id}": map[string]any{
				"put": openAPIOperation(
					[]string{"proxy-profiles"}, "更新品赞代理配置", []map[string]any{pathStringParam("id", "代理配置 ID。")},
					jsonRequestBody(refSchema("ProxyProviderProfileRequest")),
					defaulted(map[string]any{"200": jsonResponse("更新后的品赞代理配置。", refSchema("ProxyProviderProfile"))}),
				),
				"delete": openAPIOperation(
					[]string{"proxy-profiles"}, "删除未被账号使用的品赞代理配置", []map[string]any{pathStringParam("id", "代理配置 ID。")}, nil,
					defaulted(map[string]any{"200": jsonResponse("删除结果。", freeFormObjectSchema("已删除的配置 ID。"))}),
				),
			},
			"/api/proxy-profiles/areas/provinces": map[string]any{
				"get": openAPIOperation([]string{"proxy-profiles"}, "列出品赞支持的省份", nil, nil,
					defaulted(map[string]any{"200": jsonResponse("省份列表。", arraySchema(refSchema("ProxyArea")))})),
			},
			"/api/proxy-profiles/areas/cities": map[string]any{
				"get": openAPIOperation([]string{"proxy-profiles"}, "列出省份下的城市",
					[]map[string]any{queryStringParam("province", "6 位省份编码。", true)}, nil,
					defaulted(map[string]any{"200": jsonResponse("城市列表。", arraySchema(refSchema("ProxyArea")))})),
			},
			"/api/proxy-location/recommend": map[string]any{
				"post": openAPIOperation([]string{"proxy-profiles"}, "根据手机定位或公网 IP 推荐代理地区", nil,
					jsonOptionalRequestBody(refSchema("ProxyLocationRequest")),
					defaulted(map[string]any{"200": jsonResponse("匹配到供应商省市编码的推荐地区，仅用于预填。", refSchema("ProxyLocationResponse"))})),
			},
			"/api/qinglong/status": map[string]any{
				"get": openAPIOperation(
					[]string{"qinglong"}, "检查自动化面板连接状态", nil, nil,
					defaulted(map[string]any{"200": jsonResponse("面板配置和连接状态。", refSchema("QingLongStatus"))}),
				),
			},
			"/api/qinglong/config": map[string]any{
				"get": openAPIOperation(
					[]string{"qinglong"}, "读取自动化面板连接配置", nil, nil,
					defaulted(map[string]any{"200": jsonResponse("不包含 Client Secret 明文。", refSchema("QingLongConfig"))}),
				),
				"put": openAPIOperation(
					[]string{"qinglong"}, "测试并保存自动化面板连接配置", nil,
					jsonRequestBody(refSchema("QingLongConfigRequest")),
					defaulted(map[string]any{"200": jsonResponse("连接测试及保存结果。", refSchema("QingLongConfig"))}),
				),
			},
			"/api/qinglong/sync": map[string]any{
				"post": openAPIOperation(
					[]string{"qinglong"}, "将账号加入面板 YYB_SERVER", nil,
					jsonRequestBody(refSchema("AccountRefRequest")),
					defaulted(map[string]any{"200": jsonResponse("幂等同步结果。", refSchema("QingLongSyncResponse"))}),
				),
			},
			"/api/qinglong/jobs": map[string]any{
				"get": openAPIOperation(
					[]string{"qinglong"}, "获取账号的兼容脚本任务",
					[]map[string]any{queryStringParam("ref", "账号 ID、UIN 或 openid。", true)}, nil,
					defaulted(map[string]any{"200": jsonResponse("账号任务列表。", refSchema("AccountJobsResponse"))}),
				),
			},
			"/api/qinglong/jobs/enable": map[string]any{
				"put": openAPIOperation(
					[]string{"qinglong"}, "启用或停用账号脚本定时任务", nil,
					jsonRequestBody(refSchema("JobActionRequest")),
					defaulted(map[string]any{"200": jsonResponse("任务开关结果。", freeFormObjectSchema("任务状态。"))}),
				),
			},
			"/api/qinglong/jobs/run": map[string]any{
				"post": openAPIOperation(
					[]string{"qinglong"}, "立即运行账号脚本一次", nil,
					jsonRequestBody(refSchema("JobRunRequest")),
					defaulted(map[string]any{"202": jsonResponse("任务已提交到青龙。", freeFormObjectSchema("任务提交状态。"))}),
				),
			},
			"/api/qinglong/jobs/log": map[string]any{
				"get": openAPIOperation(
					[]string{"qinglong"}, "获取账号脚本最近日志",
					[]map[string]any{
						queryStringParam("ref", "账号 ID、UIN 或 openid。", true),
						queryStringParam("script_key", "订阅仓库内的受支持脚本路径。", true),
					}, nil,
					defaulted(map[string]any{"200": jsonResponse("最近运行日志。", refSchema("JobLogResponse"))}),
				),
			},
			"/api/qinglong/runs": map[string]any{
				"get": openAPIOperation(
					[]string{"qinglong"}, "获取账号的历史运行日志",
					[]map[string]any{queryStringParam("ref", "账号 ID、UIN 或 openid。", true)}, nil,
					defaulted(map[string]any{"200": jsonResponse("只包含当前账号专属任务的日志。", refSchema("AccountRunsResponse"))}),
				),
			},
			"/api/qinglong/runs/log": map[string]any{
				"get": openAPIOperation(
					[]string{"qinglong"}, "读取账号的一条历史运行日志",
					[]map[string]any{
						queryStringParam("ref", "账号 ID、UIN 或 openid。", true),
						queryStringParam("log_key", "账号运行日志列表返回的日志键。", true),
					}, nil,
					defaulted(map[string]any{"200": jsonResponse("日志正文。", refSchema("AccountRunLogResponse"))}),
				),
				"post": openAPIOperation(
					[]string{"qinglong"}, "读取账号的一条运行日志（代理兼容）", nil,
					jsonRequestBody(refSchema("AccountRunLogRequest")),
					defaulted(map[string]any{"200": jsonResponse("日志正文。", refSchema("AccountRunLogResponse"))}),
				),
			},
			"/api/qinglong/push": map[string]any{
				"get": openAPIOperation(
					[]string{"qinglong"}, "获取账号推送设置",
					[]map[string]any{queryStringParam("ref", "账号 ID、UIN 或 openid。", true)}, nil,
					defaulted(map[string]any{"200": jsonResponse("推送配置状态，不返回密钥。", refSchema("PushSetting"))}),
				),
				"put": openAPIOperation(
					[]string{"qinglong"}, "保存账号推送设置", nil,
					jsonRequestBody(refSchema("PushSettingRequest")),
					defaulted(map[string]any{"200": jsonResponse("保存后的推送配置状态。", refSchema("PushSetting"))}),
				),
			},
			"/wx/oauth": map[string]any{
				"post": openAPIOperation(
					[]string{"oauth"},
					"生成微信公众号网页授权链接",
					nil,
					jsonRequestBody(objectSchema([]string{"ref", "appid", "redirect_uri"}, map[string]any{
						"ref":             map[string]any{"type": "string"},
						"appid":           map[string]any{"type": "string", "description": "18 位公众号 AppID"},
						"redirect_uri":    map[string]any{"type": "string", "format": "uri"},
						"scope":           map[string]any{"type": "string", "enum": []string{"snsapi_base", "snsapi_userinfo"}},
						"state":           map[string]any{"type": "string"},
						"component_appid": map[string]any{"type": "string"},
					})),
					defaulted(map[string]any{
						"200": jsonResponse("生成授权链接；用户授权后 code 会回传到 redirect_uri。", freeFormObjectSchema("公众号网页授权结果")),
					}),
				),
			},
			"/wx/code": wxAliasOperation("获取小程序 code（兼容入口）", "WxappRequest", "WxappResponse"),
			"/wx/getuserinfo": map[string]any{
				"get": openAPIOperation([]string{"wx"}, "获取 YYB 账号用户信息", []map[string]any{queryStringParam("ref", "账号 ID、UIN 或 openid。", true)}, nil,
					defaulted(map[string]any{"200": jsonResponse("用户信息。", freeFormObjectSchema("用户信息结果"))})),
				"post": openAPIOperation([]string{"wx"}, "获取 YYB 账号用户信息", nil, jsonRequestBody(refSchema("AccountRefRequest")),
					defaulted(map[string]any{"200": jsonResponse("用户信息。", freeFormObjectSchema("用户信息结果"))})),
			},
			"/wx/encryptkey":       wxAliasOperation("加密能力兼容转发（需要真实 payload）", "OperateWXDataRequest", "WxappResponse"),
			"/wx/getlatestuserkey": wxAliasOperation("getLatestUserKey 加密密钥转发（需要真实 payload）", "OperateWXDataRequest", "WxappResponse"),
			"/wx/getphonenumber":   wxAliasOperation("获取手机号（兼容入口）", "WxappRequest", "WxappResponse"),
			"/wx/cloud":            wxAliasOperation("云函数/通用 operateWxData 兼容入口", "OperateWXDataRequest", "WxappResponse"),
			"/wx/qrcodeauth": map[string]any{
				"post": openAPIOperation([]string{"qr"}, "创建二维码授权会话", nil, jsonOptionalRequestBody(refSchema("ProxySpec")),
					defaulted(map[string]any{"200": jsonResponse("二维码授权会话。", refSchema("QRCreateResponse"))})),
			},
			"/wx/mpgeta8key": wxAliasOperation("文章会话兼容入口", "OperateWXDataRequest", "WxappResponse"),
			"/wx/appmsgext":  wxAliasOperation("文章扩展数据兼容入口", "OperateWXDataRequest", "WxappResponse"),
			"/wx/appmsglike": wxAliasOperation("文章点赞兼容入口", "OperateWXDataRequest", "WxappResponse"),
			"/wxapp/getCode": map[string]any{
				"post": openAPIOperation(
					[]string{"wxapp"},
					"获取小程序code",
					nil,
					jsonRequestBody(refSchema("WxappRequest")),
					defaulted(map[string]any{
						"200": jsonResponse("getCode 调用结果。", refSchema("WxappResponse")),
					}),
				),
			},
			"/wx/qrcodeauth/{session_id}/image": map[string]any{
				"get": openAPIOperation([]string{"qr"}, "获取兼容二维码图片", []map[string]any{pathStringParam("session_id", "二维码会话 ID。")}, nil,
					defaulted(map[string]any{"200": imageResponse("二维码图片。")})),
			},
			"/wx/qrcodeauth/{session_id}/poll": map[string]any{
				"get": openAPIOperation([]string{"qr"}, "轮询兼容二维码状态", []map[string]any{pathStringParam("session_id", "二维码会话 ID。")}, nil,
					defaulted(map[string]any{"200": jsonResponse("扫码状态。", refSchema("QRPollResponse"))})),
			},
			"/wx/qrcodeauth/{session_id}/confirm": map[string]any{
				"post": openAPIOperation([]string{"qr"}, "确认兼容二维码授权", []map[string]any{pathStringParam("session_id", "二维码会话 ID。")}, nil,
					defaulted(map[string]any{"200": jsonResponse("已保存的账号。", refSchema("AccountPublic"))})),
			},
			"/wxapp/getPhoneNumber": map[string]any{
				"post": openAPIOperation(
					[]string{"wxapp"},
					"获取手机号",
					nil,
					jsonRequestBody(refSchema("WxappRequest")),
					defaulted(map[string]any{
						"200": jsonResponse("getPhoneNumber 调用结果。", refSchema("WxappResponse")),
					}),
				),
			},
			"/wxapp/operateWxData": map[string]any{
				"post": openAPIOperation(
					[]string{"wxapp"},
					"小程序云函数",
					nil,
					jsonRequestBody(refSchema("OperateWXDataRequest")),
					defaulted(map[string]any{
						"200": jsonResponse("operateWxData 调用结果。", refSchema("WxappResponse")),
					}),
				),
			},
		},
		"components": map[string]any{
			"schemas": map[string]any{
				"APIResponse": objectSchema([]string{"code", "msg", "data"}, map[string]any{
					"code": map[string]any{"type": "integer", "example": 0, "description": "业务状态码，0 表示成功，非 0 表示业务错误。"},
					"msg":  map[string]any{"type": "string", "example": "success", "description": "提示信息，前端可直接用于 Toast 提示。"},
					"data": nullableObjectSchema("实际数据载荷，可以是对象、数组或 null。"),
				}),
				"APIErrorResponse": objectSchema([]string{"code", "msg", "data"}, map[string]any{
					"code": map[string]any{"type": "integer", "example": 400, "description": "非 0 业务错误码。"},
					"msg":  map[string]any{"type": "string", "example": "ref is required"},
					"data": nullableObjectSchema("错误响应当前固定返回 null。"),
				}),
				"HealthResponse": objectSchema([]string{"ok"}, map[string]any{
					"ok": map[string]any{"type": "boolean"},
				}),
				"QRCreateResponse": objectSchema([]string{"session_id", "status", "image_url"}, map[string]any{
					"session_id":   map[string]any{"type": "string"},
					"status":       map[string]any{"type": "string", "example": "pending"},
					"image_url":    map[string]any{"type": "string", "example": "/qr/{session_id}/image"},
					"image_base64": nullableStringSchema("当 as_base64=true 时返回二维码图片 data URI。"),
					"proxy":        map[string]any{"type": "string", "description": "本次授权会话使用的脱敏代理地址；直连时为空。"},
				}),
				"QRPollResponse": objectSchema([]string{"status"}, map[string]any{
					"status": map[string]any{
						"type": "string",
						"enum": []string{"pending", "scanned", "authorized", "confirmed", "expired", "cancelled", "unknown"},
					},
					"errcode": map[string]any{"type": "integer", "nullable": true},
				}),
				"QuickLoginCreateResponse": objectSchema([]string{"session_id", "appid", "scope", "redirect_uri", "state", "ports", "expires_in"}, map[string]any{
					"session_id":   map[string]any{"type": "string"},
					"appid":        map[string]any{"type": "string"},
					"scope":        map[string]any{"type": "string"},
					"redirect_uri": map[string]any{"type": "string", "format": "uri"},
					"state":        map[string]any{"type": "string"},
					"ports":        arraySchema(map[string]any{"type": "integer"}),
					"expires_in":   map[string]any{"type": "integer", "format": "int64"},
				}),
				"QuickLoginConfirmRequest": objectSchema([]string{"redirect_url"}, map[string]any{
					"redirect_url": map[string]any{"type": "string", "format": "uri"},
				}),
				"AccountPublic": objectSchema([]string{"id", "openid", "created_at", "updated_at"}, map[string]any{
					"id":                        int64Schema(),
					"openid":                    map[string]any{"type": "string"},
					"uin":                       nullableInt64Schema(),
					"alias":                     nullableStringSchema("账号别名。"),
					"nickname":                  nullableStringSchema("账号昵称。"),
					"remark":                    nullableStringSchema("用户设置的账号备注。"),
					"avatar":                    nullableStringSchema("本地头像路径或远程头像 URL。"),
					"status":                    nullableStringSchema("账号状态。"),
					"refresh_token_observed_at": nullableInt64Schema(),
					"rescan_recommended":        map[string]any{"type": "boolean", "description": "当前 refresh token 已连续使用约 25 天，建议重新扫码。"},
					"last_checked_at":           nullableInt64Schema(),
					"created_at":                int64Schema(),
					"updated_at":                int64Schema(),
				}),
				"RefreshResult": objectSchema([]string{"id", "openid", "status", "rescan_required"}, map[string]any{
					"id":              int64Schema(),
					"openid":          map[string]any{"type": "string"},
					"uin":             nullableInt64Schema(),
					"nickname":        nullableStringSchema("账号昵称。"),
					"status":          map[string]any{"type": "string", "example": "alive"},
					"rescan_required": map[string]any{"type": "boolean", "description": "仅在服务端明确判定登录凭证失效时为 true。"},
					"refresh_error":   nullableStringSchema("本次刷新失败原因；临时网络错误不代表账号失效。"),
				}),
				"DeleteAccountResponse": objectSchema([]string{"deleted", "openid", "qinglong_cleanup", "env_entries_removed", "tasks_deleted"}, map[string]any{
					"deleted":             int64Schema(),
					"openid":              map[string]any{"type": "string"},
					"qinglong_cleanup":    map[string]any{"type": "string", "enum": []string{"completed", "skipped"}},
					"env_entries_removed": map[string]any{"type": "integer"},
					"tasks_deleted":       map[string]any{"type": "integer"},
				}),
				"AccountRefRequest": objectSchema(nil, map[string]any{
					"ref": map[string]any{"type": "string", "description": "账号 ID、UIN 或 openid。支持批量操作的接口不传时表示全部账号。"},
				}),
				"AccountRemarkRequest": objectSchema([]string{"ref", "remark"}, map[string]any{
					"ref":    map[string]any{"type": "string"},
					"remark": map[string]any{"type": "string", "maxLength": 80},
				}),
				"ProxySpec": objectSchema(nil, map[string]any{
					"product":               map[string]any{"type": "string", "enum": []string{"appstore"}, "default": "appstore", "description": "登录产品。电脑管家等产品在凭据兑换链路验证前不会创建会话。"},
					"mode":                  map[string]any{"type": "string", "enum": []string{"direct", "static", "api"}, "default": "direct"},
					"proxy_type":            map[string]any{"type": "string", "enum": []string{"http", "socks5"}, "default": "http", "description": "http 使用 HTTP CONNECT。"},
					"static_proxy":          map[string]any{"type": "string", "example": "user:pass@127.0.0.1:8080"},
					"api_url":               map[string]any{"type": "string", "format": "uri", "description": "可携带省市参数；响应支持 txt、json、json2 及常见嵌套字段。"},
					"provider_profile_id":   nullableInt64Schema(),
					"region_code":           map[string]any{"type": "string", "description": "品赞 6 位城市编码，all 表示全国。"},
					"region_province":       map[string]any{"type": "string"},
					"region_city":           map[string]any{"type": "string"},
					"refresh_ahead_minutes": map[string]any{"type": "integer", "minimum": 5, "maximum": 90, "default": 5},
				}),
				"AccountProxyRequest": objectSchema([]string{"ref", "mode"}, map[string]any{
					"ref":                   map[string]any{"type": "string", "description": "账号 ID、UIN 或 openid。"},
					"mode":                  map[string]any{"type": "string", "enum": []string{"direct", "static", "api"}},
					"proxy_type":            map[string]any{"type": "string", "enum": []string{"http", "socks5"}},
					"static_proxy":          map[string]any{"type": "string"},
					"api_url":               map[string]any{"type": "string", "format": "uri"},
					"provider_profile_id":   nullableInt64Schema(),
					"region_code":           map[string]any{"type": "string"},
					"region_province":       map[string]any{"type": "string"},
					"region_city":           map[string]any{"type": "string"},
					"refresh_ahead_minutes": map[string]any{"type": "integer", "minimum": 5, "maximum": 90, "default": 5},
				}),
				"AccountProxyTestRequest": objectSchema(nil, map[string]any{
					"ref":                   map[string]any{"type": "string", "description": "仅传 ref 时测试已保存配置；也可直接传代理配置。"},
					"mode":                  map[string]any{"type": "string", "enum": []string{"direct", "static", "api"}},
					"proxy_type":            map[string]any{"type": "string", "enum": []string{"http", "socks5"}},
					"static_proxy":          map[string]any{"type": "string"},
					"api_url":               map[string]any{"type": "string", "format": "uri"},
					"provider_profile_id":   nullableInt64Schema(),
					"region_code":           map[string]any{"type": "string"},
					"region_province":       map[string]any{"type": "string"},
					"region_city":           map[string]any{"type": "string"},
					"refresh_ahead_minutes": map[string]any{"type": "integer", "minimum": 5, "maximum": 90, "default": 5},
				}),
				"AccountProxySetting": objectSchema([]string{"account_id", "mode", "proxy_type", "configured", "updated_at"}, map[string]any{
					"account_id":            int64Schema(),
					"mode":                  map[string]any{"type": "string", "enum": []string{"direct", "static", "api"}},
					"proxy_type":            map[string]any{"type": "string", "enum": []string{"http", "socks5"}},
					"static_proxy":          map[string]any{"type": "string"},
					"api_url":               map[string]any{"type": "string"},
					"provider_profile_id":   nullableInt64Schema(),
					"region_code":           map[string]any{"type": "string"},
					"region_province":       map[string]any{"type": "string"},
					"region_city":           map[string]any{"type": "string"},
					"refresh_ahead_minutes": map[string]any{"type": "integer", "minimum": 5, "maximum": 90},
					"token_ttl_minutes":     map[string]any{"type": "integer", "minimum": 1, "description": "该账号最近一次应用宝响应中的令牌有效期，用于估算实际刷新周期。"},
					"configured":            map[string]any{"type": "boolean"},
					"updated_at":            int64Schema(),
				}),
				"ProxyProviderProfileRequest": objectSchema([]string{"name", "provider"}, map[string]any{
					"name":               map[string]any{"type": "string", "maxLength": 50, "example": "巨量代理 1"},
					"provider":           map[string]any{"type": "string", "enum": []string{"ipzan", "juliang"}, "default": "ipzan"},
					"proxy_type":         map[string]any{"type": "string", "enum": []string{"http", "socks5"}, "default": "http"},
					"authorization_mode": map[string]any{"type": "string", "enum": []string{"auth", "whitelist"}, "default": "auth", "description": "auth 使用提取结果中的临时账号密码；whitelist 依赖服务器出口 IP 白名单。"},
					"api_url":            map[string]any{"type": "string", "format": "uri", "description": "品赞配置填写包含 no 和 secret 的 core-extract HTTPS 链接。"},
					"trade_no":           map[string]any{"type": "string", "description": "巨量企业动态代理业务编号。"},
					"api_key":            map[string]any{"type": "string", "minLength": 32, "maxLength": 32, "description": "巨量业务 API Key。"},
				}),
				"ProxyProviderProfile": objectSchema([]string{"id", "name", "provider", "proxy_type", "api_url"}, map[string]any{
					"id":         int64Schema(),
					"name":       map[string]any{"type": "string"},
					"provider":   map[string]any{"type": "string", "enum": []string{"ipzan", "juliang"}},
					"proxy_type": map[string]any{"type": "string", "enum": []string{"http", "socks5"}},
					"api_url":    map[string]any{"type": "string", "format": "uri"},
					"created_at": int64Schema(),
					"updated_at": int64Schema(),
				}),
				"ProxyArea": objectSchema([]string{"code", "name"}, map[string]any{
					"code": map[string]any{"type": "string", "example": "370100"},
					"name": map[string]any{"type": "string", "example": "济南市"},
				}),
				"ProxyLocationRequest": objectSchema(nil, map[string]any{
					"latitude":  map[string]any{"type": "number", "format": "double", "minimum": -90, "maximum": 90},
					"longitude": map[string]any{"type": "number", "format": "double", "minimum": -180, "maximum": 180},
				}),
				"ProxyLocationResponse": objectSchema([]string{"province", "source", "matched"}, map[string]any{
					"province":      map[string]any{"type": "string", "example": "山东省"},
					"city":          map[string]any{"type": "string", "example": "济南市"},
					"source":        map[string]any{"type": "string", "enum": []string{"browser_geolocation", "client_public_ip", "server_public_ip"}},
					"ip":            map[string]any{"type": "string"},
					"province_code": map[string]any{"type": "string", "example": "370000"},
					"city_code":     map[string]any{"type": "string", "example": "370100"},
					"matched":       map[string]any{"type": "boolean"},
				}),
				"AccountProxyTestResponse": objectSchema([]string{"resolved", "proxy"}, map[string]any{
					"resolved":    map[string]any{"type": "boolean"},
					"proxy":       map[string]any{"type": "string", "description": "不含账号密码的代理入口地址。"},
					"exit_ip":     map[string]any{"type": "string", "description": "通过代理探测到的实际出口 IP。"},
					"exit_region": map[string]any{"type": "string", "description": "实际出口省份或地区。"},
					"exit_city":   map[string]any{"type": "string", "description": "实际出口城市。"},
					"probe_error": map[string]any{"type": "string", "description": "代理已提取但出口探测失败时的提示。"},
				}),
				"RefreshResponse": oneOfSchema(
					refSchema("RefreshResult"),
					arraySchema(refSchema("RefreshResult")),
				),
				"ResyncResponse": oneOfSchema(
					refSchema("AccountPublic"),
					arraySchema(refSchema("AccountPublic")),
				),
				"WxappRequest": objectSchema([]string{"ref", "app_id"}, map[string]any{
					"ref":    map[string]any{"type": "string", "description": "账号 ID、UIN 或 openid。"},
					"app_id": map[string]any{"type": "string"},
				}),
				"OperateWXDataRequest": objectSchema([]string{"ref", "app_id", "payload"}, map[string]any{
					"ref":     map[string]any{"type": "string", "description": "账号 ID、UIN 或 openid。"},
					"app_id":  map[string]any{"type": "string"},
					"payload": freeFormObjectSchema("完整的 operateWxData 请求 JSON。"),
				}),
				"WxappAccountLabel": objectSchema([]string{"id"}, map[string]any{
					"id":       int64Schema(),
					"alias":    nullableStringSchema("账号别名。"),
					"nickname": nullableStringSchema("账号昵称。"),
					"remark":   nullableStringSchema("用户设置的账号备注。"),
				}),
				"WxappResponse": objectSchema([]string{"openid", "account", "result"}, map[string]any{
					"openid":  map[string]any{"type": "string"},
					"account": refSchema("WxappAccountLabel"),
					"result":  freeFormObjectSchema("wxapp 接口返回结果。"),
				}),
				"QingLongStatus": objectSchema([]string{"configured", "connected"}, map[string]any{
					"configured": map[string]any{"type": "boolean"},
					"connected":  map[string]any{"type": "boolean"},
					"error":      nullableStringSchema("连接失败时的错误摘要。"),
				}),
				"QingLongConfig": objectSchema([]string{"url", "client_id", "secret_configured", "configured"}, map[string]any{
					"type":              map[string]any{"type": "string", "enum": []string{"qinglong", "daidai", "arcadia"}},
					"active_type":       map[string]any{"type": "string", "enum": []string{"qinglong", "daidai", "arcadia"}},
					"url":               map[string]any{"type": "string"},
					"client_id":         map[string]any{"type": "string"},
					"secret_configured": map[string]any{"type": "boolean"},
					"configured":        map[string]any{"type": "boolean"},
					"connected":         map[string]any{"type": "boolean"},
					"profiles":          freeFormObjectSchema("按面板类型返回已保存的非敏感连接信息。"),
				}),
				"QingLongConfigRequest": objectSchema(nil, map[string]any{
					"type":          map[string]any{"type": "string", "enum": []string{"qinglong", "daidai", "arcadia"}, "default": "qinglong"},
					"url":           map[string]any{"type": "string", "example": "http://qinglong:5700"},
					"client_id":     map[string]any{"type": "string", "description": "青龙 Client ID 或呆呆 App Key；Arcadia 模式忽略此字段。"},
					"client_secret": map[string]any{"type": "string", "writeOnly": true, "description": "青龙 Client Secret、呆呆 App Secret 或 Arcadia OpenAPI Token；留空表示保留。"},
					"clear":         map[string]any{"type": "boolean", "description": "设为 true 时清除连接配置。"},
				}),
				"QingLongSyncResponse": objectSchema([]string{"account", "name", "value", "added"}, map[string]any{
					"account": refSchema("AccountPublic"),
					"name":    map[string]any{"type": "string", "example": "YYB_SERVER"},
					"value":   map[string]any{"type": "string"},
					"added":   map[string]any{"type": "boolean"},
				}),
				"AccountJob": objectSchema([]string{"script_key", "name", "schedule", "provisioned", "enabled", "running"}, map[string]any{
					"script_key":         map[string]any{"type": "string"},
					"name":               map[string]any{"type": "string"},
					"schedule":           map[string]any{"type": "string"},
					"provisioned":        map[string]any{"type": "boolean"},
					"enabled":            map[string]any{"type": "boolean"},
					"running":            map[string]any{"type": "boolean"},
					"ql_cron_id":         nullableInt64Schema(),
					"last_execution_at":  int64Schema(),
					"last_running_time":  int64Schema(),
					"global_task_active": map[string]any{"type": "boolean"},
				}),
				"AccountJobsResponse": objectSchema([]string{"account", "jobs", "count"}, map[string]any{
					"account": refSchema("AccountPublic"),
					"jobs":    arraySchema(refSchema("AccountJob")),
					"count":   map[string]any{"type": "integer"},
				}),
				"JobActionRequest": objectSchema([]string{"ref", "script_key", "enabled"}, map[string]any{
					"ref":        map[string]any{"type": "string"},
					"script_key": map[string]any{"type": "string"},
					"enabled":    map[string]any{"type": "boolean"},
				}),
				"JobRunRequest": objectSchema([]string{"ref", "script_key"}, map[string]any{
					"ref":        map[string]any{"type": "string"},
					"script_key": map[string]any{"type": "string"},
				}),
				"JobLogResponse": objectSchema([]string{"script_key", "ql_cron_id", "log"}, map[string]any{
					"script_key": map[string]any{"type": "string"},
					"ql_cron_id": int64Schema(),
					"log":        map[string]any{"type": "string"},
				}),
				"AccountRun": objectSchema([]string{"account_id", "script_key", "name", "ql_cron_id", "log_key", "started_at", "size", "running", "status"}, map[string]any{
					"account_id": int64Schema(),
					"script_key": map[string]any{"type": "string"},
					"name":       map[string]any{"type": "string"},
					"ql_cron_id": int64Schema(),
					"log_key":    map[string]any{"type": "string"},
					"started_at": int64Schema(),
					"size":       int64Schema(),
					"running":    map[string]any{"type": "boolean"},
					"status":     map[string]any{"type": "string", "enum": []string{"运行中", "已完成"}},
				}),
				"AccountRunsResponse": objectSchema([]string{"account", "runs", "count"}, map[string]any{
					"account": refSchema("AccountPublic"),
					"runs":    arraySchema(refSchema("AccountRun")),
					"count":   map[string]any{"type": "integer"},
				}),
				"AccountRunLogResponse": objectSchema([]string{"account_id", "script_key", "log_key", "log"}, map[string]any{
					"account_id": int64Schema(),
					"script_key": map[string]any{"type": "string"},
					"log_key":    map[string]any{"type": "string"},
					"log":        map[string]any{"type": "string"},
				}),
				"AccountRunLogRequest": objectSchema([]string{"ref", "log_key"}, map[string]any{
					"ref":     map[string]any{"type": "string"},
					"log_key": map[string]any{"type": "string"},
				}),
				"PushSetting": objectSchema([]string{"channel", "token_configured", "topic_configured"}, map[string]any{
					"channel":          map[string]any{"type": "string", "enum": []string{"none", "serverchan", "pushplus", "qywx"}},
					"token_configured": map[string]any{"type": "boolean"},
					"topic_configured": map[string]any{"type": "boolean"},
				}),
				"PushSettingRequest": objectSchema([]string{"ref", "channel"}, map[string]any{
					"ref":     map[string]any{"type": "string"},
					"channel": map[string]any{"type": "string", "enum": []string{"none", "serverchan", "pushplus", "qywx"}},
					"token":   map[string]any{"type": "string", "writeOnly": true, "description": "留空时保留已配置密钥。"},
					"topic":   nullableStringSchema("PushPlus 群组编码。"),
				}),
			},
		},
	}
}

func openAPIOperation(tags []string, summary string, parameters []map[string]any, requestBody map[string]any, responses map[string]any) map[string]any {
	out := map[string]any{
		"tags":      tags,
		"summary":   summary,
		"responses": responses,
	}
	if len(parameters) > 0 {
		out["parameters"] = parameters
	}
	if requestBody != nil {
		out["requestBody"] = requestBody
	}
	return out
}

func defaulted(responses map[string]any) map[string]any {
	responses["default"] = jsonErrorResponse("错误响应。")
	return responses
}

func jsonResponse(description string, schema map[string]any) map[string]any {
	return map[string]any{
		"description": description,
		"content": map[string]any{
			"application/json": map[string]any{
				"schema": apiResponseSchema(schema),
			},
		},
	}
}

func jsonErrorResponse(description string) map[string]any {
	return map[string]any{
		"description": description,
		"content": map[string]any{
			"application/json": map[string]any{
				"schema": refSchema("APIErrorResponse"),
			},
		},
	}
}

func imageResponse(description string) map[string]any {
	return map[string]any{
		"description": description,
		"content": map[string]any{
			"image/jpeg": map[string]any{
				"schema": map[string]any{"type": "string", "format": "binary"},
			},
		},
	}
}

func jsonRequestBody(schema map[string]any) map[string]any {
	return map[string]any{
		"required": true,
		"content": map[string]any{
			"application/json": map[string]any{
				"schema": schema,
			},
		},
	}
}

func jsonOptionalRequestBody(schema map[string]any) map[string]any {
	return map[string]any{
		"required": false,
		"content": map[string]any{
			"application/json": map[string]any{
				"schema": schema,
			},
		},
	}
}

func pathStringParam(name, description string) map[string]any {
	return map[string]any{
		"name":        name,
		"in":          "path",
		"description": description,
		"required":    true,
		"schema":      map[string]any{"type": "string"},
	}
}

func queryStringParam(name, description string, required bool) map[string]any {
	return map[string]any{
		"name":        name,
		"in":          "query",
		"description": description,
		"required":    required,
		"schema":      map[string]any{"type": "string"},
	}
}

func boolQueryParam(name, description string) map[string]any {
	return map[string]any{
		"name":        name,
		"in":          "query",
		"description": description,
		"required":    false,
		"schema":      map[string]any{"type": "boolean"},
	}
}

func oneOfSchema(schemas ...map[string]any) map[string]any {
	return map[string]any{"oneOf": schemas}
}

func refSchema(name string) map[string]any {
	return map[string]any{"$ref": "#/components/schemas/" + name}
}

func arraySchema(item map[string]any) map[string]any {
	return map[string]any{
		"type":  "array",
		"items": item,
	}
}

func objectSchema(required []string, properties map[string]any) map[string]any {
	schema := map[string]any{
		"type":       "object",
		"properties": properties,
	}
	if len(required) > 0 {
		schema["required"] = required
	}
	return schema
}

func apiResponseSchema(dataSchema map[string]any) map[string]any {
	if dataSchema == nil {
		dataSchema = nullableObjectSchema("实际数据载荷。")
	}
	return objectSchema([]string{"code", "msg", "data"}, map[string]any{
		"code": map[string]any{"type": "integer", "example": 0, "description": "业务状态码，0 表示成功，非 0 表示业务错误。"},
		"msg":  map[string]any{"type": "string", "example": "success", "description": "提示信息，前端可直接用于 Toast 提示。"},
		"data": dataSchema,
	})
}

func freeFormObjectSchema(description string) map[string]any {
	return map[string]any{
		"type":                 "object",
		"description":          description,
		"additionalProperties": true,
		"nullable":             true,
	}
}

func nullableObjectSchema(description string) map[string]any {
	return map[string]any{
		"type":                 "object",
		"description":          description,
		"additionalProperties": true,
		"nullable":             true,
	}
}

func nullableStringSchema(description string) map[string]any {
	return map[string]any{
		"type":        "string",
		"description": description,
		"nullable":    true,
	}
}

func int64Schema() map[string]any {
	return map[string]any{"type": "integer", "format": "int64"}
}

func nullableInt64Schema() map[string]any {
	return map[string]any{"type": "integer", "format": "int64", "nullable": true}
}
