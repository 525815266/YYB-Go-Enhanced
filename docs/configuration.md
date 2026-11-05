# 配置与账号安全

本文集中说明 YYB Go Enhanced 的运行配置、网页登录与数据安全。基础部署步骤见[根 README](../README.md)。

## 配置方式

Docker Compose 部署推荐复制 `.env.example` 为 `.env`，再修改需要的变量：

```bash
cp .env.example .env
docker compose pull yyb-go
docker compose up -d --no-build
```

首次安装还需要初始化数据目录权限和面板网络，完整步骤见 [Docker 部署说明](docker-deployment.md)。已有配置请保留，不要重新复制示例覆盖它。

原生二进制从 v0.2.25 起自动读取 `.env`，Windows、Linux、macOS 使用相同规则：

1. 使用 `-env-file 路径` 时只读取指定文件；相对路径按启动时的工作目录解析，文件不存在或格式错误会停止启动。
2. 未指定时，先查找程序所在目录的 `.env`，没有才读取当前工作目录的 `.env`，两份文件不会合并；都不存在则沿用进程环境和默认值。
3. 配置优先级为 **显式命令行参数 > 已有进程环境变量 > 所选 `.env` > 默认值**。进程中已设置为空的变量也不会被文件覆盖。

Windows 可把 `.env.example` 复制为程序旁的 `.env`，注意开启文件扩展名显示，避免保存成 `.env.txt`。使用 UTF-8 编码（支持 BOM 和 CRLF）；密码含 `#`、`$` 等字符时用单引号包裹。最小本机配置示例：

```dotenv
YYB_BIND_ADDRESS=127.0.0.1
YYB_PORT=8000
YYB_AUTH_DRIVER=sqlite
YYB_ADMIN_USER=admin
YYB_ADMIN_PASSWORD='请替换为强密码'
YYB_KEEPALIVE_INTERVAL=2m
YYB_KEEPALIVE_AHEAD=4m
```

```powershell
# PowerShell：也可选用其他文件，-port 显式覆盖文件中的 YYB_PORT
.\yyb-go-windows-amd64.exe -env-file .\production.env -port 8001
```

`YYB_BIND_ADDRESS`、`YYB_PORT`、`YYB_KEEPALIVE_INTERVAL`、`YYB_KEEPALIVE_AHEAD` 分别对应 `-host`、`-port`、`-keepalive-interval`、`-keepalive-ahead`；其余已有环境变量配置也支持从文件读取。监听 `0.0.0.0` 才允许从其他设备访问，请同时配置登录与协议鉴权。

Magisk 继续优先使用模块配置。Docker Compose 的 `.env` 用于 Compose 变量替换，仅写入该文件不代表任意变量都会传入容器，请核对服务的 `environment`/`env_file`。配置只在服务启动时读取，修改后需要重启；Docker 配置变化通常需要重新创建容器。

## Web 用户数据库

网页登录、用户、角色和会话默认保存在 SQLite：

```dotenv
YYB_AUTH_DRIVER=sqlite
YYB_AUTH_DSN=
```

默认文件为 `resource/db/auth.db`。微信账号、协议会话、代理和账号配置使用独立的协议 SQLite 数据库；切换 Web 用户数据库不会迁移或覆盖这些数据。

需要复用 MySQL 时：

```dotenv
YYB_AUTH_DRIVER=mysql
YYB_AUTH_DSN=yyb_go:数据库密码@tcp(mysql:3306)/yyb_go?charset=utf8mb4&parseTime=true&loc=UTC
```

MySQL 必须已创建目标数据库，且服务进程能够访问该地址。旧变量 `YYB_AUTH_MYSQL_DSN` 继续兼容；仅设置旧变量时会自动选择 MySQL。`YYB_AUTH_DRIVER=none` 会关闭网页登录，只适合受保护的本机调试环境。

## 管理员与普通用户

可在首次启动前预设管理员：

