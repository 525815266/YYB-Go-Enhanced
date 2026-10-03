# YYB Go Enhanced

[![给项目点个 Star](https://img.shields.io/github/stars/525815266/YYB-Go-Enhanced?style=social&label=Star)](https://github.com/525815266/YYB-Go-Enhanced)
[![Release](https://img.shields.io/github/v/release/525815266/YYB-Go-Enhanced?display_name=tag)](https://github.com/525815266/YYB-Go-Enhanced/releases)
[![Docker](https://github.com/525815266/YYB-Go-Enhanced/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/525815266/YYB-Go-Enhanced/actions/workflows/docker-publish.yml)
[![Go Release](https://github.com/525815266/YYB-Go-Enhanced/actions/workflows/release.yml/badge.svg)](https://github.com/525815266/YYB-Go-Enhanced/actions/workflows/release.yml)

如果你喜欢这个项目，欢迎点击仓库右上角的 ⭐ **Star**，支持持续更新！

面向自托管环境的应用宝协议服务与微信账号管理平台。提供微信扫码登录、账号与 OpenID 管理、`wx.login` code 获取、凭据续期、账号独立代理，以及青龙、呆呆和 Arcadia 面板接入。

[在线演示](https://525815266.github.io/YYB-Go-Enhanced/) · [备用演示](https://raw.githack.com/525815266/YYB-Go-Enhanced/main/docs/demo/index.html) · [版本发布](https://github.com/525815266/YYB-Go-Enhanced/releases) · [更新日志](CHANGELOG.md) · [脚本目录](scripts/README.md) · [问题反馈](https://github.com/525815266/YYB-Go-Enhanced/issues)

> 在线演示使用虚构账号和数据，可体验账号切换、扫码、代理、运行日志与接口调试，不连接真实 YYB 服务，也不会保存输入。

## 界面预览

![账号工作台：搜索、状态筛选与有效期（全部为虚构演示数据）](docs/images/console-demo-1440.png)

工作台支持昵称、备注、ID、OpenID 搜索和状态筛选。v0.2.22 使用紧凑卡片网格，电脑端可在卡片内直接更新扫码、管理代理；手机以单列卡片展示，选中账号后在其下方显示快捷操作。点击“待确认”可确认账号状态，新增账号和新增链接位于顶部。账号区随数量自然展开，已失效账号停止展示有效期倒计时。页面预览使用虚构账号。

<p align="center">
  <img src="docs/images/scan-sync-mobile.png" alt="扫码成功后一键添加到青龙" width="32%">
  <img src="docs/images/account-runs-mobile.png" alt="账号运行管理" width="32%">
  <img src="docs/images/console-demo-390.png" alt="新版手机工作台（虚构数据）" width="32%">
</p>

## 核心能力

| 分类 | 能力 |
| --- | --- |
| 账号管理 | 手机扫码添加账号、重复扫码更新、账号备注、OpenID 查看、状态刷新、账号整理与安全删除 |
| 工作台 | 账号搜索、状态筛选、凭据与扫码有效期、失效状态提示，支持桌面和手机 |
| 用户系统 | 独立登录与注册、管理员与普通用户权限、用户启停、密码重置、会话管理 |
| 协议接口 | `/wx/*` 与 `/wxapp/*` 兼容接口、小程序 code、用户信息、手机号、云函数与协议调试 |
| 面板联动 | 青龙、呆呆、Arcadia OpenAPI；同步 `YYB_SERVER`；按账号创建、启停、运行任务并读取隔离日志 |
| 代理与保活 | 每账号独立直连、静态代理、动态代理 API、品赞与巨量配置；凭据按需和后台续期 |
| 脚本管理 | 统一收录审核后的 YYB 多账号脚本；支持整库同步、单脚本拉取、拉取并创建任务 |
| 部署维护 | Docker Compose、Android ARM64 Magisk、跨平台内嵌资源二进制；按运行平台提供更新入口 |

## 部署选择

| 方式 | 适用场景 | 入口 |
| --- | --- | --- |
| Docker Compose | NAS、服务器、与青龙同机或同网络运行 | [快速开始](#docker-compose-快速开始) |
| Magisk | Android ARM64 设备常驻运行，不依赖 Termux | [Magisk 文档](docs/magisk.md) |
| 原生二进制 | Linux、Windows、macOS 独立运行 | [Releases](https://github.com/525815266/YYB-Go-Enhanced/releases) |

原生程序 v0.2.25 起支持自动读取程序旁的 `.env`，也可通过 `-env-file` 指定配置；Windows 不再需要逐项设置系统环境变量。具体用法见[配置方式](docs/configuration.md#配置方式)。

## Docker Compose 快速开始

环境需要 Docker、Docker Compose v2，以及供面板互通的 `qinglong_default` 网络。

```bash
git clone https://github.com/525815266/YYB-Go-Enhanced.git
cd YYB-Go-Enhanced

docker network inspect qinglong_default >/dev/null 2>&1 || \
  docker network create qinglong_default

cp .env.example .env
docker compose up -d --build
```

打开 `http://服务器IP:8000`。未预设管理员时，第一个注册用户自动成为管理员。

默认使用 SQLite，无需额外数据库：

```dotenv
YYB_AUTH_DRIVER=sqlite
YYB_AUTH_DSN=
YYB_COOKIE_SECURE=false
```

需要复用 MySQL、预设管理员、启用 HTTPS Cookie 或配置协议令牌时，参阅 [配置与账号安全](docs/configuration.md)。

## 连接自动化面板

可以在 Web 控制台填写连接信息，也可以编辑 `.env`。青龙最小配置如下：

```dotenv
PANEL_TYPE=qinglong
QL_URL=http://qinglong:5700
QL_CLIENT_ID=你的 Client ID
QL_CLIENT_SECRET=你的 Client Secret
YYB_QINGLONG_SERVER=yyb-go:8000
YYB_QINGLONG_REPO=525815266_YYB-Go-Enhanced_main/scripts
```

连接后可在扫码完成页将账号合并到 `YYB_SERVER`，并在“账号运行管理”中为每个账号独立创建任务、设置推送、执行脚本和查看日志。

呆呆、Arcadia、跨服务器地址、任务路径和常见登录页问题参阅 [面板接入与排错](docs/panel-integration.md)。

## 脚本拉取与任务创建

先在青龙容器内安装管理工具：

```bash
curl -fsSL https://raw.githubusercontent.com/525815266/YYB-Go-Enhanced/main/tools/yyb-scriptctl.sh \
  -o /ql/data/scripts/yyb-scriptctl.sh
chmod +x /ql/data/scripts/yyb-scriptctl.sh
```

三种操作互不混淆：

```bash
# 同步整个 scripts/ 目录，不创建任务
bash /ql/data/scripts/yyb-scriptctl.sh sync

# 只拉取一个脚本，不创建任务
bash /ql/data/scripts/yyb-scriptctl.sh pull 麦富迪_code版.py

# 拉取脚本，并创建或更新对应任务
bash /ql/data/scripts/yyb-scriptctl.sh install 麦富迪_code版.py \
  --cron "13 1 * * *" --name "麦富迪"
```

脚本来源、目录覆盖、任务去重和 Issue #66 的任务名修复说明见 [脚本管理文档](scripts/README.md#青龙脚本拉取工具)。

手动上传的脚本不出现在“全部脚本”中？列表读取已配置目录内的**面板任务**，不是文件浏览器。请按[独立上传脚本接入步骤](docs/panel-integration.md#独立上传的脚本不在全部脚本里issue-73)配置目录、创建任务，再刷新列表。

## 调用协议接口

`YYB_SERVER` 每行一个账号：

```text
http://yyb-go:8000@1
http://yyb-go:8000@账号OpenID
```

获取小程序 `wx.login` code：

```bash
curl -X POST http://yyb-go:8000/wxapp/getCode \
  -H 'Content-Type: application/json' \
  -d '{"ref":"1","app_id":"wx0000000000000000"}'
```

如果配置了 `YYB_PROTOCOL_TOKEN`，请求还需携带 `Authorization: Bearer <token>`。接口清单、公众号 OAuth 边界和 `operateWxData` 参数说明见 [协议接口说明](docs/protocol-api.md)，运行中的服务也提供 OpenAPI 页面。

## 账号代理与保活

- 直连、静态 HTTP CONNECT、SOCKS5 和动态 API 均按账号独立保存。
- 动态 API 支持 `txt`、`json`、`json2` 及常见嵌套结构，并可复用命名的品赞、巨量配置。
- 短效动态代理只用于扫码和临时请求，不参与账号长期保活。
- 后台按凭据剩余时间触发刷新；微信明确拒绝 refresh token 后才标记为需要重扫。

配置格式、地区匹配、刷新窗口和安全注意事项见 [代理与账号保活](docs/proxy-keepalive.md)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [配置与账号安全](docs/configuration.md) | SQLite/MySQL、管理员、用户权限、令牌、实验性本机微信授权 |
| [面板接入与排错](docs/panel-integration.md) | 青龙、呆呆、Arcadia、跨服务器访问、运行管理与日志 |
| [协议接口说明](docs/protocol-api.md) | `/wx/*`、`/wxapp/*`、OAuth、云函数与真实 payload 边界 |
| [代理与账号保活](docs/proxy-keepalive.md) | 静态/动态代理、品赞、巨量、刷新策略与失效状态 |
| [脚本管理](scripts/README.md) | YYB 适配脚本、拉取工具、任务元数据与公共账号缓存 |
| [Magisk 模块](docs/magisk.md) | Android ARM64 安装、升级、局域网监听与 DNS |
| [系统维护](docs/maintenance.md) | Docker 在线检查、宿主机维护执行器、恢复和安全边界 |
| [构建与发布](docs/release-builds.md) | Docker、Magisk、跨平台二进制与 GitHub Actions |

## 更新

管理员可点击控制台顶栏版本号检查新版本。系统会识别当前运行环境：Windows、Linux、macOS 裸机提供匹配架构的 Release 下载，Magisk 提供模块 ZIP；Docker 仅在维护执行器已连接时提供在线更新和重启。

v0.2.24 根据 #74 的反馈，增加仅供版本检查使用的 `YYB_UPDATE_PROXY` 和可选的 `YYB_UPDATE_VERSION_URL`，并修复前端过早取消备用查询的问题。默认仍使用官方 Raw / API / Release 来源，不内置第三方镜像。遇到连接重置或 403，请按[更新网络排错](docs/maintenance.md#检查更新报连接重置或-http-403issue-74)检查容器到宿主机代理的可达性；Docker 拉取镜像使用宿主机的独立网络配置。感谢 @Xx1aoy1 提供 Armbian 实测排查过程。

完整版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 安全边界

- 默认只在可信局域网、VPN 或受控反向代理后使用，不要直接暴露协议接口和数据库。
- 公网使用时必须配置 HTTPS、`YYB_COOKIE_SECURE=true` 和随机 `YYB_PROTOCOL_TOKEN`。
- 不要提交 `.env`、`data/`、SQLite 数据库、代理密钥、OpenID 或微信登录凭据。
- `wx.login` code 是短期且一次性的；公众号 OAuth code 必须由用户在微信内完成授权后回调产生。
- 项目不会伪造微信返回值，也不会从缺失的业务参数中推导签名、加密数据或授权结果。

## 来源

项目基于 [SuperNaiBA/YYB_GO](https://github.com/SuperNaiBA/YYB_GO) 整理和增强。使用和分发时请同时遵守上游授权条件、目标平台规则及所在地法律法规。
