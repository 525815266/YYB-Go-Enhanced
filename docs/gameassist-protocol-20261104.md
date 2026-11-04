# 手游助手（GameAssist）渠道接入复核（2026-11-04）

本记录说明 v0.2.68 新增的第三条扫码渠道 `gameassist`（腾讯手游助手）的协议依据、接入范围与未验证边界。安装样本为本机 `E:\Program Files\Tencent\GameAssist`，版本 `6.10.7110.578`。

## 结论

手游助手 6.10 是应用宝 Androws 源码树的改版（二进制内残留源码路径 `E:\workplace\Androws\...`），**WMPF/ILink 取码栈与 YYB 渠道完全同构**，因此本项目只新增"凭据签发链"，login_buffer 之后的 ManualAuth、短连取码、会话恢复全部复用：

| 项 | 手游助手 | 应用宝（现有 appstore） | 依据 |
| --- | --- | --- | --- |
| WMPF host_appid | `wxd44977328b36e647` | 同左 | GameAssist.exe 与 AndrowsStore.exe 中同串出现 |
| ilink_appid | `ilinkapp_060000b7b93f6c` | 同左 | 同上 |
| 小程序取码栈 | `wmpf_host_export_x64.dll` → `ilink2.dll`（mmtls ManualAuth）+ `flue.dll`（js-login） | 同款 WMPF runtime（`Androws\WmpfRuntime\5.10.7300.414`） | GameAssist.log 有 `LoadWmpfSdk` 与 `wmpf_helper::ThirdAppLogin` 日志串 |
| OAuth appid | `wxef99873dbfab493c` | `wxd44977328b36e647` | GameAssist.exe qrconnect 页构造 + GameAssist.log 打点 |
| OAuth 回调 | `yybadaccess.3g.qq.com/syzslogin/syzs_oauth?login_type=wx&domain=1&syzs_prefix=` | `/pc_yyb/pcyyb_oauth?login_type=WX` | 同上 |
| 凭据形态 | `openId(oLvh-…) + accessToken "1_…" + refreshToken "1_…" + loginType/scope/expireTime` | 同形（cookie 签发） | GameAssist.log 2026-11-03 23:54 登录态实录（`SetLoginParam` 载荷） |
| 网关 | `yybadaccess.3g.qq.com`（备 `yybadaccess.sparta.html5.qq.com`） | 同左 | 两 exe 内 URL 全集 |

## 证据

1. **符号级同源**：GameAssist.exe 与 AndrowsStore.exe 的 WMPF/ILink 符号集逐条相同（`ILinkAuthManager`、`IlinkAuthBuffer`、`wmpf.IlinkAuthBuffer` 等 protobuf 全家、`web_object::WXApplet::{InitSdkV4,LoginV4,GetLoginStateV4,LaunchAppletV4}`）。
2. **登录态实录**（`logs/GameAssist.log`）：`{"username":…,"openId":"oLvh-…","accessToken":"1_…","refreshToken":"1_…","loginType":"WX","scope":"snsapi_login,snsapi_runtime_pcsdk","expireTime":1793728454,…}`；`wxapp*` 字段为空的第二凭据槽是留给"本机微信客户端探测"（`localhost.weixin.qq.com:13013-14015/api/check-login`）的备选通道，与 WMPF 无关。
3. **端点探测**（2026-11-04，假 code）：`pcyyb_oauth` 返回 `{"code":-107,"msg":"RC_NO_AUTH_ERR","user_info":null,"ext_info":null}`；`syzs_oauth` 返回 `{"code":-109,"msg":"RC_PARAMS_INVALID","userInfo":null,"extInfo":null}` —— 同一网关、同信封结构，仅字段驼峰差异。
4. **排除项**：全目录无 WMPF/微信运行时组件（`WmpfRuntime` 按需下载，本机未装）；VM 镜像内无 `com.tencent.mm`；凭据链不经过安卓模拟器。

## 接入设计