```dotenv
YYB_ADMIN_USER=admin
YYB_ADMIN_PASSWORD=请替换为强密码
```

未预设管理员时，第一个完成注册的用户自动成为管理员，之后注册的用户默认为普通用户。

- 普通用户可以管理本人微信账号、代理、授权链接、推送和账号级脚本任务。
- 管理员可以管理全部账号、用户、面板连接、全量同步和系统维护。
- 修改密码会注销该用户的其他会话；管理员重置密码或停用用户会注销其全部会话。
- 管理员可在控制台关闭公开注册。

### 忘记管理员密码（v0.2.32 起）

在部署机器的终端执行本地恢复命令，最后的 `admin` 替换为**实际管理员用户名**。按提示输入两次新密码（不回显，10–72 字节）；成功后命令退出，不启动另一份 Web 服务，也不执行保活或面板同步。

Docker（容器名按实际修改）：

```bash
docker exec -it yyb-go /app/yyb-go -resource-root /app/resource -reset-admin-password admin
```

Windows（在平时启动程序的目录执行）：

```powershell
.\yyb-go-windows-amd64.exe -reset-admin-password admin
# 使用自定义配置和数据目录时，应与日常启动参数一致：
.\yyb-go-windows-amd64.exe -env-file .\production.env -resource-root D:\YYB\resource -reset-admin-password admin
```

Linux / macOS：

```bash
./yyb-go-linux-amd64 -resource-root ./resource -reset-admin-password admin
```

命令与服务共用 `.env`、`YYB_AUTH_DRIVER`、`YYB_AUTH_DSN` 和旧 MySQL 变量的选择规则；Docker 会读取容器的现有环境变量。自定义配置、SQLite 路径或 MySQL 连接必须与服务保持一致；不会因为忘记密码而自动切换数据库。SQLite 文件不存在会直接报错，不创建空库。Magisk 请在 root 终端先加载 `/data/adb/yyb-go/config.conf` 并导出其中的 `YYB_AUTH_DRIVER`、`YYB_AUTH_DSN`、`YYB_AUTH_MYSQL_DSN`，再使用模块内的 `bin/yyb-go`，指定 `-resource-root /data/adb/yyb-go/resource`；不要重新运行 `service.sh` 代替恢复命令。

仅重置指定的、已启用的管理员密码，并在同一事务内注销该用户的全部旧会话；普通用户、其他管理员、账号归属、微信凭据、代理和面板设置保留。不自动创建用户、提升普通用户权限或启用已停用用户。自动化时也可从标准输入提供一行密码；推荐交互输入，避免把密码写进命令历史、截图或日志。

`YYB_ADMIN_USER` / `YYB_ADMIN_PASSWORD` **仅初始化空用户库**，修改它们不会覆盖已有密码。不要删除 `auth.db` 来重置密码，也不要以关闭网页登录作为恢复办法。该能力仅在持有部署机器终端权限时可用，没有免鉴权的 Web 重置接口。

## Cookie 与 HTTPS

局域网 HTTP 部署保持：

```dotenv
YYB_COOKIE_SECURE=false
```

通过 HTTPS 反向代理提供服务时改为：

```dotenv
YYB_COOKIE_SECURE=true
```

开启后浏览器只会通过 HTTPS 发送登录 Cookie。若直接使用 HTTP 却设置为 `true`，会表现为登录后立即回到登录页。

## 服务监听

```dotenv
YYB_BIND_ADDRESS=0.0.0.0
YYB_PORT=8000
```

`0.0.0.0` 会监听所有网卡。只需本机访问时可改为 `127.0.0.1`；需要让另一台青龙服务器访问时，应监听局域网地址并通过防火墙限制来源，不要直接暴露到公网。

## 接口与集成令牌

### 协议接口令牌

`YYB_PROTOCOL_TOKEN` 用于保护 `/wx/*` 与 `/wxapp/*` 自动化接口：

