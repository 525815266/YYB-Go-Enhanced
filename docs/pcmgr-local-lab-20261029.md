# YYB / PCMgr：双渠道实现与协议实验

2026-10-29，v0.2.47 接入双渠道，v0.2.48 增加原 ID 切换；下文保留早期 Windows DLL 对照实验，并补充后续纯 Go 实现。正式程序不依赖 Windows DLL，不读取本机 PCMgr 登录配置。

## 正式扫码与默认设置

- `/scan` 可选择 YYB / PCMgr；切换取消旧二维码，不接受旧轮询结果。扫码确认后自动保存账号并按原设置同步青龙。
- `/settings` 管理个人默认渠道；管理员可管理系统默认。本次手选优先于个人默认，个人默认优先于系统默认；本机模式支持系统默认。
- 新增账号短链接继承创建者默认，也可在扫码页切换。更新链接默认沿用原渠道，可在创建弹窗或扫码页显式确认切换，成功后在原 ID 替换授权，不新增 ID。保留代理、归属、任务、推送及备注；旧会话、UIN、旧更新链接和内存租约清理，旧请求保存会话时校验授权快照。
- 同渠道仍检查原 OpenID；跨渠道无法仅凭 OpenID 证明是同一微信，弹窗要求使用原微信，不按昵称匹配。新身份已在其他 ID 下时拒绝覆盖。事务提交身份替换和链接消费，失败不改原账号。面板旧 OpenID 引用迁移、数字 ID 保留；故障提示并持久化待重试引用，常规同步可继续处理。
- 管家账号使用服务端稳定 account ID 组成 `pcmgr:<accountId>` 引用。私有 key、GUID、产品 OpenID 不通过公开账号接口返回；既有应用宝账号保持原引用。
- 管家的下一轮续期计划与真实有效期分别处理；未知续期间隔先安排一天后检查，已知间隔按官方静态分支的一半安排，小于 15 天的间隔安排一分钟后检查。不把该计划或 `next_renew_len` 当作已验证的凭据寿命。

## 纯 Go 网络实现

### v0.2.48 的切换与误新增修复

更新链接在创建时保存本次渠道及确认状态，扫码页可重新选择；未确认的跨渠道请求在创建二维码前拦截。授权成功后用事务更新原 ID、清除旧会话与 UIN、消费更新链接并释放编号；重复确认、旧链接及其他 ID 已占有的新身份均不得覆盖正常记录。协议池保存与清理会话时检查授权快照，兼容取新票据而不改持久凭据的按需恢复。

误新增记录通过“合并账号”预览后，将来源授权移入目标 ID。目标配置保留，来源 ID 及其归属记录清理；跨用户归属、来源不可用、预览后重新授权或来源已有专属任务时拦截。面板数字 ID 与旧 OpenID 引用会迁移，失败保存待重试记录；若旧引用已被其他账号复用则停止自动修改，避免误改新账号。

本地验证通过：完整 Go tests、vet、`protocolaudit` 离线回归，旧数据库迁移、双向替换、并发消费、旧会话写回保护、归属保护、面板故障重试与编号复用保护。三组浏览器回归覆盖 320 / 390 / 768 / 1024 / 1440 像素界面、切换取消及确认、合并预览失效，以及 24 个虚构账号下调用结果出现及后台刷新时视口位置保持。README 截图继续仅使用虚构资料。

v0.2.48 发布与部署验收：

- Docker、原生 Release、Magisk 云端构建均通过。正式 Release 16 项资产齐全，包含七架构单文件、七完整包、Magisk 和 SHA256 清单。
- 下载正式 Windows amd64 单文件并核对 SHA256，实际启动、内嵌模板、鉴权均通过。另在全新目录运行该正式二进制，三组渠道 / 合并 / 视口及响应式浏览器回归均通过，测试账号为虚构资料。
- 飞牛部署精确发布镜像，Linux amd64 manifest 为 `sha256:af679347bf62ea2f62c05d3ef8bb9e85f35830aa0f287745103408fc985b244f`。直连下载较慢时，通过代理取得同一官方镜像，逐层核对压缩及解压 SHA256 后导入；部署前隔离启动及鉴权校验通过，备份数据库与配置并保留可回滚镜像。正式容器健康，22 个既有账号 ID 与身份、容器配置及扫码默认均保留。
- 正式 API 验证两渠道真实二维码生成并取消；另创建未消费的更新测试链接，未确认切换返回 422，确认后默认渠道及原渠道均可生成真实二维码，再取消二维码、作废该测试链接。未扫码、未合并真实账号，不能将这些检查当成新扫码完整闭环或长期保活验证。

