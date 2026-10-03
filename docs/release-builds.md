# 构建与发布

仓库使用根目录 `VERSION` 作为 Docker、Magisk 和原生二进制的统一版本来源。正式发布前应先更新版本号和 `CHANGELOG.md`，再创建对应标签。Release 正文由 `tools/release_notes.py` 从当前版本章节生成，包含本次更新、下载选择、升级提示和相关文档，不再只显示 GitHub 自动生成的 `Full Changelog`。

## Docker 镜像

工作流：`.github/workflows/docker-publish.yml`

发布到：

```text
ghcr.io/525815266/yyb-go-enhanced
```

支持 `linux/amd64` 和 `linux/arm64`。触发方式：

- 服务端、运行资源、Go 依赖、Docker 文件或工作流变更并推送到 `main`；
- 推送 `v*` 标签；
- 在 Actions 中手动运行并指定镜像 Tag。

普通 README、文档和业务脚本修改不会触发 Docker 构建。工作流使用内置 shell 命令检出、生成标签和登录 GHCR，避免在作业启动前依赖额外的第三方 Action 下载；网络步骤带有限次重试。

常用镜像标签：

```text
latest
main
<版本号>
sha-<短提交>
```

## Magisk 模块

工作流：`.github/workflows/magisk-publish.yml`

当前只构建 Android ARM64 模块。触发方式：

- Magisk 打包文件、服务端、运行资源或 Go 依赖变更并推送到 `main`；
- 推送 `magisk-vX.Y.Z` 标签时创建或更新 Release；
- 在 Actions 中手动指定版本与 `versionCode`。

普通 `vX.Y.Z` Release 也会通过多架构发布工作流附带同版本 Magisk ZIP；`magisk-vX.Y.Z` 保留为只发布模块的独立通道。

本地构建：

```bash
VERSION=0.2.17 VERSION_CODE=2017 bash ./scripts/build-magisk.sh arm64
```

安装、升级、数据目录和局域网监听见 [Magisk 模块文档](magisk.md)。

## 多架构原生二进制

工作流：`.github/workflows/release.yml`

支持目标：

| 系统 | 架构 |
| --- | --- |
| Linux | amd64、arm64、armv7 |
| Windows | amd64、arm64 |
| macOS | amd64、arm64 |

每个目标同时生成：

- 内嵌 Web 控制台、可直接运行的独立二进制；
- 包含二进制、`resource/`、`.env.example` 和 README 的完整归档；
- 汇总所有产物 SHA256 的 `checksums.txt`。

独立二进制未显式指定 `-resource-root` 时，会把内嵌页面恢复到 `resource/.web-assets/v<版本>`。替换程序后使用新的版本目录，不会继续加载上一版本的页面；数据库、头像和二维码仍保留在原数据目录。

v0.2.25 起，Windows、Linux、macOS 原生程序会自动加载程序旁的 `.env`（没有才查当前工作目录）。完整包内的 `.env.example` 可复制后使用，独立二进制用户也可自行创建；支持 `-env-file` 指定文件。进程环境和显式启动参数优先，详细规则见[配置文档](configuration.md#配置方式)。

推送 `vX.Y.Z` 标签会构建并发布对应 Release。正式 Release 固定包含 7 个独立二进制、7 个完整归档、1 个 Magisk ARM64 ZIP 和 1 个 `checksums.txt`，共 16 个资产。Pull Request 和相关 `main` 提交只做测试、交叉编译与打包校验，不上传临时产物。手动工作流可选择是否发布 Release。

## 本地构建

需要 Go 1.23+、Bash、`tar`、`zip` 和 SHA256 工具：

```bash
# 构建全部平台
bash ./scripts/build-release.sh

# 只构建一个目标
bash ./scripts/build-release.sh linux-amd64
```

输出目录默认为 `dist/`，可通过 `OUT_DIR` 修改：

```bash
OUT_DIR=/tmp/yyb-release bash ./scripts/build-release.sh windows-amd64
```

脚本在构建前清理目标目录中的旧 YYB 产物，防止校验文件混入历史文件。

## 发布流程

推荐顺序：

1. 运行测试并确认工作区只包含本次改动。
2. 更新 `VERSION` 与 `CHANGELOG.md`。
3. 提交并推送 `main`，观察 Docker、Magisk 和原生构建验证。
4. 创建 `vX.Y.Z` 标签发布 Docker、原生二进制和同版本 Magisk 模块。
5. 仅需单独发布 Magisk 时，再创建 `magisk-vX.Y.Z` 标签。
6. 下载 Release 产物并核对 `checksums.txt`。

示例：

```bash
git tag v0.2.17
git push origin v0.2.17

git tag magisk-v0.2.17
git push origin magisk-v0.2.17
```

Docker 与 Magisk 是独立工作流，其中一条失败不会阻塞另一条。文档或脚本提交没有触发镜像构建时属于预期行为。

### Release 更新说明格式

普通版本发布前，`CHANGELOG.md` 必须存在对应章节：

```markdown
## v0.2.19 - 2026-09-30

- 第一项用户可感知的变化。
- 第二项修复或兼容性说明。
```

独立 Magisk 版本可使用 `## Magisk v0.1.5 - 2026-09-30`；若 Magisk 与主程序使用相同版本，也可复用 `## v0.2.19`。找不到对应章节或章节为空时，发布任务会直接失败并指出应补充的标题，避免生成没有内容的 Release。

对已有标签重新运行发布工作流时，会同时更新 Release 标题、正文和附件。可在本地预览正文：

```bash
python3 tools/release_notes.py \
  --version 0.2.18 \
  --tag v0.2.18 \
  --kind full \
  --repository 525815266/YYB-Go-Enhanced \
  --output /tmp/release-notes.md
```

## 版本与回滚

- `VERSION` 使用不带 `v` 的 SemVer，例如 `0.2.17` 或 `0.2.17-rc.1`。
- 发布标签使用 `v0.2.17`；Magisk 独立标签使用 `magisk-v0.2.17`。
- 容器回滚只能恢复程序镜像，不能恢复数据库。
- 升级前备份 SQLite 或 MySQL，并保留上一版本镜像和 Compose 配置。
- 管理员在线更新能力及宿主机执行器见[系统维护](maintenance.md)。