```dotenv
YYB_PROTOCOL_TOKEN=请生成随机长字符串
```

启用后，调用方必须携带：

```http
Authorization: Bearer <token>
```

控制台「接口测试」在「协议令牌」输入框填写；接口文档使用 `Authorize → ProtocolBearer`（只填令牌，不带 `Bearer`）。这两个入口从 v0.2.35 起提供。控制台输入只在当前页面使用，不保存，刷新后需重新填写；服务器不会把已配置的令牌回传给页面。网页登录与协议令牌分别校验。

未设置时保留旧版兼容行为，因此更应把接口限制在可信内网、VPN 或受控反向代理后。

### 控制面集成令牌

`YYB_INTEGRATION_TOKEN` 用于可信控制面调用集成接口，与网页登录密码、面板 Client Secret 和协议接口令牌不是同一种凭据：

```dotenv
YYB_INTEGRATION_TOKEN=请生成另一条随机长字符串
```

不要在脚本、截图、Issue 或公开日志中提交这些令牌。

## 授权链接加密密钥

```dotenv
YYB_ACCOUNT_LINK_KEY=请生成随机长字符串
```

该密钥用于加密保存授权短链接的历史 URL。生产环境应显式设置，并在升级、迁移和恢复时保持不变。更换密钥后，旧记录可能无法解密。

## 面板账号引用方式

默认使用数字账号 ID 写入面板 `YYB_SERVER`。如果账号可能删除或重排，可改用稳定的 OpenID：

```dotenv
YYB_QINGLONG_REF_MODE=openid
```

可选值为 `id` 或 `openid`。旧变量 `YYB_ACCOUNT_REF_MODE` 继续兼容。切换后应在控制台重新同步 `YYB_SERVER`，已有脚本环境变量不会凭空改写。

## 本机微信快速授权

该能力默认关闭：

```dotenv
YYB_ENABLE_PC_LOGIN=false
```

在已验证的 Windows 桌面微信环境中可改为 `true`。浏览器会尝试连接 `https://localhost.weixin.qq.com`，由本机微信完成确认，再把一次性回调交给 YYB Go。

限制如下：

- 本机微信必须已登录且未锁定。
- 浏览器和企业安全策略必须允许访问微信本地服务。
- 微信版本或 OAuth Cookie 会话不兼容时会自动回退到手机扫码。
- 该流程仍需要用户确认，不提供无交互的静默登录。

## 协议设备标识（v0.2.58 起）

```dotenv
YYB_DEVICE_IDENTITY=stable
```

默认 `stable`：每个账号固定一套设备，ManualAuth 的 AppDeviceId 和每个取码 / 取号 / 转发请求携带的设备地址都保持不变，与官方客户端持久保存设备的做法一致。设备由实例首次启动时生成并保存在 `app_settings` 的随机种子与账号 OpenID 派生，重启、缓存重建、重新扫码都不变；不同账号、不同部署之间互不相同。旧版每次握手和每个请求都换新的随机设备。

改为 `random` 可完全恢复旧行为。其他值会在启动时报错。已有协议会话不会因升级重新握手，下一次握手才使用固定设备；会话自带的原生 AutoAuth 仍使用建立时的设备。这是贴近官方行为、降低“同一账号频繁出现新设备”的调整，不能保证上游不再拒绝授权。

## 掉线通知（v0.2.58 起）

账号因授权被拒变为“需重新扫码”或“需补授权”时，每次授权只推送一次提醒；重新扫码后再次失效会重新提醒。手动标记失效不推送。

```dotenv
YYB_NOTIFY_CHANNEL=pushplus
YYB_NOTIFY_TOKEN=你的 token
YYB_NOTIFY_TOPIC=
YYB_NOTIFY_ACCOUNT_PUSH=true
```

