# YYB Go Enhanced

主要功能变化请查看 [更新日志](CHANGELOG.md)。

应用宝协议服务增强版，提供微信扫码登录、账号与 OpenID 管理、`wx.login` code 获取、凭据按需续期、带用户权限的 Web 控制台，以及 Docker 和面板接入。

## 功能

- 默认使用手机扫码添加账号，授权成功后显示账号 ID、OpenID 和存活状态；实验性本机微信快速授权默认关闭
- 扫码成功后可填写账号备注，并一键合并到面板 `YYB_SERVER`，重复操作不会产生重复账号
- Web 控制台支持配置 **青龙面板** 与 **呆呆面板 (daidai-panel)** OpenAPI，支持自动识别与测试连接，且不会回传 Secret 明文
- Web 控制台管理账号并复制 OpenID
- 每个微信账号可独立选择直连、静态代理、动态代理 API，或命名的品赞/巨量供应商配置；支持按账号选择省市、HTTP CONNECT、SOCKS5、代理认证以及 `txt`、`json`、`json2` 响应
- 扫码时可根据手机定位或公网出口推荐代理省市；推荐不会自动覆盖配置，代理测试会显示实际出口地区并提示目标地区不一致
- 提供 `/wx/*` 和 `/wxapp/*` 两套兼容接口：小程序 code、用户信息、手机号、加密 Key、云函数、二维码授权、文章会话/扩展数据/点赞
- 应用宝短期凭据接近失效时由后台任务主动续期，业务调用失败时也会按需续期
- 重复扫码更新原账号，不再消耗新编号；授权后保存失败会清理本次新建的占位记录
- SQLite 持久化账号与协议会话
- 独立登录与注册页面，支持管理员、普通用户、用户启停、密码重置和会话管理
- 统一管理平台外壳：固定侧栏、页面顶栏、用户身份区和移动端抽屉导航，账号、运行、用户与设置不再是相互独立的页面
- 用户、角色和网页登录会话默认存储在 SQLite，也可切换到 MySQL；微信协议数据继续使用独立 SQLite
- 支持与青龙容器共享 Docker 网络
- 账号运行管理：每个微信账号独立创建、启停和运行青龙脚本，并查看日志
- 运行日志使用独立抽屉连续刷新，保持阅读位置；支持超过 2 MB 的青龙日志索引响应
- 账号独立推送：支持 Server酱、PushPlus 和企业微信机器人，密钥只保存在青龙环境变量

账号队列中的“一键整理”仅供管理员使用。系统先列出候选记录，点击确认后才会清理明确没有 `login_buffer`、凭据且未绑定用户的扫码残留；该操作不会删除正常账号或已过期账号，也不会把已有账号的 ID 整体前移。释放的 ID 会在下一次成功扫码时自动复用。扫码页刷新二维码和返回时会取消旧会话，避免旧二维码的延迟请求再次写入账号。

## 界面

![账号控制台与青龙连接设置](docs/images/account-console.png)

<p align="center">
  <img src="docs/images/scan-sync-mobile.png" alt="扫码成功后一键添加到青龙" width="360">
  <img src="docs/images/account-runs-mobile.png" alt="带账号备注的运行日志" width="360">
</p>

## Docker Compose 部署

运行环境需要 Docker、Docker Compose，以及名为 `qinglong_default` 的 Docker 网络。如果没有青龙，也可以先创建同名网络：

```bash
docker network create qinglong_default
```

创建本地配置：

```bash
cp .env.example .env
```

公开版本默认使用 SQLite，无需单独部署数据库。编辑 `.env` 时保留：

```dotenv
YYB_AUTH_DRIVER=sqlite
YYB_AUTH_DSN=
YYB_COOKIE_SECURE=false
```

认证数据库默认保存为持久化卷中的 `resource/db/auth.db`。首次打开控制台注册的第一个账号会自动成为管理员，后续注册账号为普通用户。也可以在首次启动前设置 `YYB_ADMIN_USER` 和 `YYB_ADMIN_PASSWORD` 来预先创建管理员。

构建并启动：

```bash
docker compose up -d --build
```

默认 Compose 只启动一个 `yyb-go` 容器，访问地址为 `http://服务器IP:8000`。Nginx Basic Auth 已由应用内登录页面替代；需要 HTTPS 或公网入口时，请在项目外使用已有反向代理并限制协议接口访问。

扫码页的“推荐代理地区”只在用户点击后运行。HTTPS 页面可在浏览器授权后将坐标发送到 OpenStreetMap Nominatim 做反向地理解析；局域网 HTTP 页面会使用公网 IP 地理服务，无法读取手机 GPS。推荐结果只预填省市，用户仍需确认并应用。

