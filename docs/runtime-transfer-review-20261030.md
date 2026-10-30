# #116 runtime 下载包：扫码与取码实测

2026-10-30，基于 v0.2.53。已检查反馈者另行提供的 Python 下载包，并在用户扫码确认后完成独立实验：**宿主登录成功，目标小程序取码被 `-12 / no permission` 拒绝**。这是一条区别于当前 ILink 传输的候选路径，尚不能作为可用的新渠道或持续授权修复上线。

反馈者在 [#116 的补充评论](https://github.com/525815266/YYB-Go-Enhanced/issues/116#issuecomment-6515568181) 中也说明“自己试了目前这个项目好像也不行了”。该包与此前的官方 Android demo 是两份材料；不能用官方 demo 的 MIT 许可推断此下载包的许可，也不能用下载包的 HTTP 入口说明官方 Android demo 无需 Service。

## 材料与实验隔离

下载包共 22 个文件，约 152 KB，包含 Python 服务与网页；未附 README、依赖清单或 LICENSE，未发现预置账号文件。本轮未启动其 Flask 服务、每日健康检查或自动删号逻辑，未导入其配置和存储模块到生产服务。

用独立本地工具完成二维码、登录交换和少量 `js-login` 探测。实验会话只保存在本地私有目录，没有写入飞牛账号、覆盖原授权或修改生产握手。确认扫码的单次 code 在交换前标记已尝试，避免重复消费；签名、登录与转发均使用 HTTPS，保持 TLS 校验，不输出会话或真实 code。

用于复核文件版本的 SHA256：

| 下载包文件 | SHA256 |
| --- | --- |
| `server/protocol.py` | `6685cfa417a28f38c71e32b0306b397678ec9e4510d60006aa60ce2386a75f01` |
| `server/routes/login.py` | `d4c016d11bfe3c940949cee988d787a300385aeb2584dd466f24b7e5e0531d4a` |
| `server/config.py` | `5e9c6808baa07e2aed99fe2b0819f4b4b47c929578a3860533ac9ae547cb2be8` |

未提交下载包源码、账号、二维码或私有状态。实验工具为调查用途；尚未取得成功业务响应，响应解析也不能视为已完成正向验证。

## 实际通路及与现有协议的差别

| 阶段 | 下载包的实现 | 与现有服务的关系 |
| --- | --- | --- |
| 获取扫码签名 | `open.weixin.qq.com/wxaruntime/getqrcodesignature`，指定宿主 AppID | 独立 runtime 宿主入口，不能任意替换为 YYB / PCMgr 的身份 |
| 微信扫码 | `/connect/sdk/qrconnect`，scope 为 `snsapi_userinfo,snsapi_runtime_apk`，长轮询确认 | 有效二维码和用户同意只证明扫码流程通过 |
| 交换登录 | `/wxaruntime/login`，取得 `uin` 与 `session_id` | runtime 会话，不是现有 OAuth refresh token、ILink PSK 或 AutoKey |
| 调用目标 | HTTPS `/wxaruntime/transfer`，JSON 包装 protobuf `ReqData` 和 `Ctx` | 服务端转发入口，区别于本项目直接使用 ILink / MMTLS 的传输 |
| 后续维持 | `main.py` 首次等待约一分钟，其后每日取码检查，匹配 `-5` 则删账号 | 健康检查，没有实现凭据刷新、AutoAuth 或已证实的续期心跳 |

下载包的宿主 AppID 为 `wx6b60fbda5d87d519`；转发上下文固定 `DeviceTypeId=0`、`ClientVersion=1064961`、`SessionType=1`、`ClientIP=0`、`ClientPort=80`、`CmdID=4963`。内层 BaseRequest 使用 `8421505` 和 `Windows-Ocean`，包含账号 UIN、持久化的本轮设备 UUID 及每次生成的 UUID。两层版本值不同是原包行为，不能仅凭数值不同认定配置错误。

没有把这些宿主字段、设备或 session 混入 YYB / PCMgr；即使未来打通，也应隔离保存渠道会话，并单独验证账号身份、权限及失效行为。

## 真实请求结果

北京时间 2026-10-30：

| 检查 | 结果 | 可以证明什么 |
| --- | --- | --- |
| 约 13:21 获取扫码签名、二维码 | HTTP 200，业务码 0，有效 JPEG | 入口可用，能够发起扫码 |
| 13:23:03 用户扫码后的登录交换 | HTTP 200，`base_resp.ErrCode=0`，取得长度 32 的 session | 本次宿主登录成功 |
| 13:23:12 起，控制目标 `wx3bb18aff4f2fe45f` 及包内默认目标 `wx832f9c3d7b1250ad` | HTTP 200，转发 `ErrCode=-12`，无 code | 登录后的目标取码仍被拒绝 |
| 原函数对照 | 受控执行原包 `do_transfer`，仍为 HTTP 200 / `-12`；嵌套错误为 `no permission` | 仅重写转发函数并未消除拒绝；原包漏读了详细错误 |

响应的有效诊断结构如下，已省略每次变化的 hint：

```json
{
  "ErrCode": -12,
  "base_resp": {
    "ErrCode": -12,
    "ErrMsg": ", no permission hint: [omitted]"
  }
}
```

不能把 HTTP 200、登录成功或 session 长度当作小程序 `wx.login` 成功。已观测的失败点是 runtime 转发权限；具体涉及宿主权限、配置前提或服务端策略，当前响应无法进一步区分。没有证据把它归因于飞牛、青龙脚本、代理不稳定或本项目被特别处理，也不能断言该入口在所有配置下永久不可用。

原函数对照采用 AST 只加载所需函数，在受控依赖中执行，没有运行原应用。离线固定虚构 UUID 后，独立工具的请求 protobuf 和 Ctx 与下载包一致；真实对照沿用本次实验 session、设备与目标。这里确认的是请求构造及所见拒绝，没有证明全部运行环境相同：独立工具未保存从签名、扫码到登录的连续 Cookie，原包使用进程内全局 Session，但其账号文件同样不持久保存 Cookie。尚无证据证明 Cookie 是本次拒绝根因，不重放已消费的扫码 code 来补造对照。

## 原包中已复现和需隔离的问题

| 位置 | 问题及复核结果 | 处理原则 |
| --- | --- | --- |
| `protocol.py:48` `do_transfer` | 只读顶层 `ErrMsg`，本次漏掉 `base_resp.ErrMsg` 的 `no permission` | 保留 HTTP、转发、业务三层结果，精确读取嵌套错误 |
| `protocol.py:75` `extract_code` | 将可打印字符拼接后扫描 code。用无关 protobuf 字段 9 放入虚构 32 字符串，已复现被当作 code | 必须按响应字段及业务状态解析，不能凭外形判定取码成功 |
| `routes/wxapp.py:19`，以及 `main.py` 健康检查 | 子串匹配 `ErrCode=-5`。已离线复现 `ErrCode=-50` 也被判断为过期，原调用者可进一步删账号 | 解析数值错误码，不凭文本子串自动删除授权 |
| `protocol.py:507` 之后的扫码流程 | 所有二维码共用全局 `qr_state`；确认后没有单次登录交换标记 | 按链接 / 会话隔离扫码状态，防止并发串号和重复消费 |
| `routes/login.py:65` 及服务入口 | 无鉴权导出完整 session，默认监听 `0.0.0.0` | 不能把原服务原样接入现有账号管理或公开部署 |
| `cloud.py:21`、扫码交换及存储 | 云请求关闭 TLS 校验；原签名 / 登录使用 HTTP；账号为明文 JSON 且非原子保存 | 正式接入仍需校验 TLS、安全持久化和既有权限边界 |

本轮只在虚构数据上复现解析 / 判断问题，没有触发原包真实删号、调用其云业务或修改下载目录。它们是这个候选包的缺陷，不是现有 Go 服务已确认存在相同 bug 的证据。

## 公开同源对照与下一步

GitHub 中找到 [ZHwin/yyb 的同源实现](https://github.com/ZHwin/yyb/blob/e1e9c3608d4008d24e0c23301ef4258cf2e11391/yyb1/app/pure_wxcode/wmpf.py)，固定到 `e1e9c3608d4008d24e0c23301ef4258cf2e11391`。其文件说明移植自同名目录，使用相同宿主和 transfer 参数；改为外部持有扫码状态、数值判断 `-5`，但仍有 code 字符扫描，未看到新的权限解决方案或 session 刷新实现。本次查看仓库 API 的许可字段为 null，不直接复制其代码。

可继续借鉴的是独立 runtime 通路与分层诊断。要进一步推进，需要可复核的当前成功样本及宿主权限前提，例如原运行环境 / 版本、宿主配置、目标 AppID，以及脱敏的登录和转发结果；不需要公开 session、单次 code、账号或密钥。当前没有证据支持靠增加健康检查频率、盲换宿主或设备字段解决 `no permission`。

本次仅补充调查记录与反馈，程序仍为 v0.2.53，未增加正式扫码选项、未切换生产渠道、未重启飞牛；没有把二维码可扫写成业务可用，也没有把未实现的续期描述为已修复。
