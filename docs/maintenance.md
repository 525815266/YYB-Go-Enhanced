# 面板更新与重启（v0.2.24，2026-10-02）

管理员点击任意页面顶栏的版本号即可检查更新并拉取新镜像；完整状态与独立重启入口位于左侧「管理 → 系统维护」。普通用户不能执行维护，后端同样校验管理员会话；关闭登录鉴权的本机模式也不能执行维护。检查版本缓存 5 分钟，不会随普通页面刷新反复访问 GitHub。

版本检查会同时请求 GitHub Raw 与 Contents API，优先采用最先返回的合法版本号。两个来源都失败时，再通过 `github.com` 的官方 Release 跳转查询已发布的正式版本；备用查询不占用 GitHub API 的匿名配额，也不会下载安装包。成功结果缓存 5 分钟，全部失败仅缓存 30 秒；取消请求不会缓存成网络故障。无法确认版本时不会触发重建或降级。

Release 备用来源只反映已发布版本，可能暂时落后于主分支。Docker 维护执行器仍检查镜像标签是否与目标版本一致，不会因某个来源恢复就跳过校验。

## 检查更新报连接重置或 HTTP 403（Issue #74）

`connection reset by peer` 表示连接被重置；`HTTP 403` 表示访问被拒绝，可能来自 GitHub 限流、出口限制或代理。只有返回 `X-RateLimit-Remaining: 0` 或 HTTP 429 等明确信号时，程序才提示限流，不能仅凭 403 确定原因。

检查由 **YYB 服务所在容器 / 设备**发出。电脑浏览器能打开 GitHub，不代表容器出口也正常；工作台里的“账号代理”用于账号协议请求，不会自动成为版本检查或 Docker 下载镜像的代理。

可在官方 Docker 容器内分别检查三个来源（`yyb-go` 替换为实际容器名）：

```bash
docker exec yyb-go wget -S -O /dev/null -T 15 'https://raw.githubusercontent.com/525815266/YYB-Go-Enhanced/main/VERSION'
docker exec yyb-go wget -S -O /dev/null -T 15 'https://api.github.com/repos/525815266/YYB-Go-Enhanced/contents/VERSION?ref=main'
docker exec yyb-go wget -S --spider -T 15 'https://github.com/525815266/YYB-Go-Enhanced/releases/latest'
```

网络恢复后等待 30 秒再检查。若需要代理，可为 YYB 服务进程配置标准 `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`；代理地址必须能从容器访问，`127.0.0.1` 在容器里指向容器本身。分享诊断时请隐去代理密码、Cookie 和令牌。

现有标准 Go HTTP 客户端会读取这些变量，并非使用自定义 `http.Client` 就会忽略代理。v0.2.24 增加下列可选配置；修改后需重新创建容器或重启独立程序。配置有误只阻止版本检查，面板与账号功能仍可启动。

### 仅版本检查使用代理

```yaml
# 合并到现有 yyb-go 服务，不要替换原来的配置
environment:
  YYB_UPDATE_PROXY: http://host.docker.internal:20173
extra_hosts:
  - "host.docker.internal:host-gateway"
```

`YYB_UPDATE_PROXY` 支持 `http://`、`https://`、`socks5://`、`socks5h://` 地址，可携带代理认证信息。显式设置后优先于系统代理和 `NO_PROXY`，只作用于版本查询，不修改全局 HTTP transport、账号代理、青龙连接或 Docker daemon。未设置时仍使用标准环境变量。`host-gateway` 需要 Docker 20.10+，它只解决宿主机地址解析，不能使只监听宿主机 `127.0.0.1` 的端口被容器访问。

Issue #74 的实际部署是 Armbian + Docker bridge，v2rayA 仅监听宿主机 `127.0.0.1:20172`。作者反馈用 socat 的宿主机 `20173` 端口转发后恢复。优先让代理监听 Docker 可访问的接口并限制来源；若必须转发，下面是**已限制客户端网段**的 systemd 示例：