需要复用 MySQL 时，设置以下变量并确保 `yyb-go` 与 MySQL 容器位于同一 Docker 网络：

```dotenv
YYB_AUTH_DRIVER=mysql
YYB_AUTH_DSN=yyb_go:数据库密码@tcp(mysql:3306)/yyb_go?charset=utf8mb4&parseTime=true&loc=UTC
```

旧变量 `YYB_AUTH_MYSQL_DSN` 继续兼容：只设置该变量时会自动选择 MySQL，不会迁移或覆盖已有用户。`YYB_AUTH_DRIVER=none` 可关闭网页登录认证，仅建议用于受保护的本机调试。`YYB_COOKIE_SECURE` 仅在 HTTPS 反代下设为 `true`。

## Magisk 模块

Android ARM64 设备可从 [Releases](https://github.com/525815266/YYB-Go-Enhanced/releases) 安装最新版 Magisk 模块。模块由 `late_start service` 开机常驻运行，不依赖 Termux；默认控制台为 `http://127.0.0.1:8000`，账号和配置持久化在 `/data/adb/yyb-go`。v0.1.4 已通过官方 Magisk 真机安装、进入控制台和扫码验证。

模块目前只提供 ARM64 构建，不支持 32 位 Android。需要让青龙或呆呆面板访问手机服务时，必须在 `/data/adb/yyb-go/config.conf` 中配置局域网监听和面板地址；不要把端口暴露到公网。完整安装、升级、DNS 和局域网配置见 [Magisk 模块文档](docs/magisk.md)。

### 用户与权限

- 第一个管理员可由 `YYB_ADMIN_USER`、`YYB_ADMIN_PASSWORD` 初始化；未设置时首个注册账号成为管理员。
- 后续注册账号默认为普通用户，可使用工作台、扫码添加和管理本人微信账号、本人代理与账号级脚本任务，并可修改个人资料、密码和管理自己的会话。
- 管理员可查看和管理全部微信账号、扫码、协议调试、面板连接与全量同步、运行管理和用户管理，并可关闭公开注册；管理员查看用户账号为只读，不会切换用户会话。
- `/wx/*`、`/wxapp/*` 保持给青龙脚本调用，不要求浏览器 Cookie；不要直接将这些协议接口暴露到公网。
- 修改密码会注销该用户的其他会话；管理员重置密码或停用用户会注销该用户全部会话。

### GitHub Actions 自动与手动构建镜像

项目已添加自动与手动构建 Docker 镜像的 Workflow (`.github/workflows/docker-publish.yml`)，支持打包发布到 **GitHub Container Registry (GHCR)**：

- **手动触发构建**：在 GitHub 仓库页面进入 **Actions** -> 选择 **Build and Publish Docker Image** Workflow -> 点击 **Run workflow**，可自定义填入镜像 Tag（默认 `latest`）并一键构建发布。
- **自动触发构建**：当推送分支到 `main` 或推送版本 Tag (如 `v1.0.0`) 时自动触发镜像构建。
- **支持架构**：多架构支持 (`linux/amd64`, `linux/arm64`)。
- **失败恢复**：代码拉取、QEMU 初始化、Buildx 启动、GHCR 登录及镜像构建推送均最多重试 3 次；镜像标签由工作流直接生成，不依赖需要在 Set up job 下载的第三方 Action。
- **发布校验**：Docker 构建会先执行 `go test ./...`，测试失败时不会生成或推送镜像。

Magisk 模块使用独立的 `Build Magisk Module` Workflow：主分支每次提交都会编译并校验 ARM64 安装包；推送 `magisk-v0.1.5` 这类标签时，会自动创建对应 Release 并上传 ZIP。也可在 Actions 页面手动填写版本并选择是否发布 Release。Docker 与 Magisk 为两条独立任务，一方失败不会阻塞另一方。

## 本机微信快速授权（实验性）

该能力默认关闭，因为桌面微信版本、浏览器本地网络权限和 OAuth Cookie 会话都会影响稳定性。已验证本机环境后可显式开启：

```dotenv
YYB_ENABLE_PC_LOGIN=true
```

开启后，在 Windows 电脑上打开 YYB Go 的“添加微信账号”页面，页面会尝试连接当前电脑已登录的微信客户端。检测成功后，点击“使用本机微信授权”，在电脑微信中确认即可。

该能力复用了微信开放平台网页授权的 `fast_login` 流程。浏览器只与本机微信通信，并把微信返回的一次性回调地址交给 YYB Go；账号凭据仍由 YYB Go 服务端换取和保存。服务端会校验回调协议、域名、路径和 `state`，且快速授权会话只能使用一次。

使用条件与限制：

- 仅桌面微信客户端支持，本机微信需保持登录且未锁定。
- 浏览器必须允许访问 `https://localhost.weixin.qq.com`。企业安全策略、浏览器本地网络访问限制或微信版本不支持时，检测会失败。
- 检测失败会自动切换到原有扫码授权，不影响手机扫码登录。
- 快速授权最终仍需要用户在微信中确认，不能在无交互的情况下静默登录。

如果只允许某个局域网地址监听，可设置：

```dotenv
YYB_BIND_ADDRESS=192.168.1.10
```

## 账号独立代理

`txt` / `json` / `json2` 动态 API 代理通常是短效 IP，只适合扫码或临时业务请求，不能作为账号长期保活手段。YYB Go 会在界面上标记“短效代理，不可用于保活”，并跳过这类账号的后台自动保活，避免反复提取 IP 和无效刷新。静态、长效且地区稳定的代理才适合参与保活。

左侧“代理设置”集中管理每个微信账号的独立出口，工作台会显示当前账号的代理摘要；添加账号页面也可先设置登录代理，再生成二维码或发起本机微信授权。登录二维码、OAuth 回调、凭据刷新、用户资料、`wx.login`、LongLink 和 ShortLink 会使用该账号的同一代理策略。

支持以下配置：

- **直连**：该账号不使用代理；即使启动参数设置了全局 `-tcp-proxy`，账号显式选择直连后也会覆盖全局值。
- **静态代理**：填写 `host:port`、`user:pass@host:port`、`host:port:user:pass` 或 `host:port|user|pass`。
- **动态代理 API**：填写完整的 HTTP/HTTPS 提取地址，省、市和运营商参数直接保留在 URL 查询参数中。响应可为纯文本，或 `json` / `json2`；解析器支持常见的 `data`、`result`、`list`、`proxy_list`、`rows`、`items` 等嵌套结构，以及独立的 IP、端口、用户名和密码字段。
- **品赞配置库**：可添加任意数量的命名配置，例如“品赞代理 1”“山东套餐”。配置库集中保存 `core-extract` 链接，每个账号只关联配置和省市；修改配置链接后，所有关联账号自动生效。授权方式可直接选择“账号密码”或“出口 IP 白名单”，账号密码模式会强制使用 JSON 并读取品赞返回的临时 `account/password`，不依赖服务器公网 IP。
- **巨量配置库**：填写企业动态代理的业务编号和 API Key。YYB Go 会按账号保存的省市实时生成签名，并使用 `json2` 返回的临时 `http_user/http_pass`；也可切换为自动白名单模式。巨量当前提取接口仅能通过 HTTP 访问，是否启用请结合自己的网络环境判断。
- **代理协议**：HTTP 代理使用 CONNECT 隧道，也可选择 SOCKS5；两者均支持用户名密码认证。

动态 API 会在短时间内复用同一代理，避免一段脚本流程反复提取和跳 IP；新代理会先尝试使用现有 `login_buffer` 建立协议会话，只有腾讯明确拒绝时才刷新 token。账号显式配置的代理若不可用，请求会直接失败，不会静默切换为直连。代理测试同时显示供应商入口节点和实际出口地区，入口机房所在地不等于最终出口所在地。原有 Magisk `TCP_PROXY` / 服务端 `-tcp-proxy` 仅作为没有账号配置时的兼容默认值。

同一微信账号重新扫码时只更新凭据并恢复账号状态，已有代理配置、地区和提前刷新时间不会被扫码页的默认直连覆盖。已知失效账号继续显示在工作台，协议调用会在提取动态代理前返回需重扫；重新扫码后自动恢复调用。

代理 API URL 和静态代理认证信息保存在本机协议 SQLite 数据库中。不要把数据库、管理接口或包含密钥的代理 URL 暴露到公网。

## 自动保活

服务默认每 1 分钟执行一次轻量数据库检查。直连账号仍在 access token 剩余不足 45 分钟时刷新；使用专属代理的账号默认仅在剩余不足 5 分钟时刷新，可按账号调整为 5 到 90 分钟。数据库检查本身不会调用代理提取 API，默认代理账号通常约 115 分钟才提取一次新代理。该过程不会生成未消费的 `wx.login` code。

微信服务端可能只更新 access token 而不轮换 refresh token，因此后台保活不能保证 refresh token 永久有效。控制台会记录当前 refresh token 的首次观察时间；连续使用约 25 天后显示“建议重扫”，给可能存在的约 30 天失效窗口预留处理时间。只有微信实际返回不同的 refresh token 时，这个计时才会重置。

可以在 `.env` 中调整：

```dotenv
YYB_KEEPALIVE_INTERVAL=30m
YYB_KEEPALIVE_AHEAD=45m
```

将 `YYB_KEEPALIVE_INTERVAL` 设为 `0` 可关闭后台保活。提前续期遇到临时网络失败时会保留当前账号状态并在后续周期重试；凭据真正过期或 refresh token 被服务端撤销后会标记为需重扫，并停止保活和协议调用，避免继续消耗动态代理。

## 青龙、呆呆与 Arcadia 面板接入

支持对接 **青龙面板 (Qinglong)**、**呆呆面板 (daidai-panel)** 与 **Arcadia**：

- **Web 控制台配置**：可在 Web 控制台的“面板连接设置”中选择【青龙面板】或【呆呆面板 (daidai-panel)】，填入面板地址与对应的鉴权凭据（青龙使用 `Client ID` / `Client Secret`；呆呆面板使用 `App Key` / `App Secret`）。配置会持久化到 SQLite 数据库并优先于容器环境变量。
- **连接类型识别**：保存连接时若面板类型选错，且当前地址返回 `404` 或 `405`，系统会尝试另一种驱动；识别成功后使用正确的面板类型。
- **青龙环境变量配置**：
  ```dotenv
  PANEL_TYPE=qinglong
  QL_URL=http://qinglong:5700
  QL_CLIENT_ID=你的青龙ClientID
  QL_CLIENT_SECRET=你的青龙ClientSecret
  ```
- **呆呆面板环境变量配置**：
  ```dotenv
  PANEL_TYPE=daidai
  DAIDAI_URL=http://daidai-panel:5700
  DAIDAI_APP_KEY=你的呆呆面板AppKey
  DAIDAI_APP_SECRET=你的呆呆面板AppSecret
  ```
- **Arcadia 环境变量配置**：
  ```dotenv
  PANEL_TYPE=arcadia
  ARCADIA_URL=http://arcadia:5678
  ARCADIA_TOKEN=你的Arcadia_OpenAPI_Token
  ```
  在 Arcadia 的 OpenAPI 令牌中启用 `env:query`、`env:manage`、`cron:query`、`cron:manage`、`cron:run`、`file:list` 和 `file:read` 权限。Arcadia 不持久化定时任务 stdout，YYB 创建的账号独立任务会将日志写入 `/arcadia/log/yyb_account_*`，再通过 File OpenAPI 回读；现有 Arcadia 任务不会被改写。

三种面板的地址和凭据在控制台中按类型分别保存。切换面板类型只会切换当前运行面板，不会覆盖其他面板配置；Arcadia 账号独立任务的日志读取失败会直接显示面板返回的具体错误。

升级前已经使用青龙的部署不需要迁移配置，原有 `QL_URL`、`QL_CLIENT_ID` 和 `QL_CLIENT_SECRET` 会继续生效。面板适配层会分别处理三种面板的任务启停、运行状态与日志接口，避免混用不同的状态字段。

扫码成功页和账号控制台都提供“添加/同步到面板”按钮。同步会保留 `YYB_SERVER` 中已有的多行内容和环境变量备注，只追加缺少的账号，并同时识别账号 ID 与 OpenID，避免重复添加。

当面板和本服务都连接到 `qinglong_default` 网络后，面板环境变量可以填写：

```text
YYB_SERVER=yyb-go:8000@1
```

`@` 后可以使用控制台显示的账号 ID 或 OpenID。账号 ID 是本地数据库编号，删除并重新添加账号后可能变化；OpenID 更适合长期配置。

使用 OpenID 时要填写控制台显示的完整值，`OpenID` 只是说明文字，不能原样照填。例如：

```env
YYB_SERVER=yyb-go:8000@owNAxxxxxxxxxxxxxxxxxxxxxxxx
```

已确认报错的青龙/呆呆面板脚本修复版收录在 [`scripts/`](scripts/README.md)。

## API 示例

截图中的短路径均已提供兼容入口：

```text
/wx/code             获取小程序 code
/wx/getuserinfo      获取 YYB 账号用户信息
/wx/encryptkey       加密能力兼容转发（需要真实 payload）
/wx/getphonenumber   获取手机号
/wx/cloud            云函数（通过 operateWxData 传递 payload）
/wx/qrcodeauth       二维码授权会话
/wx/mpgeta8key       文章会话（通过 operateWxData 传递 payload）
/wx/appmsgext        文章扩展数据（通过 operateWxData 传递 payload）
/wx/appmsglike       文章点赞（通过 operateWxData 传递 payload）
```

这些接口不会伪造微信返回值。`/wx/encryptkey`、`/wx/cloud`、`/wx/mpgeta8key`、`/wx/appmsgext` 和 `/wx/appmsglike` 都是 `operateWxData` 兼容转发，调用方必须在 `payload` 中提供目标业务真实使用的 `api_name`、`data` 等字段，例如：

```json
{
  "ref": "1",
  "app_id": "wx0000000000000000",
  "payload": {
    "api_name": "callFunction",
    "data": {"name": "签到", "data": {}}
  }
}
```

`payload` 会原样传给微信协议层，路由名称不会自动生成目标业务参数。文章会话接口不能只根据文章 URL 推导 `api_name`、会话或点赞参数；需要抓取 PC 微信调用 `operateWxData` 时的原始请求体，而不是文章最终 HTTP 请求。

同样，业务接口中的 `encryptData` 不等于 `/wx/encryptkey` 可以直接返回的通用 Key。仅抓到业务服务器的最终 POST 无法确定它是微信能力、云函数还是小程序自己的 JavaScript 加密。若没有原始 `operateWxData` 调用，接口会返回明确的 `payload is required`，不会再发送已知无效的空 `getUserEncryptKey` 请求。

若目标能力并不经过 `operateWxData`，服务端会原样返回微信协议错误，需要根据目标小程序的原始调用补充专用协议实现。

获取 `wx.login` code：

```bash
curl -X POST http://yyb-go:8000/wxapp/getCode \
  -H 'Content-Type: application/json' \
  -d '{"ref":"1","app_id":"wx0000000000000000"}'
```

主动刷新单个账号状态：

```bash
curl -X POST http://yyb-go:8000/accounts/refresh \
  -H 'Content-Type: application/json' \
  -d '{"ref":"1"}'
```

Web 控制台内还提供完整的 OpenAPI 文档入口。

## 账号运行管理

在 `.env` 或 Web 控制台中配置自动化面板 OpenAPI 后，打开 `/runs`。以下为青龙示例：

```dotenv
QL_URL=http://qinglong:5700
QL_CLIENT_ID=你的青龙应用 Client ID
QL_CLIENT_SECRET=你的青龙应用 Client Secret
YYB_QINGLONG_SERVER=yyb-go:8000
YYB_QINGLONG_REPO=SuperNaiBA_YYB-GO-Script,525815266_YYB-Go-Enhanced/scripts
```

`YYB_QINGLONG_REPO` 填面板定时任务命令中的仓库目录，多个目录用英文逗号分隔。青龙通常使用 `task 仓库/脚本`，Arcadia 使用 `arcadia run 仓库/脚本`。通过本仓库订阅脚本时通常为 `525815266_YYB-Go-Enhanced/scripts`；通过上游脚本仓库订阅时为 `SuperNaiBA_YYB-GO-Script`。

管理页发现上述仓库目录中的 `.js` 和 `.py` 任务。每个“账号 + 脚本”会创建一个独立面板任务，新任务默认关闭；手动点击“运行一次”才会立即执行。账号变量会在任务执行前注入，运行日志按“账号 + 脚本”写入独立目录，管理页只读取当前账号的目录。

账号独立推送可从左侧“独立推送”直接进入，也可在 `/runs` 顶部当前账号区域下方配置。先选择账号，再为该账号单独选择 Server酱、PushPlus 或企业微信机器人；切换账号时页面会回读对应配置。推送 Token 不写入任务命令和 YYB 数据库，接口也不会返回明文。

如果原订阅生成的全局任务仍在运行，管理页会显示重复运行提示。迁移到账号任务后，请在青龙中停用对应的旧全局任务。

## 数据与安全

- 不要提交 `.env`、`data/`、SQLite 数据库、登录凭据或真实 OpenID。
- 建议仅在可信局域网内运行，不要把内部的 `yyb-go:8000` 接口直接暴露到公网。
- `wx.login` code 是短期且一次性的；refresh token 也可能被服务端撤销，失效后需要重新扫码。
- 本项目仅供学习和个人研究使用，请遵守相关平台条款及所在地法律法规。

## 来源说明

本项目基于 [SuperNaiBA/YYB_GO](https://github.com/SuperNaiBA/YYB_GO) 整理和增强，主要补充了账号信息展示、OpenID 可见性、Web 控制台资源修复、Docker 部署与访问保护。请同时遵守上游项目的授权条件；如需分发或商业使用，请先取得相应权利人的许可。
