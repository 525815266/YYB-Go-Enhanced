# QQ 浏览器 Token Bridge 扩展

把 QQ 浏览器的微信登录态（外层 access_token）推送给 YYB Go 服务，换取 ilink 登录票据——构成 QQ 浏览器取码渠道（host appid `wx0dbc1ae97f156c38`，buffer 由 `kde.qq.com/trpc.ad.wx_oauth_svr.WxOauthHttpSvr/GetWxLoginBuffer` 签发）。

## 安装（QQ 浏览器）
1. 打开扩展管理页（地址栏输入 `qqbrowser://extensions/`），开启"开发者模式"
2. "加载已解压的扩展"，选择本目录（`extensions/qbbrowser-token`）
3. 扩展卡片 → "选项"：填 YYB 服务地址（如 `http://192.168.9.83:8000`）与服务端 `YYB_PROTOCOL_TOKEN`（未设置可留空）
4. 点"立即推送"——服务端校验 token 并自动建号（账号 OpenID 为 `qbbrowser:<uid>`，渠道显示"QQ浏览器"），之后与其它渠道一样可进编队、走 `/wx/code` 取码

## 行为
- 安装时、浏览器启动时、每 30 分钟自动推送一次
- token 由 QQ 浏览器自身维护（微信登录态），扩展只搬运；推送被拒（如浏览器里重新登录后 token 变化）会在下个周期自愈
- 桥候选自动尝试：`browser.login.getAccountInfo` → `chrome.account.getLoginInfo`；失败原因记录在选项页
