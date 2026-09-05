#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: YYB账号状态检查
# cron: 17 */12 * * *

"""维护 YYB 账号公共缓存。

该任务只读取 YYB 的账号列表，不调用任何业务小程序接口，也不会制造
未消费的 wx.login code。业务脚本把明确的未授权/未注册响应写入同一缓存。
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from urllib.parse import urlsplit

import requests

from yyb_account_guard import account_ref, get_status, prune, set_status, status_file


def parse_server_lines() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw in os.getenv("YYB_SERVER", "").splitlines():
        value = raw.strip()
        if not value or "@" not in value:
            continue
        server, ref = value.rsplit("@", 1)
        server = server.strip()
        ref = account_ref(ref)
        if not server or not ref:
            continue
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        result.append((server.rstrip("/"), ref))
    return result


def check_health(server: str) -> None:
    """/accounts 受 YYB 登录保护，公共任务只做健康探测。"""
    response = requests.get(server + "/healthz", timeout=15)
    response.raise_for_status()


def main() -> int:
    entries = parse_server_lines()
    if not entries:
        print("未配置有效 YYB_SERVER，跳过状态检查")
        return 0
    refs = [ref for _, ref in entries]
    summary = Counter()
    checked_servers: set[str] = set()
    for server, ref in entries:
        try:
            if server not in checked_servers:
                check_health(server)
                checked_servers.add(server)
            summary["health_ok"] += 1
        except (requests.RequestException, ValueError, TypeError, RuntimeError) as exc:
            print(f"{ref} 检查失败（临时错误）：{exc}")
            summary["temporary_error"] += 1
    result = prune(refs=refs)
    print(f"公共缓存：{status_file()}，账号 {len(refs)}，状态 {dict(summary)}，清理 {len(result['removed'])} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