- 新包 `internal/gameassist`：OAuth 常量 + `ExchangeCode`（GET syzs_oauth 回调，解析 camelCase 信封 `userInfo`/`extInfo`，兼容 snake_case 与"cookie 签发"两种形态；scope 缺 `snsapi_runtime_pcsdk` 时拒绝）。
- `protocol.LoginBufferCredentials` 复用，`Provider:"gameassist"`；`FetchLoginBuffer` 走与 appstore 完全相同的 `pc_yyb_auth` 请求路径（同网关同 host 身份，生产实测可用）；**刷新与 check-login 走 syzs 专线**（v0.2.74 起，此前误用 `pc_yyb` 主刷新路由，见下）：`POST /v3/syzs_refresh_token`、`POST /v3/syzs_check_login`，`Ual-Access-Businessid: syzslogin`，请求体仍是 camelCase 的 `{"userInfo":{openId,refreshToken,accessToken,loginType}}`，应答的 `userInfo`/`extInfo` 也是 camelCase；`-109` 后同样追问 check-login。
- 扫码渠道 `product=gameassist`：`OAuthURL` 用 `wxef99873dbfab493c` 构造 qrconnect（`fast_login=1`、`self_redirect=true`、`style=black`），回调 302 由微信追加 `code/state`。
- 周边贯通：`normalizeLoginProduct`、OpenAPI product 枚举、事件日志 Channel、离线通知文案（"手游助手"）、账号 `ScanProduct`（GameAssist 账号保留真实 openId，渠道只认 `provider` 标记）、`protocol-lab` 实验台。
- quick-login（手贴 pcyyb_oauth 回跳地址）维持 appstore 专用，不放开 gameassist。

## 渠道大一统（脚本视角）

青龙脚本本就渠道无关：`YYB_SERVER` 里配 `地址@账号标识`，脚本只调 `POST /wx/code {"ref","app_id"}`，服务端按账号凭据里的 provider 自动走对应渠道链路（黑名单/熔断/限速/会话恢复/保活全部渠道无关）。v0.2.68 补齐的边缘差异：

- `/integration/accounts` 每条增加 `scan_product`，脚本可按渠道统计/分流（仅展示，选号仍按 ref）。
- `protocol.login_limited` 限流事件按账号真实渠道打标（此前硬编码 `yyb`）。
- `/wx/getuserinfo` 补齐与 `/wx/code` 一致的过期拦截；gameassist 凭据在 pc_yyb 用户信息端点被拒时降级返回本地资料（与 pcmgr 行为对齐），appstore 保持原样。
- 限流预算 `AllowLogin` 事件按渠道标记；`unsupported credential provider` 错误带出实际 provider 值。

主取码链路 `/wx/code`、`/wx/h5code`、手机号、operateWxData、cloud、encryptkey 对三渠道的行为在代码层完全一致（audit 2026-11-04）：身份取自 login buffer 字段 3，gameassist buffer 自带 `wxd44977328b36e647` 必然落入 yyb 分支，pcmgr 票据获取时有字段 3 强校验反向保护。

**凭据生命周期说明（v0.2.74 更正）**：v0.2.68 到 v0.2.73 让 gameassist 账号走 `pc_yyb` 主刷新路由，这个假设是错的。

- **生产现象（11-04，15 号，两次生命周期完全一致）**：扫码后 `expires_in=7200`；约 2 小时后保活的第一次刷新"成功"（10:34、14:26，此时旧 access token 还剩几分钟有效）；之后第一次真正需要刷新时（10:52 按需、16:24 保活）`refresh_token -109`、`check_login -109/40030 invalid refresh_token`，账号被降为仅会话。仅会话时缓存的协议会话还能继续取码，所以"能调用却被判失效"；同时凭据被整体清空，渠道标记丢失，账号在控制台和事件里变成 YYB（v0.2.74 修复）。
- **官方客户端怎么做**（本机 `GameAssist.log` / `GameAssistService.log`，令牌一律未读出）：`clogin_backend_syzs.cc` 的 `RefrashToken` 调 `/v3/syzs_refresh_token`，`FetchCheckLogin` 调 `/v3/syzs_check_login`；从 11-03 23:54 登录起每 2 小时、到期前约 4 分钟刷新一次，连续 6 次成功，access token 和 refresh token 字符串始终不变，只更新到期时间（`SetExpireTime`，新到期 = 刷新时刻 + 7200 秒）。11-04 13:36 的第 7 次刷新失败，报错格式与我们的一致（`RefreshToken catch error WXRefresh got error, code: 40030, msg invalid refresh_token`），官方客户端随即登出。那次失败发生在我们 12:29 重扫之后的第一次刷新，原因没有定论，不能据此说"同一个微信在两处登录会互相顶掉"，也不能排除。
- **探针**（假令牌，不涉及任何账号，对真实网关）：只有业务线 `syzslogin` 能到达 `/v3/syzs_refresh_token` 和 `/v3/syzs_check_login` 的真实处理逻辑（应答 `RefreshToken catch error WXRefresh got error ...`，格式与官方客户端日志一致）；`pc_yyb`、`syzs`、`pc_yyb_auth`、`syzs_pc`、`androws` 一律空体 HTTP 400；这条线上签名密钥没有被校验（错密钥与空密钥应答相同）；请求体用 snake_case 会得到 `RefreshToken empty token`，必须是 camelCase `userInfo`。
- **推断，不是实测**：`pc_yyb` 业务线刷新时用的是应用宝自己的微信应用去刷新 syzs 签发的令牌，access token 尚未过期时网关只做回显式"成功"，过期后真正去微信刷新才得到 40030。这能解释上面两次完全相同的时间线，但没有直接证据。

