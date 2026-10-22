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
- 小程序调用默认使用账号代理；v0.2.29 起可在「代理设置 → 小程序调用出口」单独选择直连，扫码及凭据刷新仍按账号代理执行，见[分流说明](proxy-keepalive.md#保活走代理小程序调用直连)。

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

当前 `/wxapp/getPhoneNumber`（含兼容入口）只调用现有手机号能力并返回微信结果，**未实现授权手机号列表查询或指定其中一个号码授权**。请求中的 `ref` 选择微信账号，不是该微信内的手机号；添加 `phone`、`phone_index` 等未定义字段不会让现有接口按号码选择。微信客户端可以维护多个号码，并不证明应用宝协议当前入口暴露了相同能力。

扩展号码选择需要确认“查询可授权号码 → 选定号码 → 返回授权结果”的协议、选择字段与账号/小程序会话绑定关系。可提供公开源码或去除真实手机号、票据与密钥的字段结构供评估；现阶段不能只新增一个选择参数就宣称支持。

## 公众号 OAuth

当前 `/wx/oauth` 的实现只构造公众号授权 URL，返回 `authorization_required=true`、`code=null`，未实现协议侧发起授权、确认授权再取得网页回调 code 的完整流程。小程序 `wx.login` code 不能直接替代公众号 OAuth code。

`/wx/mpgeta8key` 目前将 `mpGetA8Key` 转交给 `operateWxData`；存在此路由不代表通用公众号 A8Key 能力已验证可用。本项目尚未实现 `/yyb/api/oauth-authorize`、`/yyb/api/oauth-authorize-confirm`、`/yyb/api/mp/oauth-authorize`、`/yyb/api/get-a8key` 这些其他项目的接口。

后续兼容需要对应项目的公开文档、脱敏请求/响应和完整授权链路，以确认其依赖的登录态及协议。当前实现的限制不代表其他协议实现一定无法完成。

### 公众号授权对照（#75）

2026-10-16 补充的其他平台响应包含真实回调 URL 和 `code`，以及带 `uin`、`key`、`exportkey`、`pass_ticket` 等会话参数的 `full_url`。这证明对方返回的结果与本项目的“仅生成授权链接”不同，但这些字段是授权结果，不足以还原其取得会话票据的底层请求；不能从 OpenID 或小程序 code 计算出来。

当前转发 `mpGetA8Key` 的实测结果为内层 `-12003 / invalid api_name`，外层 `code=0` 只说明兼容转发已完成，不能当作公众号授权成功。现有 HAR 可以导入业务网站已下发的登录态（例如寻星网页积分），未实现无感续期公众号 OAuth。

做对照时需使用相同公众号 AppID、相同回调地址及 scope；#75 的两组示例 AppID 不同，不能直接把授权结果互换。不应公开真实 `full_url` 会话票据、Cookie 或一次性 code。2026-10-22 收到镜像后的独立 A8Key 验证已取得进一步结果，见下方补充；这与现有 `operateWxData` 转发入口是不同链路。

### 自动授权补丁说明的评估（#75，2026-10-22）

反馈者先补充了[变更说明](https://github.com/525815266/YYB-Go-Enhanced/issues/75#issuecomment-6332952610)，当时 diff 附件显示上传失败；同日稍后已通过维护者收到完整 diff 和镜像。说明提出在现有 `js-login` 请求的字段 3 写入公众号授权 URL，并将 URL、query 或裸字符串都作为 OAuth code 接受；文档本身注明尚待真机验证，因此编译和模拟测试通过不等于网页授权已经跑通。

字段有一处可核对的偏差：[Rprop/ipad 的协议定义](https://github.com/Rprop/ipad/blob/0278be2947eef98cb73f46579bce4428f7e93b96/protobuf/wechat_proto/applet.proto)和[nsky99/wechatclient 的协议定义](https://github.com/nsky99/wechatclient/blob/e1344339139a8a8a0b961ad3ee04c156ec23d8fb/pkg/wechat_sdk/mmpb/pb/JsLogin.proto)都将字段 3 定义为 `scope`、字段 5 为 `url`、字段 6 为 `state`。这些公开定义可用于检查字段，不能单凭它们证明当前 YYB 会话支持公众号自动授权。

维护者在飞牛使用一个自有账号做了隔离对照，复用同一有效会话与出口；公众号参数取自此前本人授权的 HAR，未访问业务回调：

| 请求 | 实测结果 | 能确认的范围 |
| --- | --- | --- |
| 原小程序 `js-login` | 业务状态 0，返回 32 字节值 | 基础协议通道可用 |
| 公众号 AppID，不加 URL | 业务状态 0，返回 32 字节裸值 | 无回调 URL，也未验证业务交换，不能将裸值认定为网页 OAuth code |
| 按附件将授权 URL 写入字段 3 | 内层状态 `-12000`，无 code | 附件所述关键请求在该环境未跑通 |
| 字段 3 改为 `snsapi_userinfo`，字段 5 写 URL，字段 6 写 state，保留原 loginType=1 | 内层状态 `-12000`，无 code | 仅调整这几个字段仍不足以完成授权 |

上述失败响应的外层状态仍为 0，传输成功不能覆盖内层错误。探测只读生产数据库，不修改账号、代理或会话缓存，不自动重试，不输出真实 code、OpenID、密钥或 login_buffer；测试进程正常完成也不代表授权成功。

当前不原样合入这份 js-login 补丁，`/wx/oauth` 保持生成授权链接的行为。拿到明确业务拒绝时，不应一律清除会话重试或擅自更换代理出口。

**收到镜像后的验证补充（同日）**：已确认其 OAuth 分支实际使用 `GetA8Key`（cmd `5112`、`/ilink/ilinkapp/mm/ilinkgeta8key`），说明中被认作 js-login 的函数实际是 `extractA8KeyFullURL`。维护者独立编写诊断程序，复用现有 YYB 会话，实测拿到带票据的微信授权 URL；请求授权页并按其表单确认后，微信返回 HTTP 301，Location 带有非空 code，回调域名、路径和 state 匹配本次请求。未访问业务回调或兑换业务 token，未将第三方程序接入生产。

这证明独立 A8Key 路线有实际可行性，前面 js-login 写法的失败不代表 YYB 无法完成公众号授权。详细证据、附件偏差和正式接入所需工作见[公众号自动授权附件评估](oauth-a8key-review.md)。当前结果属于隔离验证，尚未接入正式 API，不能据此认为 v0.2.33 已支持自动返回 code。

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
