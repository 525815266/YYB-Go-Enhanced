# 新渠道调研收敛：QQ 浏览器 / 渠道 2（WMPF） / 渠道 5（小游戏 PC） / 雷电（2026-11-05）

承接 docs/scan-channel-research-20261105.md，本文收敛四条候选渠道的最新状态。已有三渠道（应用宝/电脑管家/手游助手）之外，每条候选给出：链路现状、缺口、下一步。

## QQ 浏览器（wx0dbc1ae97f156c38）——结论：**不能作为取码渠道**（负结果）

本机样本 22.3.7007.400（`C:\Program Files\Tencent\QQBrowser\22.3.7007.400\chrome.dll`，281MB）：

- **全量无 ilink/mmtls/WMPF 痕迹**：`ilinkapp_manualauth`/`login_buffer`/`js-login`/`mmtls`/`wmpf_host_export`/`ilink_auth_code`/`manualauth` 计数全部为 0。QQ 浏览器**没有小程序运行时**。
- 它的"微信登录"是**浏览器账号同步用的 WUP OAuth**：源码路径 `uba\business\account\uba_signin_wup_helper.cc`，路由 `/trpc.mtt.wx_auth.wx_auth/WXAuth`，请求 `WXAuthReq{code, appid}` → 响应 `WXAuthRsp{openid, refresh_token, access_token, scope, unionid, encrypted_refresh_token}`（WxAuthRspHeader.reason 为错误位）；续期走 `/trpc.mtt.qbtoken.Qbtoken/RefreshToken`（TryParseQbRefreshTokenRsp）。appid `wx0dbc1ae97f156c38` 在 ProcessSignin 上下文中出现。
- WUP 请求主机不在 chrome.dll 明文里（此前调查的 `wup.browser.qq.com` 候选未在本样本命中），token 为浏览器会话内使用的微信 OAuth 令牌。
- **残余假设（可一次请求验证）**：把 QQ 浏览器签发的 openid/access_token 喂给 `pcyyb_get_wx_login_buffer_auth`，看 yybadaccess 网关是否认非腾讯系 OAuth 端的令牌。前提是能拿到一份真实 token（本地日志为空、存储经加密，需要用户登录 QQ 浏览器微信后抓取）。
- 结论：在没有 WMPF 栈与可验证 token 之前，**不投入**。若上述假设被证伪，QQ 浏览器渠道关闭。

## 渠道 2（WMPF/UniWeChatWin，wx6b60fbda5d87d519）——链路已完整映射，**缺 session_id 创建调用**；有公开对照实现

下载来源：不需要单独下载——它就是 RadiumWMPF 运行时（上表），PC 微信装好后落在 `%APPDATA%\Tencent\WeChat\XPlugin\Plugins\RadiumWMPF\`；本机应用宝的 WmpfRuntime 5.10.7300.414 是同一套的分支。`wx6b60fbda5d87d519` 有两处独立出处：①实测内嵌于 RadiumWMPF 2.4.4.49 的 runtime/flue.dll（2.2.3.520 旧版没有——新容器身份）；②**公开对照实现** `ZHwin/yyb` / `ZHwin/yyb-py`（2026-09-14）的 `wmpf.py` 写明 `CONTAINER_APPID="wx6b60fbda5d87d519"`、`CLIENT_VERSION=1064961`、`PLATFORM="Windows-Ocean"`、transfer CmdID 4963（js-login/js-getuserwxphone）——渠道 2 的参数可直接对照该公开实现补全。

wmpf/wmpf_demo_external 仓库仍存在（MIT）但只是 Android 硬件框架 demo，与 PC 桌面 runtime 无关。

样本就在本机：`E:\Program Files\Tencent\Androws\WmpfRuntime\5.10.7300.414\`（应用宝电脑版自带，file_component.xml 标 appClientId="UniWeChatWin"）。

- **身份三元组**（flue.dll 内实证）：host_appid `wx6b60fbda5d87d519` + ilink_appid `ilinkapp_0600004fbed4ef`（注意与应用宝渠道的 `ilinkapp_060000b7b93f6c` 不同）+ 同目录 `wxd971cb7f6adaa753`（另一配置位）。
- **专用扫码端点族**（非标准 qrconnect）：
  - `https://open.weixin.qq.com/wxaruntime/Getuuid`
  - `https://open.weixin.qq.com/wxaruntime/Getqrcode`
  - `https://long.open.weixin.qq.com/wxaruntime/qrconnect`（长轮询）
  实测探测（2026-11-05）：`Getuuid` 必带 `session_id` 参数——缺参 `-3 invalid session_id`，带假值 `-4 session_id not exist`；`appid/pageid/scope/state` 均为已知参数名。
