# PCMgr 调用路径复核（2026-10-29）

v0.2.49 修复已确认的产品身份混用，并按原生 runtime 构造 PCMgr 手机号请求。旧授权存在独立取票据拒绝；用户重新扫码后，飞牛正式版本的取码、密钥与手机号响应通过下述限定范围复测，不能据此宣称全部接口或长期续期已恢复。

## v0.2.51 续期差异追踪

2026-10-29 晚间重新读取本机官方登录态后，使用该次当前 key 的原生 `20138` 成功。响应含登录票据和 WMPF 身份，但没有 `pcmgr_tmp_token`；本机 `defWmpfPcmgrTmpToken` 同时未配置。因此“Go 未保存响应字段 4”仍是静态功能差异，本次样本不能验证临时 token 分支，更不能把补存字段当成已解决续期。没有使用旧私有副本中的 key 来替代本次读取。

当前票据建立新 PCMgr 会话并实际取码成功。进一步用这次新会话取得的 AutoKey / EncKey 发送隔离 `nativeidentity` AutoAuth 候选（网络身份等于应用身份，字段 18=0、25=17、7=0），收到命令 3198、传输字段 4 为 `-20011`、应用字段 23 为 0，响应没有加密正文。它仍是失败，不是授权续期成功；没有重放该 AutoKey 或把候选接入生产。PCMgr 当前 runtime 与 YYB 样本的 DLL 哈希、地址布局不同，不能跨样本直接套 RVA。

