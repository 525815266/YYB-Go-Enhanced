# 协议接口说明

YYB Go Enhanced 提供 `/wxapp/*` 标准入口和 `/wx/*` 兼容入口。运行中的服务可通过 `/docs` 打开 OpenAPI 页面，通过 `/openapi.json` 获取机器可读定义。

## 请求约定

小程序接口通常需要：

```json
{
  "ref": "1",
  "app_id": "wx0000000000000000"
}
```

- `ref` 是 YYB 账号数字 ID 或 OpenID。
- `app_id` 是目标小程序 AppID。
- 请求使用 `POST` 和 `Content-Type: application/json`。
- `wx.login` code 短期且一次性，不能跨请求或跨脚本复用。

启用 `YYB_PROTOCOL_TOKEN` 后，所有 `/wx/*` 与 `/wxapp/*` 自动化请求必须增加：

```http
Authorization: Bearer <token>
```

## 接口清单

| 接口 | 用途 |
| --- | --- |
| `POST /wxapp/getCode` | 获取小程序 `wx.login` code |
| `POST /wx/code` | `getCode` 兼容入口 |
| `POST /wxapp/getPhoneNumber` | 获取手机号能力结果 |
| `POST /wx/getphonenumber` | 手机号兼容入口 |
| `POST /wxapp/operateWxData` | 转发完整 `operateWxData` payload |
| `GET /wx/getuserinfo` | 获取 YYB 账号用户信息 |
| `POST /wx/encryptkey` | 加密能力兼容转发，需要真实 payload |
| `POST /wx/getlatestuserkey` | `webapi_getuserencryptkey` 加密密钥转发，需要真实 payload |
| `POST /wx/cloud` | 云函数或通用 `operateWxData` 转发 |
| `POST /wx/mpgeta8key` | `mpGetA8Key` 的 `operateWxData` 兼容转发，未验证通用公众号 A8Key 流程 |
| `POST /wx/appmsgext` | 文章扩展数据兼容转发 |
| `POST /wx/appmsglike` | 文章点赞兼容转发 |
| `/wx/qrcodeauth/*` | 二维码授权会话相关入口 |

账号管理还提供 `/accounts/{ref}/getCode`、`/accounts/{ref}/getPhoneNumber` 和 `/accounts/{ref}/operateWxData` 等等价路由，完整参数以 OpenAPI 为准。

## 获取 wx.login code

```bash
curl -X POST http://yyb-go:8000/wxapp/getCode \
  -H 'Content-Type: application/json' \
  -d '{"ref":"1","app_id":"wx0000000000000000"}'
```

同一个账号的取码请求会串行执行，避免多个任务同时刷新或消费一次性 code；不同账号仍可并发。

## operateWxData

通用调用必须提供目标业务真实使用的 `payload`：

```json
{
  "ref": "1",
  "app_id": "wx0000000000000000",
  "payload": {
    "api_name": "callFunction",
    "data": {
      "name": "目标云函数",
      "data": {}
    },
    "env": 1
  }
}
```

`payload` 会交给微信协议层。路由名称不会自动补出目标业务的活动 ID、签名、会话、文章参数或加密字段。

微信客户端的 `wx.getUserCryptoManager().getLatestUserKey()` 在 `operateWxData` 协议层对应 `webapi_getuserencryptkey`。兼容入口会把 payload 中的 `api_name: "getLatestUserKey"` 或历史名称 `getUserEncryptKey` 规范为该协议名，其他字段保持不变。成功结果通常包含 `encrypt_key`、`iv`、`version` 和 `expire_in`，取决于账号、目标小程序和基础库；`expire_in` 是短时效，应该现取现用。

## 用户信息与手机号

`/wx/getuserinfo` 读取 YYB 已保存的微信账号资料，不会生成目标业务所需的 `encryptedData`、`iv` 或签名。

手机号能力需要目标小程序支持，并由对应账号获得真实的一次性 code。仅有 OpenID、普通用户资料或业务服务器最终 POST，无法推导手机号授权结果。

## 公众号 OAuth

当前 `/wx/oauth` 的实现只构造公众号授权 URL，返回 `authorization_required=true`、`code=null`，未实现协议侧发起授权、确认授权再取得网页回调 code 的完整流程。小程序 `wx.login` code 不能直接替代公众号 OAuth code。

`/wx/mpgeta8key` 目前将 `mpGetA8Key` 转交给 `operateWxData`；存在此路由不代表通用公众号 A8Key 能力已验证可用。本项目尚未实现 `/yyb/api/oauth-authorize`、`/yyb/api/oauth-authorize-confirm`、`/yyb/api/mp/oauth-authorize`、`/yyb/api/get-a8key` 这些其他项目的接口。

后续兼容需要对应项目的公开文档、脱敏请求/响应和完整授权链路，以确认其依赖的登录态及协议。当前实现的限制不代表其他协议实现一定无法完成。

## 能力边界

服务不会：

- 伪造微信返回值或业务成功结果；
- 从缺失字段中猜测签名、活动参数或加密数据；
- 根据文章 URL 自动推导文章会话和点赞 payload；
- 把业务服务器使用的自定义加密误当作通用微信 Key；
- 在结果未知的情况下自动重放可能产生副作用的业务请求。

分析新小程序时，应同时核对小程序代码、HAR 和真实 `operateWxData` 调用。只抓到最终业务 HTTP 请求时，通常只能复现业务接口，不能证明请求经过微信协议能力。

## 错误定位

| 状态或错误 | 含义 |
| --- | --- |
| `400 ref is required` | 路由可达，但请求缺少账号引用 |
| `404` | 路径或大小写不匹配 |
| `405` | HTTP 方法错误，应使用 `POST` |
| `401` | 协议令牌缺失、错误，或调用了需网页登录的管理接口 |
| `409` | 账号状态或同账号会话冲突，检查账号是否完成扫码和目标小程序授权 |
| `502` | 上游协议、网络、代理或微信服务调用失败 |
| `payload is required` | 兼容转发接口缺少真实 `operateWxData` 请求体 |

排错时先在调用脚本所在容器执行最小 `curl`，再检查反向代理和面板日志，避免把网页登录页误判为协议响应。
