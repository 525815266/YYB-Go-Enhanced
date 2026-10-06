# Docker / Armbian 部署与排错

## 优先使用官方镜像

v0.2.26 起，仓库 `compose.yaml` 的默认镜像为 `ghcr.io/525815266/yyb-go-enhanced:latest`，自动匹配 Linux amd64 / arm64。使用 `docker compose pull yyb-go` 后执行 `docker compose up -d --no-build`，无需在低性能盒子上下载 Go 依赖、编译或运行构建测试。首次安装的目录初始化见 [README](../README.md#docker-compose-快速开始)。

`.env` 可设置 `YYB_IMAGE=ghcr.io/525815266/yyb-go-enhanced:0.2.26` 固定版本。旧 Compose 如果仍写着 `local/yyb-go:58492557cc66`，请先更新镜像行，保留自己的挂载路径、端口与配置；仅执行 pull 不会自动把旧文件改成官方镜像。

这是应用镜像选择，不能修复 Docker daemon 本身访问 GHCR 的网络问题。32 位 ARM 系统不在 Docker 镜像矩阵内，可使用 Release 中的 Linux ARMv7 原生程序；查看 `uname -m` 与 `getconf LONG_BIT` 确认实际系统。

## 青龙容器名与网络名不同

查看青龙加入的网络（将 `qinglong` 换成实际容器名）：

```bash
docker network ls
docker inspect qinglong --format '{{json .NetworkSettings.Networks}}'
```

`qinglong_default` 是默认建议的用户自建网络名。`YYB_DOCKER_NETWORK` 应填写真实的 Docker 网络名，而不是直接填写容器名。Docker 默认 `bridge` 不提供这里需要的容器名解析；可增加用户自建网络，无需删除现有网络或重建青龙数据：

```bash
docker network inspect qinglong_default >/dev/null 2>&1 || docker network create qinglong_default
docker network connect qinglong_default qinglong
```

已经连接时无需重复执行 connect。在青龙自己的 Compose 中也声明同一外部网络，避免下一次重建丢失连接。两者同网后，YYB 的面板地址可填 `http://qinglong:5700`，青龙里的 YYB 地址可填 `yyb-go:8000`。跨主机部署应使用实际可达地址。

## SQLite / permission denied

镜像以非 root 的 `yyb` 用户运行。宿主机自动创建的 bind mount 目录可能属于 root；部分 NAS 继承的 ACL 还会让目录权限显示为 `000`，仅修改属主仍不可写。这时 SQLite 可能显示 `unable to open database file: out of memory (14)`，不一定是内存耗尽。先确认实际挂载路径并备份数据库；在停止该服务后，仅修正本项目的三个数据目录：

```bash
# 在本项目部署目录执行；下面必须是实际使用的 data 目录
docker compose stop yyb-go
mkdir -p data/db data/avatars data/qr
docker run --rm --user 0 --entrypoint sh \
  -v "$PWD/data:/data" ghcr.io/525815266/yyb-go-enhanced:latest \
  -c 'chown -R yyb:yyb /data/db /data/avatars /data/qr && chmod -R u+rwX /data/db /data/avatars /data/qr'
docker compose up -d --no-build
```

不要对整个磁盘执行权限修改，也无需设置 `chmod 777`。如使用 rootless Docker、NFS 或启用了 SELinux，还需核对对应挂载和身份映射规则。

## 源码构建失败

`RUN go test ./...` 报 FAIL 表示测试阶段失败，不等于已经证明是 DNS 或 ARM 不兼容。后面的 `[GIN] ... 200` 日志可能只是其他已通过测试的输出。应保留日志中**第一处 `--- FAIL: Test...` 及其错误行**：

```bash
docker compose --progress plain build yyb-go > build.log 2>&1
```

依赖下载已增加最多三次重试，全部失败时构建明确失败；不默认关闭测试，也不全局强制 Go DNS。Issue #78 中跳过测试后成功启动，仅能证明构建绕过了失败阶段，原测试根因仍需完整日志确认。可以先使用 CI 已测试的官方镜像运行，提供脱敏后的首个失败测试再继续定位。