```ini
[Unit]
Description=Proxy forward for Docker update checks
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP4-LISTEN:20173,bind=0.0.0.0,reuseaddr,fork,range=172.18.0.0/24 TCP4:127.0.0.1:20172
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

该示例需要宿主机已安装 socat。`172.18.0.0/24` 必须替换为实际 Docker 子网；结合宿主机防火墙限制该端口，不应把无认证转发暴露到局域网其他设备或公网。不要为此修改 YYB 数据卷或切换 host 网络，也无需开启 `route_localnet` 或加入 DNAT 规则。端口可达性取决于监听地址、占用和防火墙，不能只靠添加 `extra_hosts` 判断已配置成功。

如果选择全局 `HTTP_PROXY` / `HTTPS_PROXY`，应按 Go 支持的 CIDR 和实际服务名设置排除范围，例如：

```yaml
NO_PROXY: localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,qinglong,yyb-go
```

`172.`、`192.168.`、`10.*` 不是 IP 网段匹配写法；按服务名访问时也应列出服务名，不能依赖 DNS 解析后匹配 CIDR。全局变量可能影响其他采用系统代理的请求，只有更新需要代理时优先使用 `YYB_UPDATE_PROXY`。

### 可选的自定义版本源

```yaml
environment:
  # 填写完整的 VERSION 文件地址，不是镜像站首页或仅一个 URL 前缀
  YYB_UPDATE_VERSION_URL: https://your-trusted-mirror.example/YYB-Go-Enhanced/main/VERSION
```

默认不配置、不绑定任何第三方镜像。配置后先访问该地址，要求返回 `X.Y.Z` 纯文本或 GitHub Contents API 的 base64 内容，响应上限 4 KiB；网络错误或无效内容会自动回退到原官方 Raw / Contents API / Release 流程。该来源也使用版本检查代理。

**仅接受自己信任的版本源。**格式校验不代表来源可信；镜像缓存可能落后，不会保证立即看到最新版本，也不会因此降级。此配置只查询版本号，不改写镜像、Release 页面或安装包下载地址；不能用它解决 Docker 拉取 `ghcr.io` 的网络问题。保留 TLS 证书验证，不采用固定 GitHub IP 或关闭 TLS 校验。

前端检查与提交更新的请求等待上限为 45 秒，覆盖自定义源失败后逐级回退的时间；每个版本请求仍有 10 秒超时，普通维护状态查询仍为 15 秒。

**检查版本成功不代表镜像拉取成功**：Docker 镜像由宿主机 Docker daemon 从 `ghcr.io` 拉取，它的代理和网络配置独立于 YYB 容器。Docker 在线更新还需要下文的维护执行器；未配置时应按原 Compose 方式更新。三个 GitHub 来源都无法访问且未配置可用的自定义版本源时，仍需修复服务器网络，备用来源不能保证所有网络环境可达。

## 部署方式与能力

| 部署 | 检查版本 | 面板更新、重启 |
| --- | --- | --- |
| Docker Compose，已配置下面的宿主机执行器 | 支持 | 支持，仅指定 YYB 服务 |
| Docker，未配置执行器 | 支持 | 禁用，按原部署方式 pull/up 或 restart |
| Windows/Linux/macOS 裸机 | 支持 | 发现新版本后下载匹配当前系统与架构的独立程序；停止旧进程、替换文件后重启 |
| Magisk ARM64 | 支持 | 发现新版本后下载匹配版本的模块 ZIP；使用 Magisk 管理器安装并按提示重启设备 |
| 不在 Release 矩阵内的系统或架构 | 支持 | 显示 Release 页面，不提供错误架构的下载按钮 |

参考了 sub2api 的检查版本、确认操作和失败恢复流程。Docker 更新完整镜像；独立二进制已内嵌控制台资源，但仍采用“下载、停止、替换、重启”的显式流程，不在运行中覆盖自身。

裸机未显式传 `-resource-root` 时，程序从 `resource/.web-assets/v<版本>` 加载当前版本内嵌页面。替换 EXE 后会使用新的版本目录，不会继续读取旧脚本；账号数据库、头像和二维码仍保存在原 `resource` 目录。需要维护自定义模板或静态文件时，应显式传入 `-resource-root <目录>`，该目录内已有文件不会被程序覆盖。

## Docker 首次配置（可选）

需要宿主机 Python 3.9+、Docker Compose v2，以及已经健康运行的 YYB Compose 服务。执行器需要 Docker 管理权限，**不要把 `/var/run/docker.sock` 挂进 Web 容器，也不要把执行器暴露到公网**。

1. 保存 `packaging/maintenance/agent.py` 到宿主机仅管理员可写的目录，例如 `/opt/yyb-maintenance/agent.py`。
2. 使用一份包含完整部署参数的 Compose 文件（不要漏掉平时使用的 override 文件）；保留原来的端口、网络、环境变量、数据库及所有数据卷。镜像改为 `ghcr.io/525815266/yyb-go-enhanced:latest` 或 `:main`，至少先升级到 v0.2.10。
3. 通过 `docker exec yyb-go id -g` 查看容器组 ID。官方 Alpine 镜像通常为 **101**，请以实际输出为准。
4. 在 YYB 服务中额外加入：

```yaml
environment:
  YYB_MAINTENANCE_SOCKET: /run/yyb-maintenance/control.sock