另用飞牛 ID1 的当前授权私有副本复现了可修复的不同故障：旧缓存取码 `-14`，原凭据仍能取得新票据；新会话取码及后续缓存复用均成功。v0.2.51 由此增加“候选先验证、成功后替换”的受限取码恢复，具体保护及证据见[授权与会话分层](credential-session-layers.md#取码卡在旧会话的恢复)。该恢复不调用 `20042`，不修改扫码时间，不证明 OAuth 长期续期。

23:09 正式飞牛 v0.2.51 验证通过：ID1 取码触发候选验证并替换旧缓存，之后密钥、手机号和再次取码均 HTTP 200；与升级前备份比较，授权 / 登录票据 / 扫码时间保持不变。ID2 取码成功且原缓存未变化。正式 Windows 启动、多架构资产与镜像部署记录见[验收记录](credential-session-layers.md#正式发布与飞牛验收2026-10-29-2309)。这一次可恢复的缓存失败已处理，不能据此关闭 OAuth 提前撤销或长期续期问题。

## 原生程序的分层

安装样本为 PCMgr `18.2.30865.218`。主目录中的 `Qt64/MAL.exe` 负责 WMPF 登录集成；实际小程序 API 位于当前用户目录 `Tencent/QQPCMgr/radiumv3/wmpfsdk/14673/runtime/flue.dll`，不在主程序的普通网页登录接口中。

| 层级 / 调用 | 原生路径与依据 | 本项目对应 |
| --- | --- | --- |
| 扫码 code 兑换 | `jprx.m.qq.com/data/3078/forward` | 独立 PCMgr 凭据，不能替代 YYB OAuth |
| 登录 key 轮换 | `QMLoginPlugin.dll`，指令 `20042` | `pcmgr.Renew`，先保存新 key |
| 小程序登录票据 | `MAL.exe`，`GetLoginBufferReq`，指令 `20138` | `pcmgr.FetchBuffer`，不是具体业务 API |
| 小程序取码 | `flue.dll`，函数 RVA `0x12ca0b0`，URL 引用 `0x12ca366`，`/cgi-bin/mmbiz-bin/js-login` | `/wx/code`，指令 `1029` |
| 通用数据转发 | `flue.dll`，函数 RVA `0x12bbc10`，URL 引用 `0x12bc07e`，发送点 `0x12bc0dc`，`/cgi-bin/mmbiz-bin/js-operatewxdata` | `operateWxData`，指令 `1133`（`0x46d`） |
| 手机号能力 | `flue.dll`，函数 RVA `0x12bc1f0`，URL 引用 `0x12bc415`，发送点 `0x12bc479`，`/cgi-bin/mmbiz-bin/js-getuserwxphone` | PCMgr 使用 `1141`（`0x475`）及 JSON 体，需目标小程序授权 |
| A8Key | `flue.dll`，函数 RVA `0x12bf760`，URL 引用 `0x12bfa65`，发送点 `0x12bfa77`，`/ilink/ilinkapp/mm/ilinkgeta8key`，指令 `5112`（`0x13f8`） | 独立原生路径，尚未接入；不能用 `1133` 通用转发代替 |
| A8Key Lite | `flue.dll`，函数 RVA `0x12fbab0`，URL 引用 `0x12fbdfc`，发送点 `0x12fbe0e`，`/ilink/ilinkapp/mm/ilinkgeta8keylite`，指令 `4744`（`0x1288`） | 独立原生路径，尚未接入；请求结构仍需单独验证 |

`flue.dll` SHA256：`0248de2199ec48db63fe182854e3f58e5d799322558f2e1b5e8fe552b5467615`。`MAL.exe` 和登录插件哈希见[双渠道实验](pcmgr-local-lab-20261029.md)。以上地址均为样本 RVA，换版本需要重新核对。

## 已确认的构造错误

旧代码只有 `getCode` 把会话 HostAppID 带入外层 transfer；手机号与通用转发传入空值，最终回退至 YYB HostAppID。PCMgr 会话因此混用产品身份。现在三条路径均使用当前账号会话的 HostAppID，旧 YYB 和无标识兼容行为保留。

PCMgr 原生手机号走 `1141`，沿用 `JSOperateWxDataRequest`，字段 2 为目标 AppID、字段 3 为 JSON；旧实现却使用 `1020` 和类似 `js-login` 的请求体。原生调用处及序列化函数 RVA `0x178b510` 共同确认字段布局，v0.2.49 拆分该构造。通用转发的原生 builder 默认 `versionType=1`，位于请求字段 6；YYB 路径保留原有值。

手机号请求当前使用最小 `webapi_getuserwxphone` JSON，不实现查询或选择微信内多个手机号。字段对照与离线测试不能证明某个小程序已授予手机号能力；新授权后已验证一个目标 AppID 的响应，范围见下文，不保证任意小程序可用。

## 为什么多个测试入口同时失败

本次现场先后观察到：

1. 飞牛 PCMgr 记录显示 `alive`，但旧票据建立会话返回 `ManualAuth -10`。
2. 纯 Go 重新调用 `20138` 返回业务 `1`；轮换 key 可以成功，但随后的 `20138` 仍是 `1`。这是两种不同凭据能力，不能合并成“续期成功，所以接口可用”。
3. 本机通过已核对 ABI 的官方 DLL、只读取得当前登录信息后调用同一指令，亦返回 `1 / fail`，没有 buffer 或临时 token。因此没有证据将其归为仅 Linux / Docker 的编码问题。
4. 先前保存的隔离 PCMgr 会话取码基线返回短连接 `-14`；对照工具因此跳过后续密钥与手机号请求，没有用失败会话伪造成功结果。

这些拒绝发生在具体小程序 API 执行前，无法据此判断每个 API 都不支持。`alive` 在这里表示账号凭据状态，不证明 WMPF 会话可用；PCMgr 的账号资料接口也只读取保存资料。

v0.2.49 遇到 `20138/1` 时保留完整错误链，停止每次接口测试都自动轮换 key。保留账号、凭据和原周期，不把通用 `fail` 强行解释为账号撤销。后台正常续期调度不变。

### 原生存在但尚未接入的授权分支

`MAL.exe` 还包含获取后端 login_code、拉起 OpenSDK 额外授权以及使用 `authcode` / `pcmgr_tmp_token` 的分支。缓存 token 分支函数 RVA `0x47eb0`，读取 `defWmpfPcmgrTmpToken` 后发送 `20138`（发送点 `0x4835f`）。本次只读读取的当前配置未提供缓存 token，没有重放历史授权 code，也没有把该分支当成已上线的自动修复。

新扫码后已恢复本次小程序取码，尚不能确定旧 key 票据拒绝是否与该分支相关；静态存在分支不等于它已经能解决 `1/fail`，没有据此上线猜测的 authcode 或临时 token。

## 新扫码后的真实复测与发布验收

用户完成 PCMgr 更新扫码后，先在升级前验证 `/wx/code`：HTTP 200、code 长度 32，账号授权不变。随后从只读数据库复制现有缓存会话到隔离私有文件，仅发送请求，不取新票据、不续期、不写正式数据库：

| 顺序 | 真实结果 |
| --- | --- |
| 取码基线 | code 长度 32 |
| 旧 YYB HostAppID / 版本字段的密钥请求 | `err_no=0` |
| 修正 PCMgr HostAppID / 版本字段的密钥请求 | `err_no=0` |
| PCMgr 原生手机号构造 | 返回 `code`、`encryptedData`、`iv`、`cloud_id` 与结构化 `data`，没有公开任何值 |
| 再次取码 | code 长度 32 |

该对照确认新构造可请求，但旧构造在这次密钥调用中同样被接受，**不能把先前所有接口失败归因于 HostAppID 混用**。旧授权的 `20138/1` 与请求构造问题分别保留，未证明长期凭据寿命或额外授权分支。

正式 v0.2.49 发布后，飞牛实际执行 `code → getlatestuserkey → getphonenumber → code`：四次均 HTTP 200；两次小程序 code 长度 32，密钥 `err_no=0`，手机号返回非空 `code` / `encryptedData` / `iv` 且 `need_auth=false`。测试前后的账号身份、凭据、登录票据与状态逐项比较一致，没有轮换 key 或重扫。手机号 code 没有提交给目标业务后端，不能据此证明任意业务登录或其他 AppID 的授权。

发布与部署验收：

- main 及版本标签的原生、Docker 构建通过；Release 16 项资产齐全，包括七种架构单文件、七种完整包、Magisk ARM64 和 SHA256 清单。
- 正式 Windows amd64 程序校验 SHA256 后实际启动，版本、内嵌模板与协议鉴权通过。
- 飞牛使用精确源码 `13efaa8ea973920966e61a73f94ddf4545ed3d35` 的官方镜像，Linux amd64 manifest 为 `sha256:8e495a2c7e5bcf889a220dbb6493eb9231a527147f6198c99fc69ece6497eef0`，逐层压缩与解压摘要均核对。隔离启动通过后备份数据库与配置再部署，保留可回滚镜像。
- 正式容器健康，22 个账号的 ID 与身份及原容器配置保持；未合并或复活其他账号。部署后上述真实协议复测通过。

## 面板能力边界

| 入口 | 当前含义 |
| --- | --- |
| code | 小程序 `wx.login`，不同于原生 APP OpenSDK / 公众号 OAuth |
| getuserinfo | 渠道账号资料；PCMgr 为已保存的扫码资料，不检测小程序登录态 |
| encryptkey / getlatestuserkey | `1133` 转发真实 `webapi_getuserencryptkey` payload，以内层业务状态判断 |
| getphonenumber | 按渠道构造的手机号能力，PCMgr `1141`，YYB 原兼容 `1020`；不能凭路由存在宣称授权可用 |
| cloud | 通用 `operateWxData` 转发，不自动补齐业务参数 |
| mpgeta8key / appmsgext / appmsglike | 未验证的兼容转发，不是已恢复的独立文章协议 |
| oauth | 只生成授权 URL，尚无直接取得公众号 code 的完整实现 |
| qrcodeauth | 按选中渠道新建二维码，仍需扫码完成登录 |

控制台展示渠道及简短说明；密钥入口提供正确最小示例，其他入口要求真实业务 JSON。外层 HTTP 200 / `code=0` 只代表转发完成，解析到内层非零 `err_no`、`errcode` 或 `ret` 时显示上游拒绝，不显示调用成功。

## 复核

本地已通过 `go test ./...`、`go vet ./...`、协议审计标签测试及发布说明单元测试；界面用虚构账号验证 1440 / 390 / 320 宽度、渠道提示、内层拒绝、二维码渠道、自定义参数保留，并回归原有扫码切换、账号合并与调用后滚动位置。首次旧私有会话基线 `-14` 的跳过记录保留；新扫码后另行完成了上述真实对照，未将首次跳过计为通过。

```powershell
go test ./...
go vet ./...
go test -tags protocolaudit ./internal/pcmgr ./internal/protocol ./cmd/protocol-lab

# 可选真实对照，只读取明确指定的私有 PCMgr lab 会话。
# 不打开正式 DB、不取新票据、不续期；取码基线失败即跳过后续操作。
$env:YYB_PCMGR_JSAPI_SESSION = 'C:/private-lab/pcmgr/session.json'
go test -tags protocolaudit ./internal/protocol -run '^TestPCMgrCachedJSAPIComparison$' -count=1 -v
Remove-Item Env:YYB_PCMGR_JSAPI_SESSION
```

输出仅含固定操作名、成功标识、码长度与数值错误码，不输出 code、密钥、账号身份或响应正文。基线失败后的 `SKIP` 是验证未完成，不能写成接口通过。两渠道的长期自然续期仍未由本次测试证明。
