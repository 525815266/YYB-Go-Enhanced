#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: YYB账号状态检查
# cron: 17 */12 * * *

"""维护 YYB 账号公共缓存。

该任务只探测 YYB 服务健康状态，不代表各账号登录仍有效。不调用业务小程序接口，也不会制造
未消费的 wx.login code。业务脚本把明确的未授权/未注册响应写入同一缓存。
"""

from __future__ import annotations

import os
import sys
from collections import Counter

import requests

from yyb_account_guard import account_ref, prune, status_file


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
    response = requests.get(server + "/health", timeout=15)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") != 0 or not isinstance(body.get("data"), dict) or body["data"].get("ok") is not True:
        raise RuntimeError("健康检查未返回 YYB 服务状态，请检查服务地址或反向代理")


def main() -> int:
    entries = parse_server_lines()
    if not entries:
        print("未配置有效 YYB_SERVER，跳过状态检查")
        return 0
    refs = [ref for _, ref in entries]
    summary = Counter()
    checked_servers: dict[str, bool] = {}
    for server, ref in entries:
        if server not in checked_servers:
            try:
                check_health(server)
                checked_servers[server] = True
            except (requests.RequestException, ValueError, TypeError, RuntimeError) as exc:
                checked_servers[server] = False
                print(f"{server} 服务检查失败（临时错误，同地址只检查一次）：{exc}")
        summary["service_reachable" if checked_servers[server] else "temporary_error"] += 1
    result = prune(refs=refs)
    print(f"公共缓存：{status_file()}，账号 {len(refs)}，状态 {dict(summary)}，清理 {len(result['removed'])} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
