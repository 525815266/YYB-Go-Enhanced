# 雷电微信小游戏渠道与会话恢复对照（2026-11-05）

当前已验证雷电独立扫码换票据、Go 首次 `wx.login`，以及官方 SDK 在独立目录中的首次登录。Go 的后续 AutoAuth 已出现拒绝，随后缓存业务也失败；另一组官方 SDK 样本在约 10 分钟后的新进程 AutoLogin 返回 `1 / -13`。不能据首次成功宣称雷电保活完成。该渠道仍是实验能力，没有接入正式扫码页面或默认多渠道列表。

## 渠道与换票据

- 安装入口：雷电 14 → 雷电游戏中心 → 微信小游戏。
- Host AppID：`wx56135c126042c9e7`。
- OAuth scope：`snsapi_login,snsapi_runtime_pcsdk`；回调为 `https://www.ldmnq.com/ldy/pcbox/tencent-login.html`。
- 官方换票据接口：空 body 的 `POST https://game-service.ldmnq.com/mini-game/wechat/login?code=...`。GET 返回 405，不能拿 GET 结果判定渠道不支持。
- 成功条件为整数业务码 200 和字符串 `data`；后者是 base64 编码的 `login_buffer`，不是 YYB access / refresh token。
- `/mini-game/wechat/encode` 生成启动参数，尚无证据说明它能续期。没有找到可供本项目使用的雷电 OAuth refresh 接口，不能套用 YYB 刷新端点。

## 真实验证（北京时间）

全部来自用户扫码确认的本项目独立授权，不读取模拟器原有账号配置，不挂载生产数据库。两组授权属于额外实验干预，不是无干预自然寿命样本。

| 时间 | 请求 | 结果与限制 |
| --- | --- | --- |
| 21:34:17 | 第一组新扫码换票据 | 业务 200；解码后 214 字节，外层包含票据、设备、雷电 Host AppID。私有内容不公开 |
| 21:34:41 | Go ManualAuth → 控制 AppID 的 `wx.login` | code 长度 32；保存独立协议会话。只证明当次业务成功 |
| 21:39:06 | Go 原生 AutoAuth，一次 | `AutoAuthRejectedError`；旧实验入口未保留阶段和数值码，无法补判为 `-13` 或其他值。保留意图标记，不重放旧 key |
| 21:57–22:00 | 仅复用第一组缓存的业务检查，一次 | 应用 `-14`，没有 ManualAuth / OAuth / AutoAuth 回退。此前 AutoAuth 已干预，不能据此推算固定自然寿命 |
| 22:13:32 | 第二组新扫码，官方 SDK ThirdAppLogin | 回调 `error_type=5 / error_code=0`，返回数据长度 4；独立 SDK 目录。尚未验证新的 `wx.login` 或长期续期 |
| 22:24:09 | 新进程，官方 SDK AutoLogin，仅复用第二组保存目录，一次 | 回调 `error_type=1 / error_code=-13`，数据长度 2；不带票据，没有 OAuth / ThirdAppLogin 回退。首次错误已独占保存，停止该组，不重试 |

两组使用相同上游身份可能影响彼此，所以不把差异解释成唯一因果，也不继续消费第一组失败的恢复密钥。

官方 SDK 首次成功后，实验进程保留 5 秒再退出；约 10 分钟后才在新进程中自动登录。退出使用 `os._exit` 以避免 ctypes / 原生析构竞争，尚未完成官方正常关闭的对照；隔离初始化参数、持久化完整性、其他官方客户端及同身份登录也都是待排除因素。因此这次失败只能说明该隔离条件下官方 API 也没有恢复成功，不能外推为官方客户端一律无法保活，也不能把它直接归因于 Go 实现。回调 `-13` 已确认，具体服务端 / SDK 内部错误阶段尚未由本次回包独立确认。

离线核对 `ilink2.dll` 的 `TdiCheckCgiResponse<AutoAuthResponse>`：`0xa8661` 起的指令优先返回传入错误，否则返回响应头的业务 ret。虚构输入执行已证明这两条路径都可能选出 `-13`；这不是本次真实错误来源的证明。另一个已确认的本地分支在 AutoKey 为空时返回 `-14`（`0xad311`），也不能据此排除所有其他本地错误路径。

## 已核对的原生实现差异

SDK 样本为 `RadiumWMPF_2.2.3.520`。工具校验公开程序文件 SHA256 后才使用固定位置；校验值对应程序文件，不包含用户身份或凭据。

