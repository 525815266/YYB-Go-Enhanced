# 文档索引

项目首页见[根 README](../README.md)，版本变化见 [CHANGELOG](../CHANGELOG.md)。下面按用途分组；文件名带日期的是调查或复核记录，写的是当时的结论和验证边界，后续版本可能已经更新。

## 部署与运维

| 文档 | 内容 |
| --- | --- |
| [Docker / Armbian 部署与排错](docker-deployment.md) | 官方镜像与固定版本、整套 `resource` 挂载覆盖镜像、青龙容器名与网络名不同、SQLite 权限、源码构建失败 |
| [配置与账号安全](configuration.md) | 配置方式与优先级、Web 用户数据库（SQLite / MySQL）、管理员与密码恢复、Cookie 与 HTTPS、令牌、设备标识、掉线通知、取码熔断、登录预算与限速、数据目录与备份 |
| [Magisk 模块](magisk.md) | Android ARM64 安装、升级、数据目录、局域网监听与 DNS（英文） |
| [系统维护](maintenance.md) | 检查更新与版本源代理、面板更新与重启、Docker 宿主机维护执行器 |
| [构建与发布](release-builds.md) | Docker 镜像、Magisk、多架构原生二进制、GitHub Actions 与发布流程 |

## 使用与集成

| 文档 | 内容 |
| --- | --- |
| [控制台与账号管理](console-features.md) | 账号工作台、扫码渠道（YYB / PCMgr / GameAssist）、多渠道账号、合并账号、接口测试、运行日志、检查更新 |
| [面板接入与排错](panel-integration.md) | 青龙、呆呆、Arcadia、同步 `YYB_SERVER`、账号运行管理、脚本拉取、跨服务器排错 |
| [协议接口说明](protocol-api.md) | `/wxapp/*` 与 `/wx/*` 接口、`wx.login` code、`operateWxData`、公众号 OAuth、能力边界与错误定位 |
| [代理与账号保活](proxy-keepalive.md) | 静态 / 动态代理、品赞、巨量、地区匹配、自动保活与失效排查 |
| [脚本管理](../scripts/README.md) | YYB 适配脚本、拉取工具、任务元数据与公共账号缓存 |

## 授权稳定性

最新渠道复核：[QQ 浏览器桥接、手游助手调度、雷电假续期与 Refresh PSK 保存（2026-11-08）](channel-login-persistence-20261108.md)。QQ 真实全流程及多渠道长期恢复尚未完成验证。

授权被上游拒绝后需要重新扫码，根因仍在排查。先读[授权稳定性：已有措施与验证边界](authorization-stability.md)，再按需看下面的详细记录。

