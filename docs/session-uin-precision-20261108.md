# 会话缓存 UIN 精度修复（2026-11-08）

状态：已确定性复现并修复本地精度缺陷，离线回归通过，尚待真实业务验收。以 #147 的提示为线索排查 #145/#142；没有批量请求上游、重扫或修改正式账号。

## 已确认的原因

首次 ManualAuth 返回的 `AppSession.UIN` 为 `int64`。保存时，SQL `sessions.uin` 与 `session_blob.session.uin` 的 JSON 数字都能保持整数精度。但 `scanSession()` 使用 `json.Unmarshal` 解码到 `map[string]any`，数字默认成为 `float64`。超过 `2^53` 后不能保证整数精确，例如虚构值 `9007199254740993` 会读成 `9007199254740992`。

`sessionFromBlob()` 再把已舍入的浮点数转回 `int64`，无法恢复丢失的位。首次请求直接使用登录结果，后续请求读取数据库缓存，因此相同账号会发出不同的身份字段：

- WPKG 头部字段 2、22 中的完整 UIN；
- JSAPI 内外层 session info 中 UIN 的低 32 位；
- 共用上述封装的取码、手机号与 `operateWxData`/encryptkey 请求。

这是一处客户端请求构造错误。`shortlink_application -14` 仍是上游返回的拒绝，不能仅凭这个错误码判断 AppID 无效、票据已过期或账号被封。

## 修复内容

数据库会话读取改用 `json.Decoder.UseNumber()`，让 JSON 整数保持原始十进制文本，交给已有的 `int64FromAny(json.Number)` 精确转换。保留原始 JSON 作为原生会话 CAS 比较条件，继续拒绝尾随或损坏的 JSON。实验室保存会话与显式审计入口中的会话快照读取使用同样方式，避免恢复、取码与 H5 对照也损坏身份；没有因此重新运行真实会话实验。

没有修改数据库结构、OAuth 流程、保护窗口、登录预算、重试或刷新时机。

## 与 issue 现象的关系

| Issue | 本次证据与边界 |
| --- | --- |
| #147：UIN JSON 精度建议 | 已确认，整数经过真实临时数据库往返后丢位，修复前测试失败、修复后通过。 |
| #145：首次成功、后续 `-14` | 已复现缓存读回后请求身份变化。新登录可暂时掩盖该缺陷，保护窗口可能阻止这种重建；尚未用报告者账号验证全部现象。 |
| #142：Docker 手机号首次成功 | 同一缓存读取影响手机号请求，但现有测试没有证明 Docker 与 Windows 的差异都由此造成。 |
| #141：H5 `-14` | 共用会话可能受影响，缺少目标与真实验收，不能宣称已解决。 |
| #136：没有 code，诊断响应 `-12028` | 属于另一项待排查的业务响应，本次未验证解决。 |

`v0.2.35` 的数据库读取也使用普通 `json.Unmarshal`，所以不能把精度缺陷说成最近版本才引入，也不能用它单独解释所有“退回旧版就正常”的报告。需要区分首次登录、缓存复用和失败后的重新登录，比较相同输入下的实际请求身份；没有核对 UIN 的旧实验不能证明会话是一次性的。

## 验证

- `TestSessionRoundTripPreservesIntegerPrecision`：小整数、`2^53+1`、负数及 `int64` 两端边界；SQL/JSON 往返、恢复读取和 CAS 更新后再次读取。
- `TestCachedRequestsPreserveFreshLoginIdentity`：虚构 YYB/PCMgr 会话存入临时 SQLite，反复加载并关闭重开数据库；比较首次与缓存读回后的 WPKG、取码、手机号、encryptkey 请求明文，要求逐字节一致。
- `TestSessionDecodeRejectsTrailingJSON`、`TestLabSavedSessionPreservesUINPrecision`：会话格式拒绝行为与实验室保存/重载。
- `TestAuditSessionJSONPreservesUINPrecision`：直接及嵌套的审计会话快照保留 UIN，拒绝尾随数据；分别在 `protocolaudit`、`issue75probe` 构建标签下验证。
- `go test ./...`、`go vet ./...`，以及 `go test -tags protocolaudit ./internal/protocol ./cmd/protocol-lab -run 'TestLab|TestNativeLab' -count=1` 均通过。

测试均使用虚构数据，无上游网络请求。这证明本地整数与封包修复，不是长期保活或所有接口的端到端证明。

## 升级与验收

原始 `session_blob` 仍保存正确整数时，更新程序后读取即可恢复精度，不需要改库或为了此修复先重扫。若旧程序已经把舍入后的 UIN 再次写回，则本次读取修复不能还原丢失的数字；不能凭猜测改 UIN，也不应为了修正它反复登录或重放失败密钥。

用包含此修复的构建，在保护设置保持现状时，对同一账号、同一 AppID 按正常调用间隔少量复测首次及后续取码、手机号或 encryptkey。区分“接口不再报错”和业务实际返回成功；手机号等接口仍受目标应用授权前提约束。如果仍失败，保留版本、接口、阶段和上游错误码，继续定位，不上传数据库、凭据、票据、code、身份或原始私有日志。
