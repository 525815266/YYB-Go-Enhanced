# 扫码渠道 2、5 与 QQ 浏览器调查

日期：2026-11-05。基线为主分支 `151bf1e`（v0.2.79 合并后的代码）。本次只静态检查公开安装包/已安装程序和未登录的公开页面，没有读取浏览器用户资料、导出已登录凭据、替换现有微信登录或发起业务取码。

## 找到的渠道来源

| 编号 | AppID | 来源与证据 | 获取入口 | 接入状态 |
| --- | --- | --- | --- | --- |
| 2 | `wx6b60fbda5d87d519` | 本机桌面微信 RadiumWMPF 26042 的 `runtime/flue.dll` 命中；小游戏 SDK 25263 的共享 `flue.dll` 也带此默认值及 `ilinkapp_0600004fbed4ef` | [微信 Windows 官网](https://pc.weixin.qq.com/)，小程序运行时随微信组件提供 | 已有 runtime 独立实验，取目标小程序 code 曾返回 `-12 / no permission`；未作为可用生产渠道 |
| 5 | `wx7c011f53c0737655` | 腾讯官方微信小游戏客户端 **1.0.0.44** 的 `WechatGameCenter.exe` 精确命中，邻接 `host_appid`、`base_request` | [微信小游戏官网](https://xiaoyouxi.qq.com/)，点击“下载客户端” | 已确定对应产品，完成静态登录接口定位；未完成真实扫码、取票据、取码及续期验证 |
| 6 | `wx0dbc1ae97f156c38` | 本机 QQ 浏览器 **22.3.7007.400** 的 `chrome.dll` 精确命中，官方 `wx-login.html` 使用相同值 | [QQ 浏览器官网](https://browser.qq.com/) | 已定位 OAuth、原生 code 交换与刷新接口，尚缺 WMPF/ILink 票据链路，未加入可用渠道 |

渠道 2 是桌面微信运行时的宿主身份，不能以 appid 搜不到独立安装包就认定不存在，也不能将任意 WMPF demo 当成同一实现。渠道 5 已有 EXE 精确证据，无需再凭产品名称猜测。

## 渠道 2：桌面微信小程序运行时

既有调查见 [runtime-transfer-review-20261030.md](runtime-transfer-review-20261030.md)。已定位的独立协议入口为：

1. `https://open.weixin.qq.com/wxaruntime/getqrcodesignature` 获取扫码签名。
2. `/connect/sdk/qrconnect` 生成二维码，实验使用 `snsapi_userinfo,snsapi_runtime_apk`。
3. `/wxaruntime/login` 建立宿主会话，返回 `uin`、`session_id`。
4. `/wxaruntime/transfer` 携带 protobuf `ReqData` / `Ctx` 访问目标小程序。

历史实验是宿主登录成功、目标 getCode 被 `-12` 拒绝，不是可用取码渠道。宿主 AppID、设备上下文、scope、目标小程序授权权限和 transport 都需匹配。没有重放旧失败会话。

## 渠道 5：微信小游戏 PC 客户端

从官网按钮下载的在线安装器 `WechatGameCenter_Setup.exe` 为 2,927,176 字节，腾讯签名。仅离线解压 NSIS 头与资源，未运行安装器。头中给出两个腾讯 CDN 地址：

- [官方离线安装包 1.0.0.44](https://mmgameres-40068.sh.gfp.tencent-cloud.com/wechatgame/minigamepc/OfflineInstaller/GameCenter/x64/1.0.0.44/WechatGameCenter_Setup.exe)，14,266,032 字节。
- [该安装器对应的 WMPF SDK 25263](https://mmgameres-40068.sh.gfp.tencent-cloud.com/wechatgame/minigamepc/WmpfSDK/GameCenter/x86_64-pc-windows-llvm/zstd/wmpfsdk-25263.tar.zst)，150,348,347 字节。

固定版本 CDN 地址用于复现；后续版本优先从官网重新获取。离线包内 `1.0.0.44/WechatGameCenter.exe` 为 12,837,416 字节，精确包含 AppID。SDK 的 `file_component.xml`、`host/wmpf_host_export_x64.dll`、`runtime/flue.dll`、`ilink2.dll` 和 `ilink_wrapper.dll` 可独立检查，不必安装或登录客户端。

客户端的 `GchWmpfManager` 动态加载 `wmpf_host_export_x64.dll` 并取得 `IILinkAuthManager2`，有 `AutoLogin`、`ThirdAppLogin`、`CheckLoginState` 等调用点；前端入口是 `game.weixin.qq.com/cgi-bin/h5/static/main-window/`。`flue.dll` 另有 `WmpfSdkThirdAppLogin` / `ilink_buffer_base64` / `userinfo_pb` / `ilink_uin` 标记。`ilink_wrapper.dll` 导出 `AuthManagerNoPB::GetLoginQrCodeAsync`、`CheckLoginQrCodeAsync`、`GetAuthRawData`。

`ilink2.dll` 中定位到以下**原生 ILink 路由**：

```text
/ilink/ilinkapp/sys/ilinkapp_getloginqrcode
/ilink/ilinkapp/sys/ilinkapp_checkloginqrcode
/ilink/ilinkapp/sys/ilinkapp_manualauth
/ilink/ilinkapp/sys/ilinkapp_autoauth
/ilink/ilinkapp/sys/ilinkapp_getoauthcode
```

这些是 native transport 内部路由，不能据此拼成公网 HTTP GET 接口。共享 `flue.dll` 内还出现 `https://open.weixin.qq.com/wxaruntime/Getuuid`、`Getqrcode` 和 `https://long.open.weixin.qq.com/wxaruntime/qrconnect`，但不能仅凭同一个 DLL 内的字符串断言小游戏宿主实际使用了这套分支。下一步应将 `IILinkAuthManager2` 的具体实现与 `ThirdAppLogin` 参数连接起来，核实客户端实际选择的 ILink AppID、二维码响应、验证票据及登录 buffer 格式。

## QQ 浏览器：OAuth 不等于小程序运行时授权

公开页面：

- `https://login.browser.qq.com/login`
- `https://go.browser.qq.com/wx-login.html`
- `https://res.browser.qq.com/login/js/index.2ca628a8.js`

原生登录页通过 `WxLogin` 生成 `https://open.weixin.qq.com/connect/qrconnect`，`appid=wx0dbc1ae97f156c38`、**scope 仅为 `snsapi_login`**、回调 `https://go.browser.qq.com/wx-login.html`、state 取自本次请求。新网页登录页使用 `https://go.browser.qq.com/wxlogin?url=...` 包装 `https://login.browser.qq.com/callback/web-login?session=<state>`。

收到 code/state 后，原生页面调用 `chrome.qbFrameCommand.invoke('login', JSON.stringify(args), ...)`，备用路径是扩展 `ajlpghgkcddknknmgmahohkkdgakaicl` 的 `login` 消息。解出的 Chromium PAK 资源也确认 `qbFrameCommand` 仅对白名单域名开放；本项目 localhost 页面不能直接调用这个 native bridge。

`chrome.dll` 中的 `uba_signin_wup_helper.cc` 日志和 protobuf 类型定位到：

```text
DoWxAuth
/trpc.mtt.wx_auth.wx_auth/WXAuth
trpc.mtt.wx_auth.WXAuthReq: code, appid
trpc.mtt.wx_auth.WXAuthRsp:
  openid, refresh_token, access_token, scope, unionid, encrypted_refresh_token

WxRefreshToken
/trpc.mtt.wx_auth.wx_auth/RefreshToken
```

原生 WUP 层另包含 `Content-Encoding`、`Common-Header`、`PB`、`CipherWupHandler`、`Q-UA2`、`encrypt/tk/iv` 参数，以及 `wup.browser.qq.com:443`、`qbwup.imtt.qq.com`、`qbwupacc.imtt.qq.com:8080`、`pbprxpc.qq.com:443` 候选主机。字符串只能确认协议组件存在，尚未确定 WXAuth 实际选用的封装和主机，不应把相对路由直接作为普通 JSON API 调用。protobuf 字段编号也未在此报告中猜测。

目前没有确认 `snsapi_runtime_pcsdk` 授权，也没有确认 QQ 浏览器 access/refresh token 可获取本项目需要的 login_buffer。不能把 QQBrowser code 发到 YYB / PCMgr / GameAssist 的回调兑换，不能给旧凭据换个 provider 就保存，更不能将“二维码能生成”写成“渠道已接通”。

### 电脑微信快速授权

项目现有快捷授权使用本地微信 `/api/authorize` 的 `qrconnectfastauthorize`，appid、scope、redirect_uri、state 及后续 code 交换都绑定 YYB。QQ 浏览器需要独立会话、官方回调/state 校验、WXAuth 交换及可用运行时票据链路，缺一不可。静态页面的普通 `snsapi_login` 支持不构成快捷授权成功证据。

新增 [channel_oauth_probe.py](../scripts/channel_oauth_probe.py) 可只读检查公开 OAuth 页。默认只生成官方参数 URL；`--scope runtime-test` 和 `--fast-login-test` 是明确标记的探索参数，不默认启用，也不代表已通过认证。`--fetch` 只输出页面结构，不导出 cookie、二维码 UUID、code 或凭据，不轮询、不访问业务回调。真实桌面微信授权需用户亲自确认，目前没有执行。

本次公开页面探测：官方 `snsapi_login` 和探索组合 `snsapi_login,snsapi_runtime_pcsdk` 加 `fast_login=1` 均返回 HTTP 200、包含二维码元素，未显示 scope 错误。这只能排除“页面立即拒绝参数”，不能证明 scope 最终授予、桌面微信支持该 appid 快速授权或能够生成运行时票据。

## 本次可用功能与边界

新增链接创建时的 `allow_multi_channel` 权限和公共授权页的多渠道二维码，使用当前已接通的 YYB / PCMgr / GameAssist。详见[控制台文档](console-features.md#授权链接的多渠道扫码)。该功能不依赖本机安装上述客户端，授权结果按原有 provider 分别存储。

渠道 2、5 和 QQBrowser 的正式渠道注册暂未开放。后续接入验收至少包含：官方扫码及状态回调、同渠道 code 交换、login_buffer/原生登录上下文、同微信身份核验、目标小程序业务取码、对应刷新或恢复路径、隔离测试及失败不污染其他 provider。当前没有这几条新渠道的真实业务成功或长期续期结论。

## 复现与验证

静态检查工具 [inspect_channel_artifacts.py](../scripts/inspect_channel_artifacts.py) 读取指定程序文件的 UTF-8 / UTF-16LE 字符串，可解 NSIS Deflate 安装头和 Chromium v4/v5 PAK 的匹配资源。只检查公开程序文件，不选择用户资料目录；Brotli 压缩的 PAK 资源需要 Python `brotli` 包。示例：

```sh
python scripts/inspect_channel_artifacts.py WechatGameCenter_Setup.exe --nsis-header --pattern 'https?://[^\s\x00"<>]+'
python scripts/inspect_channel_artifacts.py WechatGameCenter.exe --pattern 'wx7c011f53c0737655' --context 60
python scripts/inspect_channel_artifacts.py chrome.dll --pattern 'WXAuth|WxRefreshToken|wx0dbc1ae97f156c38' --context 70
python scripts/channel_oauth_probe.py --fetch
python scripts/channel_oauth_probe.py --scope runtime-test --fast-login-test --fetch
```

本次全量 `go test ./...`、`go vet ./...` 和 Windows 编译通过。多渠道授权新增存储/HTTP 测试覆盖权限持久化、同微信验证、重复确认、归属继承、拒绝接管独立账号、原 ID 保留、会话清理、代理继承、失败回滚、过期/作废及授权快照变化。浏览器隔离测试 [account-link-multi-ui.cjs](../tests/account-link-multi-ui.cjs) 覆盖创建开关、三个独立二维码、身份错误、确认响应丢失后的重试、刷新恢复、单/多二维码切换、完成授权，以及 320/390/768/1280px 的布局；原有 `scan-channels-ui.cjs` 回归也通过。Python 脚本编译、JavaScript 语法和 `git diff --check` 均通过。浏览器测试使用虚构账号和二维码，未验证新渠道的真实授权。
