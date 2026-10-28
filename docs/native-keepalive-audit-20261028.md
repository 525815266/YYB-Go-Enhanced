# 官方应用宝登录与续期复核（2026-10-28）

用户在本机应用宝完成微信登录后，核对客户端日志、官方网页公开脚本、原生后台反汇编，并对飞牛现有异常凭据进行官方接口对照。

## 已确认的账号续期流程

| 环节 | 官方实现 | 证据 |
| --- | --- | --- |
| 定时检查 | 每 120,000 ms（2 分钟）检查一次 | `AndrowsSvr.exe` 的 QTimer 设置及 timeout 回调连接 |
| 续期时机 | 当前时间 + 240 秒达到 expireTime 时尝试刷新 | `CLoginService::OnCheckRefreshToken` 中的时间比较与分支 |
| 主登录续期 | POST `/pc_yyb/pcyyb_refresh_token`，业务 `pc_yyb` | 原生 `CLoginBackend::RefrashToken` 与官方网页 `fetchRefreshTokenApi` 相互印证 |
| 登录核验 | POST `/pc_yyb/pcyyb_check_login` | 原生 `FetchCheckLogin` 与官方网页 `featchCheckLoginApi` |
| wxapp 独立授权续期 | POST `/pc_yyb_auth/pcyyb_refresh_token_auth`，业务 `pc_yyb_auth` | 官方网页 `refreshWxTokenV3Api`，独立检查 wxappExpireTime |
| 获取协议登录票据 | POST `/pc_yyb_auth/pcyyb_get_wx_login_buffer_auth` | 官方网页 `fetchWxGameLoginBufferApi`，与项目已有接口一致 |

这不是每两分钟都刷新一次 token。客户端每两分钟检查到期时间，只有进入提前四分钟的窗口才发送续期请求。其具体发起时间会受定时器相位、进程休眠和网络影响。

两个续期接口的请求体均包含 `userInfo` 下的 `openId`、`accessToken`、`refreshToken`、`loginType`；业务标识与签名配置不同。官方公开网页请求封装对实际 JSON 请求体、毫秒时间戳、业务 accessKey、nonce 求 MD5，与项目已有签名算法一致。不能只替换 URL 而沿用另一业务的签名配置。

## 本次真实登录状态

- 23:15:40 的客户端日志记录登录、保存用户信息及成功获取用户资料。
- 本次为主登录 `WX`，scope 为 `snsapi_login,snsapi_runtime_pcsdk`，与项目当前扫码 scope 一致。
- 客户端记录中存在主登录与 `wxapp*` 两组字段，但这次 `wxapp*` 为空。字段存在不代表本次必须生成第二组授权，也不足以证明项目漏存了必需的第二套 token。
- 通过仅传递不可逆摘要的对照，确认本次客户端登录对应飞牛 YYB ID 1。新客户端 access token 与飞牛保存的 access token 不同。客户端重新登录不会自动更新另一台机器上的平台数据库。

没有导出客户端的原始凭据，没有将客户端登录自动覆盖进飞牛账户。

## 飞牛异常凭据的官方接口对照

使用飞牛 ID 1 原有凭据，按官方公开脚本的请求体和对应业务签名进行测试：

| 接口 | HTTP | 业务结果 |
| --- | --- | --- |
| `/pc_yyb/pcyyb_check_login` | 200 | `-109`，内层微信错误明确为 `40030 invalid refresh_token` |
| `/pc_yyb/pcyyb_refresh_token` | 200 | `-109 RC_PARAMS_INVALID` |

两条官方主登录路径都未恢复这份旧凭据，生产账户没有被测试覆盖。相比此前仅看到 `RC_PARAMS_INVALID`，此次增加了明确的上游 refresh token 拒绝证据；至少 ID 1 当前的失败不能单靠换 IP、修改刷新间隔或改为主登录接口解决。

这能证明当前保存的 refresh token 已被拒绝，尚不能证明它为何失效。没有证据把其他账号的异常全部归因于本次客户端登录，也没有证实“同时登录客户端必然使平台账号失效”。

## 原生定位信息

样本：应用宝 5.10.7600.6722 的 `AndrowsSvr.exe`，以下地址为 RVA：

- `0x7f3b3`：向 QTimer 设置 `0x1d4c0`（120,000 ms）。
- `0x7f3db`：timeout 回调指向 `0x868f0`。
- `0x868f0`：`CLoginService::OnCheckRefreshToken`。
- `0x869b1`：读取当前 Unix 秒数，随后加 `0xf0`（240 秒）并与到期时间比较。
- `0x71be0`：`CLoginBackend::RefrashToken`。
- `0x71fdd`：引用 `/pc_yyb/pcyyb_refresh_token` 路径。
- `0x703d0`：`CLoginBackend::FetchCheckLogin`。

ILink 运行库中另有 `AutoAuth`、`SmartHeartbeat` 等实现，客户端也包含页面存活检测和加速器心跳。此次没有抓到 ILink 长期会话完整运行轨迹，不能把这些符号直接认定为账号续期所必需的隐藏接口。

## 对项目的实际意义

1. 继续保留已验证的旧登录票据恢复修补；它与账号 refresh token 是否有效是两个问题。
2. 对 `-109` 等含糊续期失败，可以研究利用官方 check-login 增加明确诊断；若检查成功返回新的凭据，应按完整凭据更新处理，避免测试丢失轮换结果。
3. 官方主登录与 wxapp 续期应按凭据用途和真实响应选择。仅凭静态路径存在，不应一刀切切换全部账号。
4. 每两分钟检查、提前四分钟续期是该版本的客户端策略，可作为减少调用的参考；本轮未调整生产间隔，未验证长期或 30 天保活。

本轮新增的是分析与脱敏诊断资料，没有发布新版本、切换生产续期接口或重新部署服务。