以下为既有网络实现，不代表已完成长期自然续期验证。

扫码使用管家自己的 HostAppID、runtime scope 与 code 兑换端点 `https://jprx.m.qq.com/data/3078/forward`；不经过应用宝 OAuth 回调。原生请求通过 `http://conna.gj.qq.com/` 发送 RSA 握手和 AES-GCM 密文，并验证响应认证标签、指令、序号和消息长度。账号代理沿用既有 transport；业务密钥轮换不自动重发，收到新 key 后先持久化。

本地纯 Go 实测：`20138` → ILink 新会话 → code 长度 32；随后 `20042` 轮换私有副本的 key → `20138` → 另一新会话 → code 长度 32。该实验不调用 Windows DLL。历史 DLL 只作为协议对照依据；正式 Linux / Docker 服务使用同一纯 Go 实现。

尚待验证：用户新扫码后的 `3078` 完整闭环、Linux 正式部署后的新授权、自然到期后的持续续期及长期寿命。主动轮换成功不能替代这些验证，也没有修复应用宝已被上游拒绝的旧凭据。

### v0.2.47 发布后交叉验证

- 飞牛 Linux amd64：使用隔离的当前管家授权副本，通过纯 Go `20138` 取得新票据、创建新 ILink 会话、小程序取码长度 32。未将该实验账号导入正式数据库，没有续期、重扫或修改生产 ID1。
- 同一旧实验 key 在 Windows、飞牛均返回业务码 `2`；重新只读取得官方客户端当前授权副本后，飞牛上述完整请求通过。保留这一负结果，不能把本地存在的 key 文件当作仍有效，也不能将这次码 `2` 单独归因为 Linux 兼容问题或推导 key 寿命。Windows 的新副本另有一次网络超时，未据此清空账号。
- Windows 正式 Release 的 amd64 单文件：校验 SHA256 后，在隔离目录实际启动 v0.2.47，登录、扫码渠道控件、个人设置控件、默认渠道保存均通过，不依赖外置网页目录。
- GitHub 正式 Release 保持 16 项资产：七种架构单文件、七种完整归档、Magisk ARM64 模块及 SHA256 清单。普通与实验构建测试、`go vet`、DLL 桥接解析回归，以及浏览器渠道切换、过期轮询隔离、默认优先级和原工作台回归通过。
- 飞牛正式容器已更新 v0.2.47，镜像 digest `sha256:e32930aa5c56fa872ed4e523a2be5f3af4da43eb34fae90ab970e2d1cb47f0c6`；隔离启动、正式健康检查、版本及配置校验通过，原 21 个账号的 ID 与身份不变，升级前数据库及配置已备份。正式 API 分别生成两渠道的真实二维码并取消，未创建占位账号；系统默认设为电脑管家，设置页与扫码页可用，局域网访问登录页返回 200。仍需用户扫码完成新授权及后续自然续期观察。

## 两条路径

| 项目 | 应用宝 `yyb` | 电脑管家 `pcmgr` |
| --- | --- | --- |
| HostAppID | `wxd44977328b36e647` | `wx5cd60c5d4817a188` |
| 上游凭据 | 应用宝 OAuth access / refresh token | 管家 account ID、loginkey、client GUID |
| 登录票据 | 应用宝登录票据接口 | 原生网络指令 `20138` / GetLoginBuffer |
| 凭据续期 | 应用宝 refresh-token / check-login | 原生网络指令 `20042` / RenewLoginkey |
| 本地状态 | `STATE_ROOT/yyb/session.json` | `STATE_ROOT/pcmgr/credentials.json`、`ticket-response.pb`、`session.json` |
| 共享部分 | ILink / MMTLS 传输实现 | ILink / MMTLS 传输实现 |

凭据格式、HostAppID、会话标签及目录分别校验。缺少 HostAppID、产品不符或会话过期时直接拒绝；没有跨产品回退，不会用管家授权替换应用宝 ID1。已有账号继续沿用原渠道。共享传输实现能完成此次请求，不表示所有管家原生参数均已逐字段对齐。