volumes:
  - /run/yyb-maintenance:/run/yyb-maintenance:ro
```

这只是增量示例，不能替换原来的 environment/volumes。执行器创建目录和 socket 后，再重建 YYB 服务。

5. 将执行器交给宿主机服务管理器运行。以下是 systemd 示例，路径和组 ID 必须对应自己的部署：

```ini
[Unit]
Description=YYB scoped maintenance agent
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=root
RuntimeDirectory=yyb-maintenance
RuntimeDirectoryMode=0750
ExecStart=/usr/bin/python3 /opt/yyb-maintenance/agent.py --compose /opt/yyb-go/compose.yaml --service yyb-go --socket /run/yyb-maintenance/control.sock --gid 101
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

保存为 `/etc/systemd/system/yyb-maintenance.service` 后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now yyb-maintenance
```

执行器只监听 Unix socket，只接受 `update`、`restart` 两种动作。浏览器不能指定 Compose 路径、容器名、镜像源、下载 URL 或任意命令。指定的 Compose 文件及 `.env` 必须保持仅管理员可写。

## 操作行为

- 先显示内联二次确认；重复点击不会同时启动两个维护任务。刷新页面可重新读取执行进度。
- 下载镜像时旧服务继续运行；只有镜像版本标签与检查到的版本一致才切换。主分支已更新而镜像仍在构建时，会明确拒绝切换，稍后重试即可。
- 使用 `up --no-deps --no-build` 只重建 YYB 服务，保留 Compose 配置及数据卷。新容器通过 Docker healthcheck 后才报告成功；断线本身不是成功标志。
- 镜像切换失败或健康检查超时，尝试恢复旧镜像并再次检查。旧镜像不会自动删除。**这不是数据库备份或数据库回滚**，升级前应备份 SQLite/MySQL；跨版本不兼容时仍需人工恢复数据。
- 重启只重启现有服务，不下载镜像、不重启宿主机。
- 执行器自身退出会中断任务，因此维护期间不要重启它。若执行器被强杀，重新启动前应核对 Docker 实际状态；面板「尚未执行」不是上一任务成功。

若不使用执行器，原更新方式不受影响：

```bash
docker compose pull yyb-go
docker compose up -d --no-deps yyb-go
docker compose logs --tail=50 yyb-go
```

首次配置的镜像来源、部署目录和重启方式因人而异，不会通过一个不受限的网页 shell 自动猜测或修改。