## 实机验收记录（2026-11-04，生产 v0.2.72）

1. **pcmgr 渠道接入成功**：生产扫码 → 授权 → confirm 首次 `20138 code=6`（loginkey 签发侧到原生网关的传播延迟，约 1 分钟后同凭据重试成功）→ 账号 id=14（`pcmgr:487970106`）→ `/wx/code` 真码 `login:ok`。v0.2.69 起确认阶段自动吸收该瞬态拒绝。
2. **gameassist state 校验实锤**：首次生产扫码授权成功后 `syzs_oauth` 返回 `-109 RC_PARAMS_INVALID`。假 code 参数矩阵探测（参数形状/UA/Referer/缺参均不改变结果）：`state=web` → `-109`，`state=49.57735723381141`（官方 `state=100*Math.random()` 形状）→ `-107 RC_NO_AUTH_ERR`（进入码校验层，与 pcyyb_oauth 假 code 行为一致）。结论：**网关要求 state 为纯数字**；v0.2.69 起扫码会话生成官方形状浮点 state 并原样回传。
3. **回调链与凭据载体实锤**（v0.2.70/71 逐跳指纹）：真实 code 的换票链为 `syzs_oauth?code=…&state=<数字>` → **302（本跳 Set-Cookie：`openid/accesstoken/refreshtoken/logintype/scope/expires_in/appid/syzs_prefix`，domain=.qq.com）** → `imgcache.qq.com/syzs/syzsweb/login/login_result.html`（静态页，读 cookie 后 postMessage 报成功）。v0.2.70 曾因 HTML 正文令 JSON 解析先报错短路、没走到 cookie 提取；v0.2.72 修正为"全部来源先试、全落空才诊断"，cookie 提取补齐 scope（容忍 %2C）与 expires_in。回调页源码已抓取存档，证实凭据载体就是 cookie。
4. **gameassist 接入成功**：v0.2.72 生产扫码 → confirm code:0 → 账号 id=15（真实 openId `oLvh-…`，provider=gameassist，expires_in=7200 经 cookie 读取）→ `/wx/code` 真码 `login:ok`。
5. **三渠道同脚本收官**：同一 `POST /wx/code {"ref","app_id"}` 调用，appstore（id=4）/ pcmgr（id=14）/ gameassist（id=15）全部返回真实 wx.login code，脚本零改动；新账号 ref 加入青龙 `YYB_SERVER` 即投产。
6. **H5 公众号路由三渠道一致性（2026-11-04 晚）**：同一 `POST /wx/h5code`（公众号 `wxabec74192b99a2d6` + 非白名单回调 `findstars.cn`）在三个渠道的会话上都打到命令 1254 并返回**完全相同的上游裁决 `business code=10003`**（回调域名与公众号配置不一致——域名校验在授权发放前，安全无副作用）。gameassist 新账号曾因出生一小时内多次登录触发上游 `-10` 保护，冷却后恢复。至此 H5 路由三渠道同链路有实证；签发真 code 需换该公众号白名单内的业务回调（等真实业务用法）。
7. **动态黑名单清理**：事故夜（v0.2.66 阈值 1 时代，00:24–00:33）误学的 56 条已清空，爱优客小程序 appid 实测恢复真码；v0.2.67 的跨账号确认机制接管后续学习。
8. **编队机制（v0.2.76）**：同一微信在多渠道的账号可绑定为一个编队（unionid 校验，`POST/GET/DELETE /api/fleet`）；`/wx/*` 调用按成员"最近最少使用"轮转分发，各成员独立会话/限速/预算/熔断，成员被上游推回（-14 类/凭据拒绝/预算尽/过期）时透明转移到下一成员，业务裁决（如 h5 的 10003）不转移。目的：并发分摊 + 渠道故障转移，脚本零改动。详见 CHANGELOG v0.2.76。
9. **`/wx/oauth` 自动换码（v0.2.75）**：场外"代理登录完整版"约定的兼容接入，见 CHANGELOG v0.2.75。

