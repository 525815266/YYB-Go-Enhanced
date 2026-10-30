# 2026-10-30 新反馈与候选方案复核

基于 v0.2.53，核对 #112 / #114 新评论、#115–#118，以及近期更新的开放和已关闭 issue。当前没有开放 PR。本轮没有确认可直接合入的持续授权修复，不更改生产握手或轮换策略，也不把补充文档作为协议已修复的新版本。

## 官方 WMPF demo（#116）

已读取 [wmpf/wmpf_demo_external](https://github.com/wmpf/wmpf_demo_external)，固定到 `c2f9272c6e746c7ec8f486804bb29f2a15fb185b`。该仓库为 MIT 的 Android Client demo；README 明确需要独立的 WMPF Service APK，demo 不包含该运行环境的完整服务端实现。

| 代码位置 | 实际行为 | 对本项目的意义 |
| --- | --- | --- |
| [GuideActivity.kt](https://github.com/wmpf/wmpf_demo_external/blob/c2f9272c6e746c7ec8f486804bb29f2a15fb185b/wmpf-demo/app/src/guide/java/com/tencent/wmpf/demo/activity/GuideActivity.kt) | 用 host AppID、productId、keyVersion、deviceId、signature 初始化 `WMPFBoot`；正式设备配置默认留空 | 设备身份属于宿主及设备配置，不能把任意样例值替换到现有 YYB / PCMgr 会话 |
| [ExperienceActivity.kt](https://github.com/wmpf/wmpf_demo_external/blob/c2f9272c6e746c7ec8f486804bb29f2a15fb185b/wmpf-demo/app/src/experience/java/com/tencent/wmpf/demo/experience/ExperienceActivity.kt) | 以体验 ticket 获取设备信息，初始化一次；同进程设备改变会拒绝，激活依赖正在运行的 Service | 可借鉴身份稳定与运行时存活的分离；体验模式明确不能用于生产，不能复制其 ticket 当通用授权 |
| [DocumentActivity.kt](https://github.com/wmpf/wmpf_demo_external/blob/c2f9272c6e746c7ec8f486804bb29f2a15fb185b/wmpf-demo/app/src/main/java/com/tencent/wmpf/demo/ui/DocumentActivity.kt) | 分开查询设备激活、`accountApi.isLogin`，通过 `accountApi.login` 取得宿主 OAuth code，另行启动目标小程序 | 宿主登录、设备激活与目标小程序 `wx.login` 不是同一个结果，宿主 OAuth code 不能当任意目标 AppID 的 code |
| [V1api.kt](https://github.com/wmpf/wmpf_demo_external/blob/c2f9272c6e746c7ec8f486804bb29f2a15fb185b/wmpf-demo/app/src/main/java/com/tencent/wmpf/demo/V1api.kt) | `authorizeStatus` 带设备 invokeToken，通过 `WMPFIPCInvoker` 调本地 Service；没有 bridge 时返回 DISCONNECTED | 这不是无需运行时的远程续期 HTTP 接口 |
| [设备签名工具](https://github.com/wmpf/wmpf_demo_external/tree/c2f9272c6e746c7ec8f486804bb29f2a15fb185b/wmpf-activate-util) | 为已登记设备签名、调用设备注册接口，使用开发者侧 AppID / Secret | 不是当前消费者账号授权的 AutoKey / EncKey 轮换实现，不能混用签名或密钥 |

可借鉴的方向是独立记录设备、宿主授权、运行时可达性与业务结果；若以后接入已激活的官方宿主，应作为明确依赖外部运行时的独立通路。本仓库没有提供可直接移植到 Linux Docker 的消费者账号长期续期实现，没有据此改换线上 host AppID、设备字段或增加猜测心跳。评论中“被针对打击”的描述缺乏可验证证据，不作为根因。

### 另行提供的 Python 下载包及扫码实测

#116 作者随后提供独立 runtime 下载包，并注明自己目前测试似乎也不可用。已检查源码、由用户完成真实扫码，并对照原转发函数：`/wxaruntime/login` 成功得到 session，但控制目标和包内默认目标的 `js-login` 均返回 HTTP 200 / `-12`，嵌套错误为 `no permission`，没有 code。该包走 HTTPS runtime 转发，不是当前 ILink 传输，也没有实现刷新或 AutoAuth；不能直接拿它替换现有渠道或宣称持续授权已解决。

已离线复现无关 protobuf 字段被当成 code、`-50` 被子串匹配成 `-5` 的问题；同时确认原错误处理漏读嵌套原因。完整通路、源码哈希、成功 / 失败阶段、同源实现及实验限制见[下载包实测记录](runtime-transfer-review-20261030.md)。没有启动原服务或自动删号，没有修改生产账号和扫码时间。

## 游戏中心候选（#117）

反馈目前只有文字，没有实现地址、接口、脱敏调用链或可复核记录。现有服务已区分 YYB / PCMgr 两条扫码及协议通路，但同一微信身份不等于两个互不干扰的独立授权环境。

PCMgr 静态调用者已确认可转交给已登录的桌面微信并跳过 WMPF 登录检查，详见[官方状态及启动通路](pcmgr-official-state-20261030.md)。这使“官方业务页面能打开”和“独立协议 code 持续有效”必须分开验证；也不能反过来断言所有游戏中心场景都走此分支。保持窗口打开是否触发了有效续期，仍需真实调用事件证明。暂不以此上线“无限保活”开关。

## 新日志按账号和授权分段（#112 / #114 / #115 / #118）

- #112 的 `09:31:50 pcmgr_login_buffer/6` 属于扫码前旧账号 3。新账号 1 于 `09:35:06` 扫码，`09:35:23` 成功取码，`09:39:04` 实际错误链为 `shortlink_application/-14 → native_autoauth_business/-13`。不能把旧账号的 `6` 拼成新账号同一次恢复的第三层，也不能把认证后的业务拒绝视为请求各字段必然完全正确。
- `09:39:04` 距上次成功 3 分 41 秒，距扫码 3 分 58 秒。两次请求只给出了成功和失败的采样点，不代表精确失效时刻或固定有效期。青龙请求是观察到失败的触发点，尚无证据认定该脚本撤销了授权。
- `09:41:05 recovery_deferred` 符合五分钟退避，没有再次发送 AutoAuth；本次仅返回原始 `-14` 不表示此前的 `-13` 消失或已修复。
- #114 / #118 的混合链已实际进入 AutoAuth 并被业务层拒绝，不能回复成“恢复代码没有执行”，也不能仅凭这些码确定代理不稳定或上游撤销的具体原因。
- #115 的单独 `-14` 仅说明当前应用请求被拒绝。`alive`、名义到期时间或本地 PSK 可解析，均不是此时业务成功的证明；同样也不足以判定必须清空整个账号。

`request_rejected` 的现有定义是**整个错误链仅为应用请求拒绝**，供账号级恢复保护使用。混合 `-14/-13` 为 `false`，不表示取码没有被拒绝，也不表示业务成功。它不是“错误链中是否出现过任意拒绝”的布尔值，排查应读取 `upstream_errors` 和相应恢复事件。本轮明确这一语义，不为修正文案改变恢复判定函数。

## “v0.2.46 正常，升级后失败”的对照（#118）

已对比 `v0.2.46..v0.2.53` 的 `pool.go`、`ilink.go`、`shortlink.go`、取码恢复和 HTTP 调用层：

| 项目 | v0.2.46 | v0.2.53 |
| --- | --- | --- |
| YYB 首次 js-login 构造 | `buildJSAPIPlaintext` 后加密传输 | 同一构造函数和请求字段；新调用 helper 将空 code 明确作为错误 |
| PCMgr | 尚未正式加入双渠道产品能力 | 独立 host、扫码及渠道 API 路径；不能用 v0.2.46 的 YYB 结果代替 PCMgr 对照 |
| 单独应用 `-14` | 作为 payload 类错误，可删除共享会话并用旧票据重建；后续可能进入账号凭据恢复 | 保留原请求错误；取码优先原生恢复，否则取新票据并验证候选；失败不会仅因应用拒绝清空账号 |
| 原生密钥 | 未保存独立恢复材料 | 保存设备 / AutoKey / EncKey，同出口 CAS 标记尝试并持久保存轮换，禁止重放不确定请求 |

因此升级改变了恢复行为，反馈必须保留为回归线索；但日志中的原请求 `-14` 出现在恢复 `-13` 之前，不能把新增错误文字单独当作首次失效由新版造成的证据。也不能排除新增握手影响后续上游状态。现有材料不足以确定回归根因或承诺回退有效，需要同一渠道 / 目标 / 出口以及首次失败前后的授权时间线。不建议通过生产降级、清库、连续重扫或重放已尝试密钥制造对照。

### #118 原帖作者补充：始终为 YYB，目标为君品荟

北京时间 13:42–13:43，原帖作者补充自己的 `shortlink_application/-14 → native_autoauth_business/-13` 错误链，明确一直使用 YYB、没有改用 PCMgr，并给出 `wx8d41cdc44c8aeaab`。该目标在仓库 `scripts/君品荟_code版.py` 的 `MINI_APP_ID` 及多个公开脚本中对应君品荟。现在可以排除这位反馈者的渠道切换；其 YYB 负结果也不能与 #112 的 PCMgr 授权混成同一次事件。

已单独比较 `buildJSAPIPlaintext`、`sessionWpkgHead`、`buildTransferPacket` 三个函数，v0.2.46 与 v0.2.53 的函数源码相同；当前恢复入口同样按传入目标构造请求，没有针对该 AppID 的特殊失败分支。这只缩小静态代码差异，不代表运行时状态、恢复流程或上游行为相同，仍不能排除升级回归。

错误发生在微信取码及独立恢复阶段，尚未收到君品荟业务登录结果。脚本中另一套 garden 业务使用不同 AppID 的事实，不能解释这次协议层 `-14/-13`，不据此擅改对方的目标。AutoAuth 已执行但被拒绝，不等于恢复成功，也不直接证明 OAuth 已失效。

本轮定向执行协议 / HTTP 层的取码候选提交、失败保留原会话、退避、受限授权及 AutoAuth 字段 / 响应检查，测试通过。未对反馈者的授权进行线上复现；本地测试通过不能证明其问题已修复。仍缺当前精确版本、授权是否在升级前已使用，以及同账号已知可用目标的同期结果；已回复确认收到新信息，继续保留 issue，不要求重复提供渠道或已有 AppID。本轮只更新调查记录，未改生产恢复策略。

## 本轮正式环境新增证据与验证边界

本轮只读核对现有 v0.2.53 正式日志及会话摘要，没有发起额外取码、AutoAuth、票据请求或强制续期。

维护环境单账号在 12:30–13:00 之间已有外部正常调用；日志包含退避及多次 `pcmgr_login_buffer/6`。13:00:36 的正常调用开始恢复并请求新票据，13:00:37 记录 `protocol.recovered`、`protocol.succeeded`；正式会话更新时间同步变化，扫码与账号授权摘要保持原值。该次没有 `native_rotated` 事件，属于**按需取新票据并验证新会话**，不能当作 AutoAuth 成功、后台凭据续期或长期保活成功。名义 PSK 新期限也不能独立证明真实业务寿命。

这是拒绝后出现恢复成功的证据，说明同一类错误不能一律写成永久不可恢复；也不代表后续稳定，之前的 `-13` 与票据拒绝负结果继续保留。本轮未将这些少量观察推断为服务端固定规则。

本地 `go test ./internal/protocol ./internal/httpapi ./internal/eventlog ./internal/store` 通过，覆盖现有错误链、恢复退避、会话快照 CAS、轮换防重放及日志安全边界；没有把这些离线回归当成线上续期成功。本轮为调查与文档更新，正式仍为 v0.2.53，不因没有已证实的运行时修复而额外重启容器。
