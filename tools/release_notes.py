#!/usr/bin/env python3
"""Generate a useful GitHub Release body from the matching changelog section."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")


def _heading_matches(title: str, label: str) -> bool:
    return re.match(rf"^{re.escape(label)}(?:\s+-\s+.*)?$", title, re.IGNORECASE) is not None


def extract_version_notes(changelog: str, version: str, kind: str) -> str:
    labels = [f"v{version}"]
    if kind == "magisk":
        labels = [f"Magisk v{version}", f"magisk-v{version}", *labels]

    lines = changelog.splitlines()
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if not match or not any(_heading_matches(match.group(1), label) for label in labels):
            continue

        section: list[str] = []
        for candidate in lines[index + 1 :]:
            if HEADING_RE.match(candidate):
                break
            section.append(candidate)
        body = "\n".join(section).strip()
        if body:
            return body
        raise ValueError(f"CHANGELOG.md 中的 v{version} 章节没有更新内容")

    if kind == "magisk":
        magisk_labels = (f"magisk v{version}".lower(), f"magisk-v{version}".lower())
        matching_bullets = [
            line.strip()
            for line in lines
            if line.lstrip().startswith("-")
            and any(label in line.lower() for label in magisk_labels)
        ]
        if matching_bullets:
            return "\n".join(matching_bullets)

    expected = f"## v{version} - YYYY-MM-DD"
    if kind == "magisk":
        expected += f" 或 ## Magisk v{version} - YYYY-MM-DD"
    raise ValueError(f"CHANGELOG.md 缺少发布章节，请先添加：{expected}")


def render_release_notes(
    *, version: str, tag: str, kind: str, repository: str, changes: str
) -> str:
    base = f"https://github.com/{repository}/blob/{tag}"
    title = f"Magisk v{version}" if kind == "magisk" else f"YYB Go Enhanced v{version}"

    if kind == "magisk":
        downloads = f"""## 下载与安装

- 下载 `yyb-go-magisk-arm64-{version}.zip`，在 Magisk 管理器中选择“从本地安装”。
- 当前模块仅支持 Android ARM64；其他平台请使用普通版本 Release。
- 升级前建议备份模块配置与 YYB 数据，安装后确认服务和控制台可以正常打开。"""
        upgrade = """## 升级提示

- 升级前备份模块配置与 YYB 数据目录。
- 安装完成后重启设备，并确认 Magisk 模块状态、YYB 服务和控制台均正常。
- 如遇兼容问题，请携带版本号、Android 版本、Magisk 版本和模块日志提交 Issue。"""
    else:
        image = f"ghcr.io/{repository.lower()}:{version}"
        downloads = f"""## 下载与安装

- Docker：使用 `{image}`，或继续使用 `latest`。
- Windows、Linux、macOS：首次安装优先下载 `yyb-go-v{version}-<平台>-<架构>` 完整压缩包；已有资源目录时可只替换对应的 `yyb-go-<平台>-<架构>` 独立程序。
- Magisk：Android ARM64 下载 `yyb-go-magisk-arm64-{version}.zip`。
- 完成下载后使用 `checksums.txt` 校验文件完整性。"""
        upgrade = """## 升级提示

- 升级前备份 SQLite/MySQL 数据库、环境变量和面板连接配置。
- Docker 用户更新镜像后需要重新创建容器，持久化数据目录不要删除。
- 如遇兼容问题，请携带版本号、运行方式和完整日志提交 Issue。"""

    return f"""# {title}

## 本次更新

{changes}

{downloads}

{upgrade}

## 相关文档

- [完整更新记录]({base}/CHANGELOG.md)
- [构建与发布说明]({base}/docs/release-builds.md)
- [部署说明]({base}/README.md)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--kind", choices=("full", "magisk"), required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if not VERSION_RE.fullmatch(args.version):
        parser.error(f"版本号不是 SemVer：{args.version}")

    changelog = Path(args.changelog).read_text(encoding="utf-8")
    try:
        changes = extract_version_notes(changelog, args.version, args.kind)
    except ValueError as error:
        parser.error(str(error))

    notes = render_release_notes(
        version=args.version,
        tag=args.tag,
        kind=args.kind,
        repository=args.repository,
        changes=changes,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(notes, encoding="utf-8", newline="\n")
    print(f">> Release 更新说明已生成：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