- `YYB_NOTIFY_CHANNEL`：`serverchan`（Server酱，支持 Server酱³ 的 `sctp` 开头 SendKey）、`pushplus` 或 `qywx`（企业微信群机器人 key）；留空关闭管理员渠道。设置了渠道却没有 token、或渠道名无效时启动报错。
- `YYB_NOTIFY_TOPIC`：仅 PushPlus 群组编码使用。
- `YYB_NOTIFY_ACCOUNT_PUSH`：默认 `true`，账号已在面板配置脚本推送渠道时，也推送到该账号自己的渠道，多用户部署可以通知到账号主人；设为 `false` 只推送到管理员渠道。令牌仍保存在面板环境变量中，仅在发送时读取。

推送内容只含账号编号、备注 / 昵称、渠道、状态、时间和数字诊断码，不含 OpenID、凭据或 token。发送是异步的，不阻塞续期和协议请求；结果记录在“运行日志”的 `account.notified` / `account.notify_failed`。

## 取码熔断（v0.2.59 起）

```dotenv
YYB_CIRCUIT_BREAKER=on
```

默认 `on`。熔断只控制流量，从不修改账号状态、清空凭据或触发续期；被熔断的请求立即返回 HTTP `429`（带 `Retry-After` 和 `data.circuit_open`），不再发往上游，与上游 `502`、重扫 `409` 区分，脚本应据此退避。

- **目标层：** 同一账号 + AppID + 请求类型（取码 / 取号 / operateWxData）在 30 分钟内连续 2 次被应用层 `-14` 拒绝，或连续 3 次返回空 code，暂停该目标 10 分钟。取号 / operateWxData 被拒不会拦住同 AppID 的取码（v0.2.60 起）。
- **会话层：** 同一账号 5 分钟内有 3 个不同 AppID 的取码被 `-14` 拒绝，暂停该账号全部业务请求 15 分钟。
- 一次成功清空该账号的会话层和对应目标；重新扫码、手动置失效也会清空。
- 记录：运行日志 `protocol.circuit_open` / `protocol.circuit_deferred`，v0.2.60 起事件带 `app_id` 与请求类型。
- 设为 `off` 恢复旧行为；阈值为编译期默认值。详见[取码熔断与触发规律](code-circuit-breaker-20261101.md)。

## 登录预算与取码限速（v0.2.61 起）

```dotenv
YYB_LOGIN_BUDGET=3
YYB_LOGIN_WINDOW=10m
```

同一账号因业务请求触发的 OAuth 层调用（新的登录票据、ManualAuth 恢复、强制刷新）在 `YYB_LOGIN_WINDOW` 内最多 `YYB_LOGIN_BUDGET` 次（默认 10 分钟 3 次）。用完后，本来要重新登录的请求保持它自己的错误（取码是原来的 `-14`），不再向上游登录，也不登记该 AppID 的退避，缓存会话也不作废；窗口滑过后同一 AppID 仍可修复。后台续期和面板手动刷新不受限。`off` 或 `0` 关闭。拒绝只汇总成 `protocol.login_limited` 事件。依据和验证方法见[任务高峰、重新登录与登录预算](code-pacing-20261102.md)。

取码限速默认开启：

```dotenv
YYB_CODE_MIN_GAP=3s
YYB_CODE_MAX_WAIT=20s
```

同一账号向上游发出的取码 / 取号 / operateWxData 请求至少相隔 `YYB_CODE_MIN_GAP`，槽距现在超过 `YYB_CODE_MAX_WAIT` 的请求立即返回 HTTP `429`（带 `Retry-After`，`data.circuit_open` 为 `pacing`），不访问上游，也不修改账号状态；`0` 或 `off` 关闭。熔断和登录预算只统计失败与 OAuth 层调用，一个全部成功的突发它们拦不住，限速补的就是这个缺口。依据和限制见[任务高峰、重新登录与登录预算](code-pacing-20261102.md)。

不重启就要调整时，在数据库目录（Compose 默认 `data/db`）放一个 `tuning.json`，一秒内生效：