## 未验证边界（发布时必须如实告知）

**v0.2.74 追加（2026-11-04 晚，本机实验实例 + 真实扫码账号 + 真实网关）**：

- 已验证：`/v3/syzs_refresh_token`（业务线 `syzslogin`）对真实令牌连续三次成功（扫码后 1 分半、3 分半，另有一次手工请求）。应答为 `{"code":0,"msg":"RC_SUCCESS","userInfo":{"uid":"0","loginType":"WX","openId":"","accessToken":<与发出的相同>,"refreshToken":<与发出的相同>,"guid":"","imei":"","email":"","nickName":"","avatar":"","password":"","newPassword":"","expiresIn":"7200"},"extInfo":null}`：令牌不变、`openId` 为空、`expiresIn` 是字符串。解析器按此读取（并仍兼容 snake_case 与数字 `expires_in`）。刷新后用同一凭据取登录票据（`pcyyb_get_wx_login_buffer_auth`）和调 `syzs_check_login` 都返回 `code:0`。
- 未验证：令牌真正过期之后再刷新是否成功。这正是 `pc_yyb` 路由在生产上失败的那一步。实验账号 19:36:10 扫码，原始 2 小时寿命在 21:36:10 结束，最后一次刷新在 19:39:41；定时检查在 21:37 和 21:41 各取一次登录票据、再做一次过期后的真实刷新，结果见更新日志或之后的补记。
- 未验证：同一个微信同时在官方客户端和服务里登录是否互相顶掉。

以下两项依赖实机扫码，**发布时尚未验证**，静态证据只能说明"应当可行"：

1. **`syzs_oauth` 成功响应的确切形态**：信封结构已由错误路径证实，但成功体是 `userInfo{openId,accessToken,…}` 还是 cookie 签发（现两种都解析）需要一次真实扫码确认。
2. **`pcyyb_get_wx_login_buffer_auth` 是否接受 syzs 签发的 token**：同网关同 host_appid 支持该假设，但 syzs 业务若校验 token 签发域则会拒绝。若被拒，后备路径是从 GameAssistant.exe（`clogin_backend_syzs.cc`，Ual 头族实录）与 GameAssistEmulator.exe（`syzs_gen_jwt_token`/`syzs_gen_tls_sign`）提取 syzs 专属 businessid/签名，或走 `v3/pcyyb_encrypt_wx_mini_program_launch_param` 换 `ilink_auth_code` 的路线（备选镜像域 sparta）。

## 验证步骤（实机）

1. `POST /qr`（`{"product":"gameassist"}`）→ 微信扫码 → `GET /qr/{id}/poll` 至 `authorized`。
2. `POST /qr/{id}/confirm`：观察 syzs_oauth 换凭据 → `pcyyb_get_wx_login_buffer_auth` 换 login_buffer 是否成功；账号入库后 `provider` 应为 `gameassist`。
3. `POST /wx/code`（该账号 ref + 任一小程序 appid）取真实 wx.login code，确认共享 WMPF 栈按 buffer 自带的 `wxd44977328b36e647` 身份工作。
4. 失败时按上文后备路径迭代，并回填本文档"未验证边界"。

## 复核

```bash
# 静态对照（只读）：
grep -aoP "wx[0-9a-f]{16}" GameAssist.exe | sort | uniq -c
grep -ao "ThirdAppLogin" GameAssist.exe | head
# 实机验证走面板或 curl，同《验证步骤》。
```