## 已完成的真实验证

| 本地实验 | 实际结果 | 能证明什么 |
| --- | --- | --- |
| 管家登录态 → `20138` → ManualAuth → 小程序取码 | 成功，code 长度 32 | 管家自己的票据可建立可用的独立会话 |
| 退出实验进程后读取保存的管家会话 | 成功，code 长度 32 | 本地会话可持久化复用 |
| 管家旧 key → `20042` | `result=0`，返回新 key，账号与类型一致 | 官方 key 轮换指令实际可用 |
| 新 key → `20138` → 新会话 → 取码 | 成功，code 长度 32 | 新 key 可用于完整登录链路 |
| 将上述步骤整理为仓库内桥接工具后重新执行 | 导入、续期、取票据、取码均成功 | 不是仅一次性临时脚本成功 |
| 主动续期后重读官方客户端状态并使用其原 key 取票据、取码 | 成功；原生登录时间与原 key 未改变 | 本次实验未改官方配置，原 key 在该时刻仍能使用 |
| 应用宝 ID1 先前保存的原会话，在本地 `yyb` 目录复测 | 成功，code 长度 32 | 原 ILink 会话在该时刻仍可用；没有恢复生产账号或 OAuth 凭据 |

一个放置数分钟的旧管家票据曾收到 `ManualAuth -10`，重新按需获取后通过。因此不长期囤积 login_buffer；这一个样本不能推导固定的票据 TTL。

上述表格记录前置 DLL 实验；后续纯 Go 结果见本页开头。仍未验证管家 key 自然到期后的自动续期、长期有效期、30 天保活，以及新扫码 code 到管家登录凭据的完整闭环。DLL 桥接工具只用于本地对照，不捆绑或上传官方 DLL。

