# 配置与账号安全

本文集中说明 YYB Go Enhanced 的运行配置、网页登录与数据安全。基础部署步骤见[根 README](../README.md)。

## 配置方式

Docker Compose 部署推荐复制 `.env.example` 为 `.env`，再修改需要的变量：

```bash
cp .env.example .env
docker compose up -d --build
```

原生二进制和 Magisk 部署应把同名变量写入对应的进程环境或模块配置。环境变量只在服务启动时读取，修改后需要重启服务。

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