| 文档 | 内容 |
| --- | --- |
| [授权稳定性：已有措施与验证边界](authorization-stability.md) | 防护与诊断措施一览、它们不能做什么、脚本侧建议，以及原先写在首页的按版本记录 |
| [任务高峰、重新登录与登录预算](code-pacing-20261102.md) | 对照实验与高峰回放、登录预算和取码限速的依据、`tuning.json`、验证方法 |
| [取码量对照实验](dense-batch-lab-20261103.md) | 同一账号密集取码：默认保护、去掉保护与 v0.2.35 各拿到多少 code，授权是否存活，v0.2.65 新增会话保护开关的依据 |
| [取码熔断与触发规律](code-circuit-breaker-20261101.md) | 两层本地熔断、触发规律与默认阈值 |
| [整点任务与新授权失效](hour-mark-offline-20261101.md) | 整点批量取码与新授权失效的相关性（因果尚未验证） |
| [网站应用 AppID 取码与授权失效](website-appid-getcode-20261103.md) | 对网站应用 AppID 取一次码后授权被拒的生产观察与备用号对照（一对一，机制未知），v0.2.63 默认拒绝的依据 |
| [H5 与小程序取码掉线对照](h5-vs-miniprogram-drops-20261102.md) | 非小程序 AppID 走小程序登录命令后授权被撤销、真正的 H5 命令（1254）未见撤销的备用号对照，以及飞牛脚本、生产日志和代码路径的核对（样本很小，机制未知） |
| [公众号网页授权 code 与动态 AppID 防护（2026-11-04）](h5-oauth-code.md) | /wx/h5code（1254 oauth_authorize 自动确认）的用法与失败语义；取码无 code 自学习黑名单的触发、查看与清除 |
| [固定设备标识、legacy 续期结论与掉线通知](device-identity-offline-notify-20261101.md) | v0.2.58 的改动与验证边界 |
| [AS 默认历史刷新策略](legacy-refresh-mode.md) | `legacy_auth` 与按来源刷新的差异、已知问题 |
| [授权与缓存会话分层](credential-session-layers.md) | OAuth 拒绝与协议会话分开处理 |
| [恢复取码重建顺序](code-recovery-20261031.md) | 保存票据优先的取码重建路径 |
| [原生会话恢复与续期验证](native-autoauth-recovery.md) | 独立 ILink AutoAuth 恢复 |
| [旧版与当前版的完整链路对照](old-vs-new-chain-20261101.md) | 旧版与当前版的源码对照与待完成的工作负载对照 |

## 协议与上游调查记录

新增调查：[渠道 2/5 来源、官方下载安装包、QQ 浏览器登录接口与接入边界](scan-channel-research-20261105.md)。

| 文档 | 内容 |
| --- | --- |
| [应用宝协议修复复核（2026-10-28）](protocol-audit-20261028.md) | 短链兜底更新与近期账号异常的复核 |
| [官方应用宝登录与续期复核（2026-10-28）](native-keepalive-audit-20261028.md) | 官方客户端日志、网页脚本、原生后台反汇编与官方接口对照 |
| [近期 YYB 项目对照（2026-10-29）](yyb-upstream-review-20261029.md) | 以实际提交、源码差异和反馈为依据的对照 |
| [官方 ILink 登录字段对照（2026-10-29）](native-wire-layout-20261029.md) | 官方登录字段与消息层级对照 |
| [PCMgr 调用路径复核（2026-10-29）](pcmgr-api-paths-20261029.md) | PCMgr 取码、密钥、手机号请求路径与实测范围 |
| [YYB / PCMgr：双渠道实现与协议实验（2026-10-29）](pcmgr-local-lab-20261029.md) | 双渠道实现、原 ID 切换与本地协议实验 |
| [PCMgr 官方持久状态与当前恢复差异（2026-10-30）](pcmgr-official-state-20261030.md) | 官方客户端可用与本项目延时取码失败的差异 |
| [新反馈复核与接口测试区（v0.2.50）](issue-review-20261029.md) | #105 / #112 等反馈的复核与接口测试区 |
| [2026-10-30 新反馈与候选方案复核](issue-review-20261030.md) | 官方 WMPF demo、#112 / #114–#118 的复核 |
| [#116 runtime 下载包：扫码与取码实测](runtime-transfer-review-20261030.md) | 独立 runtime 包的请求通路与实测记录 |
| [公众号自动授权：附件与镜像实测评估](oauth-a8key-review.md) | #75 相关附件与镜像的评估 |
| [早期会话与近期 H5 实现的真实对照（2026-10-31）](h5-legacy-session-compare-20261031.md) | 早期会话与近期 H5 实现的对照 |
| [未标版本号的早期 Docker：追溯与独立验证（2026-10-31）](unversioned-docker-audit-20261031.md) | 早期 Docker 的续期反馈追溯与独立验证 |

## 演示与素材

- `demo/`：在线演示页的静态源码，发布到 [GitHub Pages](https://525815266.github.io/YYB-Go-Enhanced/)，使用虚构账号和数据。
- `images/`：README 和文档使用的截图、横幅与图标素材。