| 层次 | 官方 SDK 证据 | Go 当前行为 / 结论 |
| --- | --- | --- |
| AppBase 与 AutoAuth 请求体 | Unicorn 执行 `ilink2.dll` 的真实 serializer；无附加 context 时为设备 / 1901 / AutoKey 三个字段 | 与 Go 当前布局一致。可选字段 4 是 context，不是 EncKey；完整包头、加密与服务端接受仍须分别核对 |
| ThirdApp ManualAuth | builder `0xcff20`、serializer `0x106fc0` 显式写入空 bytes 字段 6 | Go 省略字段 6。这是可复现差异，尚无对照证据证明它导致后续失效，不修改正式协议来碰运气 |
| session module 配置 | `flue.dll` 的 app-context 初始化在 `0x12f9539` 设置 module type 6 | 与 Go 的 6 相符。serializer 测试中的 session type 0 是虚构输入，未把它冒充真实初始化值 |
| MMTLS 凭据 | 原生 `0x4abc7d`、`0x4ac69c` 分别通过存储接口读取类型 1 / 2，离线运行两段原生指令已验证；程序另有 Refresh PSK 持久化及恢复握手分支 | Go 能从握手导出多组 PSK，但只选择并持久化 Access PSK。Refresh PSK 是明确的传输恢复研究线索，不能单独更新已经被应用层拒绝的授权 |
| 连接生命周期 | SDK 由独立 runtime 管理连接；原生 Mars 有长连接 / noop 与重连路径 | Go 登录完成后关闭长连接，业务走短连接。尚未证明保持连接或 noop 能延长 ILink 业务授权 |
| 官方启动恢复 | `dnplayer.exe` 保存已登录标记，启动后查询状态并调用 SDK AutoLogin | 标记本身不是凭据；要验证原生自动恢复和新的真实业务，不能只看 UI 已登录 |

本轮没有实现 Refresh PSK 恢复、修改保活间隔、伪造到期时间或启用未验证的生产开关。

下一步优先验证官方正常保存 / 关闭与持续 runtime 两种生命周期，并核对 SDK 所用完整上下文及拒绝阶段；Refresh PSK 的握手恢复与应用 AutoAuth 分别验证。当前失败材料不会被反复重放来尝试猜测字段。

## 可复核工具

- `scripts/ldplayer_channel_probe.py`：新目录、新二维码；确认后只交换一次 OAuth code，票据与图片留在私有目录，不输出 code / 身份 / 原始错误。需要用户实际扫码，不自动操作官方账号。
- `cmd/protocol-lab`（`protocolaudit` 构建）：仅接受本地隔离票据或该 provider 的保存会话；雷电必须匹配自己的 Host AppID。缓存失败没有隐藏重登录。
- `--native-autoauth`：发送前以独占创建并 fsync 的意图文件限制一次；业务验证前保存轮换状态，包括密钥已轮换但传输不完整的情况。首次结果独占保存；后续错误包含安全阶段和数值码，不输出原始错误正文。若交换与保存均失败，保留两者而不覆盖首次交换错误。
- `tools/inspect_ldplayer_auth.py`：使用虚构输入执行真实原生序列化、PSK 存储读取和 AutoAuth 错误选择片段；不读取账号、不加载真实进程、不联网。可重复离线运行。
- `scripts/ldplayer_sdk_control.py`：Windows 64 位官方 SDK 对照。默认仅新目录初始化；`--ticket-file` 新建隔离 SDK profile，`--auto-login` 只能复用该工具成功登录过的目录，不带票据、不回退到 ThirdAppLogin。每种登录在该目录最多一次，超时不自动重试。

SDK 初始化参数为 product-id 3000、enable-applet-v3 true。Init 的 runtime_path 必须是包含 `host/` 和 `runtime/` 的 SDK 根目录；仅传 `runtime/` 时虽同步返回 0，却没有收到就绪回调。修正根路径后已真实收到 `OnContextInitialized`。同步返回 0 本身不是就绪证据。

原始票据、SDK profile、会话密钥、扫码图片、日志和首次失败材料均保留在忽略的私有目录，不提交仓库。

## 持续保留的验证标准

官方自动登录提供了值得继续复现的恢复入口，但永久授权不是客户端能够保证的。本项目应分别记录：不重新扫码时自动恢复是否获得新认证、密钥是否按服务端响应轮换、业务 `wx.login` 是否继续成功、进程重启后是否仍可恢复，以及期间是否发生手刷 / 新票据 / 其他会话干预。

正式接入还缺可验证的渠道身份与同用户绑定。雷电接口只返回 buffer，不能伪造 unionid，也不能凭昵称将渠道合并为同一账号。官方 SDK 的 Windows 对照也不等于飞牛 Linux Docker 已支持该恢复路径。

已有 `/accounts` 每账号组返回一个可用成员的改动已随 PR #143 合并；它调整账号枚举，不解决协议续期。
