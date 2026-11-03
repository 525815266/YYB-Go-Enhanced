# 公众号网页授权 code（H5 OAuth）与动态 AppID 防护（v0.2.66）

日期：2026-11-04。关联 issue [#75](https://github.com/525815266/YYB-Go-Enhanced/issues/75)、[#85](https://github.com/525815266/YYB-Go-Enhanced/issues/85)、[#87](https://github.com/525815266/YYB-Go-Enhanced/issues/87)、[#133](https://github.com/525815266/YYB-Go-Enhanced/issues/133)，以及[网站应用 AppID 取码与授权失效](website-appid-getcode-20261103.md)、[早期会话与近期 H5 实现的真实对照](h5-legacy-session-compare-20261031.md)。

## 结论

- **H5/公众号脚本取 code 有了正式通道：** `POST /wx/h5code`（兼容入口 `/wxapp/getH5Code`）用账号自己的 YYB 会话走命令 1254（`/cgi-bin/mmbiz-bin/oauth_authorize`）自动完成网页授权，返回回调里带出的 code。这条链路在 10-31 的隔离对照里真机验证过（业务 `ret=0`、32 位 code、回调与 state 严格匹配，见[h5 对照文档](h5-legacy-session-compare-20261031.md)）；A8Key 5112 路径保留为审计兼容路径，未接入生产。
- **动态自学习黑名单：** 取码请求（`/wxapp/getCode` 等）到达上游后拿不到 code（重试后仍无，即"网站应用/公众号"特征）时，该 AppID 被自动禁用并持久化，之后所有账号的这类请求在本地返回 `403`，不再触达上游。未知 H5 AppID 至多损失一次请求，不再像 11-03 夜里那样接连打死账号。
- 两项保护的取舍、依据和限制都写在下面，没有宣布"根治风控"。

## 为什么 H5 脚本会打死账号

公众号/网站应用的 code 来自**网页授权**（`connect/oauth2/authorize` 回跳 `?code=`），不是小程序的 `wx.login`。此前脚本只能把公众号 AppID 塞进 `/wxapp/getCode`：上游按小程序流程处理，返回"没有 code"，随后该账号的 OAuth 层被风控拒绝（对照实验：一次请求即复现；四个被本地熔断挡住的账号全部存活）。v0.2.63 只硬编码拦了两个已知网站应用 AppID，名单外的公众号 AppID（如拼多多果园公众号版）仍会触发。

## `/wx/h5code` 用法

```bash
curl -X POST http://<server>:8000/wx/h5code \
  -H "Authorization: Bearer $YYB_PROTOCOL_TOKEN" \
  -d '{"ref":"1 或 @OpenID","app_id":"wx公众号AppID",
       "payload":{"redirect_uri":"https://业务回调/login","scope":"snsapi_userinfo"}}'
```

- `redirect_uri` 必填，`scope` 缺省 `snsapi_base`，`state` 缺省自动生成；响应 `result` 携带 `code`、`state`、`scope`、`redirect_uri`、`mode:"h5_oauth_auto"`。
- 成功标准收紧到验证过的口径：回复里必须带**显式业务状态 0**，Location 的 scheme/host/path 必须与 `redirect_uri` 完全一致、`code` 与 `state` 各出现一次且 `state` 匹配，回调原有 query 不允许被改动；服务端**不访问业务回调**，code 交给脚本自己换 token。
- 失败语义：业务拒绝（`ErrH5OAuthBusinessRejected`）与传输失败都原样返回，**不**触发强制刷新、不作废会话、不重放授权请求（确认会签发一次性 code，重放有副作用）；账号状态完全由后台保活负责。
- 并发与限速与其他协议接口一致（账号级 code 锁、登录预算、限速、熔断都生效）。

## 动态黑名单的行为

- 触发：`get_code` 请求真正到达上游、且按现有会话重建重试后仍然没有 code（`ErrLoginCodeUnavailable`）。这个签名来自对照实验：真正的网站应用 AppID 永远拿不到 code。
- 动作（v0.2.67 起跨账号确认）：同一 AppID 在 **2 个不同账号**上都拿不到 code（或累计 **4** 次，覆盖单账号部署）才禁用，持久化到 SQLite settings，重启不丢；被禁 AppID 在所有取码入口（含 `/wx/h5code`）本地 `403`，不发上游、不占限速槽位、不进熔断。任一账号成功取到 code 即清零该 AppID 的全部计数。
- **为什么需要跨账号确认（2026-11-04 夜的教训）：** 上游会把"长时间连续成功取码"的账号整体置入拒绝模式——v0.2.66 的阈值 1 在那一夜把 62 个 AppID 全部禁掉，其中 11 个是几分钟前还在别的账号上成功取码的健康小程序（证据在事件日志里）。账号被打进突发拒绝时，它身上每个 AppID 都会"无 code"，单账号的失败不能作为 AppID 危险的证据；其他账号在同一 AppID 上的成功才是反证。真死的 AppID（网站应用/下线小程序）在所有账号上都拿不到 code，跨账号规则照样禁掉它们。
- 查看与清除：`GET /api/appid-deny`（面板登录态）列出；`POST /api/appid-deny?app_id=wx…` 清除单个，不带 `app_id` 清除全部。`YYB_CODE_ALLOW_APPIDS` 优先级最高——v0.2.67 修复了它此前未覆盖动态名单的缺陷，启动时会直接从持久化名单里移除被放行的 AppID。`YYB_CODE_DYNAMIC_DENY=0` 关闭学习。
- 代价与风险：单个账号被突发拒绝的窗口里，真死的 AppID 要等第二个账号也失败才会被禁（最多多打几次上游）；单账号部署依赖累计 4 次的上限。两个方向都不完美，默认值偏保守。

## 对脚本的影响

- 使用公众号 AppID 的脚本（拼多多果园公众号版、腾讯电脑管家 _code 版等）应改调 `/wx/h5code`；继续用 `getCode` 传这些 AppID 会被内置名单或动态名单拦成 `403`，账号不再陪葬。
- 小程序脚本不受影响：正常取码不会触发学习；只有"到上游拿不到 code"才会计数。

## 没有解决的问题

- **账号被突发拒绝本身仍未解决：** 11-04 夜里账号 1 在约 80 秒内连续成功 17 个 code 后，上游开始对该账号整体拒绝，随后 OAuth 层死亡（与 11-02/11-03 的整点批量死亡同型）。3 秒限速挡不住"长时间持续取码"的模式，触发阈值在上游。脚本侧应把同一账号的取码量在时间上摊开、减少每账号每小时触达的 AppID 数量。
- 1254 路径未覆盖全部公众号/全部部署环境：10-31 的对照是单个公众号场景、直连会话；代理出口、组件授权（`component_appid`）等组合未验证。
- 风控机制未知：为什么一次无 code 的 wx.login 会连坐 OAuth 层，仍然只有相关性。动态黑名单是止损，不是对机制的修复。
- 误禁的恢复是手动的（放行/清除接口）；没有自动白名单。

## 协议层实现位置

- 生产：`internal/protocol/h5code.go`（`GetH5Code`、请求构造、严格解析）、`internal/httpapi/app.go`（`invokeH5Code`、H5 失败不恢复语义、学习逻辑）、`internal/httpapi/appid_policy.go`（`dynamicAppIDDeny`）。
- 审计对照（构建标签 `issue75probe`，默认不编译）：`internal/protocol/h5_compare_audit_test.go`、`h5_a8key_compare_helpers_test.go`。