此次是显式提前调用续期接口的受控实验，不能写成后台自然续期成功。应用宝 OAuth `-109 / 42007` 的原问题也没有因此消失，详见[第四轮终止记录](native-keepalive-audit-20261028.md#第四轮终止oauth-拒绝与仍可用的原协议会话15411544)。

## 本地复现

需要 Go 1.23+、64 位 Windows Python，以及已登录微信的电脑管家 `18.2.30865.218`。桥接工具固定校验三个 DLL 的 SHA256；升级后样本不符会停止，需要重新核对 ABI，不能关闭校验硬试。

在仓库根目录的 PowerShell 执行：

```powershell
go build -tags protocolaudit -o .codex-tmp/protocol-lab.exe ./cmd/protocol-lab
$labRoot = Join-Path (Get-Location) '.codex-tmp/protocol-lab'

# 首次导入到管家专用凭据文件；已有文件会拒绝覆盖。
python tools/pcmgr_local_bridge.py --state-root $labRoot --import-native --fetch-ticket
if ($LASTEXITCODE -ne 0) { throw '管家取票据失败' }

& ./.codex-tmp/protocol-lab.exe --provider pcmgr --state-root $labRoot `
  --ticket-file (Join-Path $labRoot 'pcmgr/ticket-response.pb')

# 复用管家会话：不刷新凭据，不重新登录，不在失败时自动回退。
& ./.codex-tmp/protocol-lab.exe --provider pcmgr --state-root $labRoot

# 显式实验：只轮换私有副本中的 key，随后取新票据。
python tools/pcmgr_local_bridge.py --state-root $labRoot --renew-key --fetch-ticket
if ($LASTEXITCODE -ne 0) { throw '管家续期或取票据失败' }
& ./.codex-tmp/protocol-lab.exe --provider pcmgr --state-root $labRoot `
  --ticket-file (Join-Path $labRoot 'pcmgr/ticket-response.pb')

# 仅检查官方客户端状态；没有业务网络请求，不输出 key / 账号身份。
python tools/pcmgr_local_bridge.py --state-root $labRoot

# 应用宝：显式提供自己的新 base64 票据文件；不读取生产数据库。
& ./.codex-tmp/protocol-lab.exe --provider yyb --state-root $labRoot `
  --ticket-file 'C:/private-lab/yyb-login-buffer.txt'
& ./.codex-tmp/protocol-lab.exe --provider yyb --state-root $labRoot
```

每次重新取票据只需 `--fetch-ticket`，不需要重复导入客户端凭据；轮换后使用私有文件中的新 key。成功轮换先原子保存，后续取票据失败不会丢失已经下发的新 key。失败的新取票据操作清除旧响应文件，不用旧文件冒充本次成功。实验失败保留已保存的会话，不会清空正式账号。

工具仅输出产品标识、成功状态、固定错误码、code 长度、PSK 期限等摘要，真实 code 不输出。管家私有目录收紧到当前 Windows 用户 SID，凭据文件只留本地；不要分享该目录。仓库忽略 `.codex-tmp/`，不提交真实票据、会话或密钥。

离线回归：

```powershell
go test ./...
go test -tags protocolaudit ./internal/pcmgr ./internal/protocol ./cmd/protocol-lab
python tools/test_pcmgr_local_bridge.py
```

覆盖产品隔离、标签与 HostAppID 不一致、过期会话、重复字段、截断 protobuf、错误 wire type、错误码溢出不得变成成功、续期身份不符、空新 key、续期拒绝不替换原 key。普通测试与正式二进制不触发这些真实网络实验。

## 原生协议依据

管家 WMPF RPC 来自 `Qt64/MAL.exe` 中的 `wmpf_login_code.proto`；账号续期结构来自 `QMTrayPlugin/QMLoginPlugin/QMLoginPlugin.dll` 的 `gjaccount_server.proto`。指令号来自真实网络发送调用点，不将 native command 猜成 jprx HTTP route。

| 消息 | protobuf 字段 |
| --- | --- |
| GetLoginBufferReq | 1 accountid:int64；2 loginkey:string；3 client_guid:string；4 authcode:string；5 pcmgr_tmp_token:string |
| GetLoginBufferResp | 1 result:int32；2 msg:string；3 login_buffer:string；4 pcmgr_tmp_token:string；5 pcmgr_openid:string |
| RenewLoginkeyReq | 1 type:enum；2 accountid:int64；3 old_loginkey:string；4 client_guid:string |
| RenewLoginkeyResp | 1 result:int32；2 login_type:enum；3 accountid:int64；4 new_loginkey:string；5 next_renew_len:int32；6 third_party_info:message |

本地登录结构的微信类型是 `16`，续期消息里的账号类型是 `2`，两者不能混用。`20138` 使用 accountid + loginkey + 小写十六进制 GUID 的已验证路径；未把临时 token、authcode 分支宣称为已测试。

32 位登录插件中，`RequestRenewLoginkeyValue` 的请求构建始于 RVA `0x5b080`，实际发送 `20042` 的调用位于 `0x5b5ae`；成功响应处理保存新 key 和 `next_renew_len`，失败不会凭空延长凭据。RVA `0x62303..0x624a6` 的一条检查路径读取登录时间、续期时长及 `defLoginPatchRunOnce`：经过时间超过时长一半，或时长小于 `0x13c680`（15 天）时进入续期；其他情况存在补丁标记的一次性分支。这是静态分支证据，尚未量测完整后台定时调度。

当前官方配置与两次实际续期响应均出现 `next_renew_len=5097600` 秒（59 天）。它是“下次续期时长”字段，不是通过实测证明的 key 有效期，更不是“小程序 code 能保活 59 天”。

核对样本 SHA256：

| 样本 | SHA256 |
| --- | --- |
| QMCommon64.dll | `307f596fe65f7012dad5ba83ed8f14cdff426c743a3d38e1993e062d1e189b6a` |
| QMGuid64.dll | `216098312549bce826d59fb2aa696233443745fa51be6fc03688fbfb6c72d858` |
| QMNetworkMgr64.dll | `e9b54b7f7dba30a0aa65b8da47f96ce7552ce3bf57c23625ca0491ebe89294e4` |
| Qt64/MAL.exe | `ce4b4acd5b7b472819f98eb321eedd44d9a4aa15b81f2ad85e5c50d791bd55bb` |
| QMLoginPlugin.dll | `f301b2978325e4e0844252948292c957cde4ffac56dcaaa57338d67e63f1043f` |
| 管家 runtime/ilink2.dll | `46a0ac2eb34ef2e781a68aa4c6bbe5bd5ed1860218c6b4cbdf51ce81d78247c6` |