```json
{"login_budget": 3, "login_window_ms": 600000, "code_min_gap_ms": 3000, "code_max_wait_ms": 20000, "fresh_session_window_ms": 120000}
```

缺少的键沿用上面的配置值，文件无法解析时保留上一次有效设置，删除文件恢复配置值。

## 刚成功后的 `-14` 保护（v0.2.65 起可调）

```dotenv
YYB_FRESH_SESSION_WINDOW=2m
```

同一账号的会话刚取到过 code 后的这段时间内，单个 AppID 的 `-14` 被当成该 AppID 自己的拒绝，不再取新票据重新登录（v0.2.60 起，之前写死为 2 分钟）。默认 `2m`，行为不变；`off` 或 `0` 关闭；也可以写成其他时长。`tuning.json` 的 `fresh_session_window_ms` 不重启生效（`0` 关闭，上限 1 小时）。

什么时候需要动它：脚本在同一账号上批量取不同 AppID 的 code，而这个账号的会话用一次就要重新登录才能换下一个 AppID 时，这个保护会把本来能修好的 `-14` 放弃，随后会话熔断（3 个不同 AppID 的 `-14` 暂停整号 15 分钟）又把这些 `-14` 当成整个会话被拒。实验室里同样的 40 个 AppID，默认只拿到 1 个，关闭后拿到 40 个。要接近这个取码量，还要同时放开 `YYB_LOGIN_BUDGET` 和 `YYB_CODE_MIN_GAP`；熔断可以保持开启。代价是本机不再替你限制密集登录，而密集登录是否会让授权被吊销目前没有答案，所以默认值没有改。数据、限制和复现方式见[取码量对照实验](dense-batch-lab-20261103.md)。

## 取码 AppID 保护

v0.2.63 起，取码请求带着已知的网站应用 AppID（腾讯电脑管家 `wx5cd60c5d4817a188`、京东网页登录护照 `wx2f5d8f9715c59d10`）时，服务端直接返回 `403`，请求不发往上游。依据见[网站应用 AppID 取码与授权失效](website-appid-getcode-20261103.md)。

```dotenv
YYB_CODE_ALLOW_APPIDS=
YYB_CODE_DENY_APPIDS=
```

两项都是逗号或空格分隔的 AppID 列表，大小写不敏感。`YYB_CODE_ALLOW_APPIDS` 放行指定 AppID，优先级最高；`YYB_CODE_DENY_APPIDS` 在内置名单之外追加自己的禁用名单。被拒绝的请求不占限速槽位、不进熔断计数，日志里记为 `protocol.appid_denied`。只影响取码请求，取号和 operateWxData 不受影响。

## 数据目录与备份

Compose 默认持久化：

```text
data/db
data/avatars
data/qr
```

升级前至少备份 `data/db`；使用外部 MySQL 时还要单独备份对应数据库。不要把以下内容提交到 Git：

- `.env` 与数据库文件；
- OpenID、微信登录凭据和协议会话；
- 代理提取 URL、账号密码及白名单密钥；
- 面板 Client Secret、API Token 和授权链接密钥。

镜像或二进制回滚不等于数据库回滚。跨版本恢复时，应同时核对程序版本和数据备份时间。

## 运行参数面板（v0.2.78）

取码限速、登录预算、新鲜会话窗口与动态黑名单开关/阈值可以在面板「个人设置 → 运行参数」（管理员）在线调整，也可以继续用环境变量或数据库旁的 `tuning.json`。三者的优先级：`tuning.json`（面板保存即写它，1 秒内生效并保留到重启之后）> 环境变量默认值；「恢复 env 默认」按钮删除 tuning 文件。动态黑名单的热开关与阈值不写入 tuning 文件，重启后回到 env 配置。仍需环境变量/重启的配置（密钥、鉴权数据库、监听地址、通知渠道等）不在面板范围内。