- 登录链：session_id → Getuuid → 用户扫码 → qrconnect 长轮询出 code → `WmpfSdkThirdAppLogin`（flue.dll js_api_pool，请求含 userinfo_pb/ilink_uin）→ IlinkAuthBuffer → ManualAuth → js-login。此前的"-12/no permission"实验即卡在 ThirdAppLogin 之后的权限层。
- **缺口 = session_id 的创建调用**（host 先与 wxaruntime 服务建立会话）。**2026-11-06 深挖定论**：flue.dll 里的 session_id 全是渲染会话（AskAI/资源表/web contents），不是 wxaruntime 的；RadiumWMPF 包里的 ilink2.dll 含 `ilinkapp_getloginqrcode`/`ilinkapp_checkloginqrcode`/`ilinkapp_getoauthcode` 路由——**运行时自身的微信登录走 ilink 层**（getloginqrcode 出码→checkloginqrcode 轮询→登录签发 session_id），session_id 是 ilink 登录后的产物。因此渠道 2 = **两层扫码叠加**（运行时 ilink 登录 + 每应用 wxaruntime 授权），比 gameassist 深一档；Go 实现需先复刻 ilink getloginqrcode/checkloginqrcode 两路由（同栈可复用现有 mmtls/ilink 代码），再叠 wxaruntime。下一步给 WeChatAppEx/WxgameDaemon 开 netlog（flue.dll 自带 `enable_netlog_once`）抓真实参数。
- 与雷电实验的联动：PR #144 发现官方 WMPF SDK 的 ManualAuth **显式写空 bytes 字段 6** 而 Go 省略——同一 WMPF 家族，字段 6 对照实验可直接复用到本渠道。

## 2026-11-07 实测：sdk/qrconnect 的 scope 门禁已收紧

用 ZHwin/yyb（2026-09-14）的流程实测：getqrcodesignature 正常出签名（ErrCode=0），但  对**所有 scope 变体**（snsapi_userinfo,snsapi_runtime_apk 原样/URL 编码、单独 runtime_apk、snsapi_base、snsapi_login、snsapi_runtime_pcsdk、空 scope）一律返回 `-2 invalid scope or bad scope`；把 scope 带进 getqrcodesignature 再签名同样被拒。结论：**腾讯服务端收紧了 sdk/qrconnect 的 scope 白名单**（与 #116 "tx 打击" 时间线吻合），2026-09 的公开流程已失效。渠道 2 的 QR 入口需要新一轮真实客户端抓包（看现行客户端改用什么 scope/端点），或走 ilink 层 getloginqrcode 路线。此前映射的 transfer/login 端点族（wxaruntime/transfer、wxaruntime/login、CmdID 4963 信封）不受此影响——一旦拿到 session_id 依然可用。

## 渠道 5（微信小游戏 PC 客户端 1.0.0.44，wx7c011f53c0737655）——同栈，最有希望，官方接口已拿到

scan-channel-research-20261105.md 已确认其 EXE 含 ilink2.dll（`ilinkapp_manualauth`/`ilinkapp_autoauth`/`ilinkapp_getoauthcode`/`ilinkapp_getloginqrcode` 等 7 条路由）——与应用宝/手游助手**同一 ilink 栈**。

**下载来源（调研 agent 逐一 HEAD 验证）**：不存在独立的"1.0.0.44"官方安装页（第三方 gameyj.com 页面为低质引流页不可信）；微信 PC 版 4.x 的"游戏中心"侧边栏就是宿主（运行时进程 WxgameDaemon.exe，文件描述即"微信游戏中心"）。官方公开分发渠道是微信开放平台《PC 小程序插件接入指南（Beta）》，4 个 dldir1.qq.com 直链全部 200 OK 且 MD5 与文档一致：

| 版本 | 直链 | 大小 |
|---|---|---|
| RadiumWMPF 2.4.4.49 universal（最新，**含微信游戏中心**） | `https://dldir1.qq.com/WechatWebDev/wmpf/RadiumWMPF_2.4.4.49_x64_universal_V20127.zip` | 184,187,939 B，md5 76e64da57c51794f7bedc532c62573cc |
| RadiumWMPF 2.2.3.520 | `https://dldir1.qq.com/WechatWebDev/WechatWebDev/wmpf/RadiumWMPF_2.2.3.520_x64_V14673.zip` | 151,852,808 B |
| RadiumWMPF 2.2.3.465 | `https://dldir1.qq.com/WechatWebDev/WechatWebDev/wmpf/RadiumWMPF_2.2.3.465_x64_V14299.zip` | 170,902,933 B |
| RadiumWMPF 2.2.3.382 | `https://dldir1.qq.com/WechatWebDev/WechatWebDev/wmpf/RadiumWMPF_2.2.3.382_x64_V14087.zip` | 418,679,966 B |

