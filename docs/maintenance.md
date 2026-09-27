# 面板更新与重启（v0.2.10，2026-09-27）

管理员从左侧「管理 → 系统维护」进入。普通用户没有此入口，后端同样校验管理员会话；关闭登录鉴权的本机模式也不能执行维护。检查版本缓存 5 分钟，不会随普通页面刷新反复访问 GitHub。

## 部署方式与能力

| 部署 | 检查版本 | 面板更新、重启 |
| --- | --- | --- |
| Docker Compose，已配置下面的宿主机执行器 | 支持 | 支持，仅指定 YYB 服务 |
| Docker，未配置执行器 | 支持 | 禁用，按原部署方式 pull/up 或 restart |
| Magisk | 支持，显示主分支版本供参考 | 本轮不支持在线替换模块；使用模块管理器安装 Release，再重启设备 |
| 源码、裸机 | 支持 | 本轮不支持，使用原服务管理器 |

参考了 sub2api 的检查版本、确认操作和失败恢复流程。YYB 的模板与静态资源不嵌入二进制，因此 Docker 更新的是完整镜像，而不是只覆盖可执行文件；不直接照搬二进制自更新。

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
