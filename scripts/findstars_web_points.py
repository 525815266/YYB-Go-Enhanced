#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: findstars_web_points
"""寻星电玩网页积分任务。

网页授权完成后，网站会给浏览器一个 FS-Token。该令牌不是 YYB 小程序
openid，也不能由 YYB 的 wx.login code 直接换出；请把令牌放入青龙变量：

FINDSTARS_TOKENS=Boom#<FS-Token>
另一个账号#<FS-Token>

也兼容单账号 FINDSTARS_TOKEN / FINDSTARS_FS_TOKEN。脚本只领取服务端
返回 status=complete 的任务，不伪造网页授权或积分结果。
"""

import hashlib
import os
import random
import string
import sys
import time
from typing import Any, Dict, List, Tuple

import requests


BASE_URL = os.getenv("FINDSTARS_API", "https://fsapi.ihezu.com").rstrip("/")
API_KEY = "L$PRBtj3@4r9^cNd"  # 网站前端公开的请求签名常量，不是用户令牌
ORIGIN = os.getenv("FINDSTARS_ORIGIN", "https://www.findstars.cn")
USER_AGENT = os.getenv(
    "FINDSTARS_UA",
    "Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36 "
    "Chrome/131.0 Mobile Safari/537.36",
)


def _nonce(length: int = 10) -> str:
    alphabet = string.digits + string.ascii_uppercase + string.ascii_lowercase
    return "".join(random.choice(alphabet) for _ in range(length))


def _headers(token: str) -> Dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = _nonce()
    sign_text = f"api_key={API_KEY}&FS-TS={timestamp}&FS-Nonce={nonce}"
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": USER_AGENT,
        "FS-TS": timestamp,
        "FS-Nonce": nonce,
        "FS-Sign": hashlib.md5(sign_text.encode()).hexdigest(),
        "FS-Token": token,
    }


def _load_tokens() -> List[Tuple[str, str]]:
    raw = os.getenv("FINDSTARS_TOKENS", "").strip()
    if not raw:
        raw = os.getenv("FINDSTARS_TOKEN", os.getenv("FINDSTARS_FS_TOKEN", "")).strip()
    # 青龙会把同名环境变量拼接成 ampersand 分隔的字符串；同时保留
    # 手工配置多行文本的兼容性。令牌本身不使用 ampersand。
    lines: List[str] = []
    for raw_line in raw.splitlines():
        lines.extend(part for part in raw_line.split("&") if part.strip())

    result: List[Tuple[str, str]] = []
    for index, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        label, token = f"账号 {index}", line
        if "#" in line:
            label, token = (part.strip() for part in line.split("#", 1))
        elif "|" in line:
            label, token = (part.strip() for part in line.split("|", 1))
        if token:
            result.append((label or f"账号 {index}", token))
    if not result:
        raise RuntimeError("未配置 FINDSTARS_TOKENS，请填写网页授权后获取的 FS-Token")
    return result


class FindStarsClient:
    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()

    def post(self, path: str, payload: Any = None) -> Any:
        response = self.session.post(
            BASE_URL + path,
            json={} if payload is None else payload,
            headers=_headers(self.token),
            timeout=20,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"HTTP {response.status_code}: 非 JSON 响应") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {body}")
        if body.get("code") in (400010, 400011):
            raise RuntimeError("FS-Token 已失效，请重新网页登录授权")
        if body.get("status") != "success" or body.get("code") != 200:
            raise RuntimeError(body.get("message") or f"接口失败: {body}")
        return body.get("data")

    def user(self) -> Dict[str, Any]:
        return self.post("/api/user/show") or {}

    def tasks(self) -> List[Dict[str, Any]]:
        data = self.post("/api/activeTask/index")
        return data if isinstance(data, list) else []

    def receive(self, task_id: Any) -> None:
        self.post("/api/activeTask/receive", {"activeTaskId": task_id})


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def run_one(label: str, token: str) -> str:
    client = FindStarsClient(token)
    before = client.user()
    start_coins = _number(before.get("coins"))
    tasks = client.tasks()
    claimable = [task for task in tasks if str(task.get("status", "")).lower() == "complete"]
    claimed: List[str] = []
    for task in claimable:
        task_name = str(task.get("name") or task.get("id"))
        if os.getenv("FINDSTARS_DRY_RUN", "0") == "1":
            claimed.append(f"{task_name}(dry-run)")
            continue
        try:
            client.receive(task.get("id"))
            claimed.append(task_name)
        except Exception as exc:
            claimed.append(f"{task_name}(失败: {exc})")
    after = client.user()
    end_coins = _number(after.get("coins"))
    delta = end_coins - start_coins
    task_text = "、".join(claimed) if claimed else "无可领取任务"
    return f"{label}：积分 {start_coins:g} → {end_coins:g}（变化 {delta:+g}）；领取：{task_text}"


def main() -> None:
    try:
        targets = _load_tokens()
    except Exception as exc:
        print(f"配置错误：{exc}")
        return
    results = []
    for label, token in targets:
        try:
            results.append(run_one(label, token))
        except Exception as exc:
            results.append(f"{label}：执行失败：{exc}")
    print("\n".join(results))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