实测解包 2.4.4.49（MD5 通过）：runtime/WeChatAppEx.exe + **WxgameDaemon.exe（微信游戏中心）** + wxgame_client.dll + **ilink2.dll/ilink_wrapper.dll（7 条路由与渠道 5 EXE 完全一致）** + **pcyyb_sdk_dll.dll 2.1000.150.0（"腾讯应用宝移动应用引擎"，Product "Androws"）**——官方 WMPF 包直接内置应用宝引擎 SDK。注意 2.2.3.520 旧分支没有这些组件，动态验证用 2.4.4.49。

**ThirdAppLogin 不必再从 EXE 盲猜——官方头文件+proto 随包提供**（`headers/include/wmpf_host_interface.h`、`headers/proto/wmpf_host.proto`，已留档 `%LOCALAPPDATA%\Temp
adiumwmpf\`）：
- `ThirdAppLogin(const char* ilink_buffer, ...)`:注释明文"传入拿到的 base64 即可"
- `AutoLogin(business_req_buffer, ...)`:注释"腾讯会议预留的额外票据，**留空即可**"——**这直接否定雷电 -13 的一个变量**：AutoLogin 的 business buffer 本来就该为空
- 初始化：`LoadLibrary("wmpf_host_export_x64.dll")` → `GetBrowsingService` → `InitilizeBrowsingCore`，JSON config 必须 `product-id=3000`、`enable-applet-v3=true`（另有 `ilink-debug-net` 走 995 测试环境）
- **登录 buffer 的官方来源（文档明文）**：网站应用 OpenSDK 申请 scope `snsapi_runtime_pcsdk` → `POST https://api.weixin.qq.com/sns/pcsdk/login?access_token=...` 取 login_buffer → base64 传 ThirdAppLogin

wx7c011f53c0737655 本身查无公开出处（GitHub 仅命中本项目，两个官方 WMPF 包的二进制里也没有）——它是该容器运行时的私有 host 身份。

### 2026-11-07 补充：游戏中心取码链路定论（pcyyb_sdk_dll 全量分析）

pcyyb_sdk_dll.dll（Androws 2.1000.150.0，游戏中心内置的应用宝引擎）的 yybadaccess 接口**全是商店运营类**（app detail/client update/device check/report/cookiemapping/伪协议 token，businessid 候选 pc_yyb/pc_yyb_client/pc_yyb_partner，渠道值 pc_wechat_game_center）——**没有 login_buffer、没有 launch_param 加密接口**；6 个二进制里 wx7c011f53c0737655 与 wxd44977328b36e647 均 0 命中（host appid 由调用方传入）。

真实取码链路（官方头文件 + ilink2.dll 实证）：宿主注入 `ilink_buffer_base64`（flue.dll JSAPI `WmpfSdkThirdAppLogin`，GlobalStorage 无保存时提示 require manual login）→ ilink2.dll `TdiThirdAppLoginAsync`（protobuf `ilink.pb_export.IlinkLoginRequest`，凭证校验类 `ThirdAppAuthCodeVerifyBuffer`）→ **mars 长连接 + MMTLS 至 sgilinklong.wechat.com / sgilinkshort.wechat.com**（微信自有域名族，非 longcloud.weixin.com）→ `IlinkLoginResponse`（ilink_uin/userinfo_pb）→ `SendWxaTransferRequest` 出码。AutoLogin 的 business buffer 官方注释留空即可。

**Go 实现评估**：可复用现有 mmtls/ilink 代码（同一协议族），但端点族、登录命令与 buffer 签发方都不同——buffer 的原始签发方（微信主客户端哪个 CGI）不在游戏中心包内，需对微信主客户端取证。工作量等同重做一个渠道级协议栈，列为独立大项。

## 雷电模拟器（wx56135c126042c9e7）——PR #144，首次登录已通，保活差三步对照

状态见 PR #144（保持草稿，按作者计划继续实验）：雷电 14 → 游戏中心 → 微信小游戏换票（`game-service.ldmnq.com/mini-game/wechat/login` 返回 base64 login_buffer）→ Go ManualAuth + wx.login 已出真码；AutoAuth 被拒（-13）。补完方向（优先级序）：①Refresh PSK 恢复（传输层，目前最明确线索）②ManualAuth 空 bytes 字段 6 对照 ③官方正常关闭对照 ④持续 runtime 生命周期。渠道化前提：换票接口无 refresh/无 unionid，身份绑定需要设计决策。

## 更新记录

- 2026-11-05：初版收敛（QQ 浏览器负结果、渠道 2 端点族与三元组、session_id 缺口）。
- 2026-11-05（二）：合并下载源调研——RadiumWMPF 2.4.4.49 官方直链（MD5 已验）即渠道 5 样本；ThirdAppLogin/AutoLogin 参数官方头文件已给出；渠道 2 有公开对照实现 ZHwin/yyb。扫码页/链接页多渠道二维码尺寸统一为 280px。
