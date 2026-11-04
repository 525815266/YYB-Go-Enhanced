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
- `protocol.LoginBufferCredentials` 复用，`Provider:"gameassist"`；`FetchLoginBuffer`/`RefreshCredentials` 走与 appstore 完全相同的 `pc_yyb_auth` 请求路径（同网关同 host 身份），`RefreshBusinessID` 留空走默认 `pc_yyb` 主刷新路由，`-109` 后既有的 check-login 恢复逻辑自动生效。
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

**凭据生命周期说明**：gameassist 初扫凭据 `expires_at` 为官方约 30 天窗口；首次经 `pc_yyb` 主路由刷新后 `expires_in` 收敛为网关返回值（通常 7200 秒），保活节奏随之自动转为小时级——与 appstore 扫码凭据行为一致。

## 实机验收记录（2026-11-04，生产 v0.2.72）

1. **pcmgr 渠道接入成功**：生产扫码 → 授权 → confirm 首次 `20138 code=6`（loginkey 签发侧到原生网关的传播延迟，约 1 分钟后同凭据重试成功）→ 账号 id=14（`pcmgr:487970106`）→ `/wx/code` 真码 `login:ok`。v0.2.69 起确认阶段自动吸收该瞬态拒绝。
2. **gameassist state 校验实锤**：首次生产扫码授权成功后 `syzs_oauth` 返回 `-109 RC_PARAMS_INVALID`。假 code 参数矩阵探测（参数形状/UA/Referer/缺参均不改变结果）：`state=web` → `-109`，`state=49.57735723381141`（官方 `state=100*Math.random()` 形状）→ `-107 RC_NO_AUTH_ERR`（进入码校验层，与 pcyyb_oauth 假 code 行为一致）。结论：**网关要求 state 为纯数字**；v0.2.69 起扫码会话生成官方形状浮点 state 并原样回传。
3. **回调链与凭据载体实锤**（v0.2.70/71 逐跳指纹）：真实 code 的换票链为 `syzs_oauth?code=…&state=<数字>` → **302（本跳 Set-Cookie：`openid/accesstoken/refreshtoken/logintype/scope/expires_in/appid/syzs_prefix`，domain=.qq.com）** → `imgcache.qq.com/syzs/syzsweb/login/login_result.html`（静态页，读 cookie 后 postMessage 报成功）。v0.2.70 曾因 HTML 正文令 JSON 解析先报错短路、没走到 cookie 提取；v0.2.72 修正为"全部来源先试、全落空才诊断"，cookie 提取补齐 scope（容忍 %2C）与 expires_in。回调页源码已抓取存档，证实凭据载体就是 cookie。
4. **gameassist 接入成功**：v0.2.72 生产扫码 → confirm code:0 → 账号 id=15（真实 openId `oLvh-…`，provider=gameassist，expires_in=7200 经 cookie 读取）→ `/wx/code` 真码 `login:ok`。
5. **三渠道同脚本收官**：同一 `POST /wx/code {"ref","app_id"}` 调用，appstore（id=4）/ pcmgr（id=14）/ gameassist（id=15）全部返回真实 wx.login code，脚本零改动；新账号 ref 加入青龙 `YYB_SERVER` 即投产。

## 未验证边界（发布时必须如实告知）

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
