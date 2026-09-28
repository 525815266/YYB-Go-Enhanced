#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: mlgogo_sign
"""马历/马历赛事小程序每日积分签到，支持 YYB Go 多账号。

青龙变量：
  YYB_SERVER       每行一个“YYB地址@账号ID或OpenID”
  MLGOGO_INVITER_USER_ID  可选，首次登录需要邀请码时填写
  MLGOGO_TOKEN_FILE       可选，默认 scripts/token_caches/mlgogo_sign_cache.json

依赖：requests、pycryptodome（缺少时：pip install requests pycryptodome）
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15


APP_ID = "wx86d2d7c2d832b4ce"
BASE_URL = "https://mlxcx.mlgogo.com"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.50 NetType/WIFI MiniProgramEnv/IOS"
)

# 来自小程序 utils/sign.js，服务端只接受这把应用内置 RSA 私钥对应的签名。
SIGNING_KEY = """-----BEGIN PRIVATE KEY-----
MIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEAuQS+G972eBxCyS30
aEKdZ/pcVqUZky/fpl8+7DOABHtWY+CmXXrBpEzGsFIiMnDzLhOd4/Ds6gTu5vvX
jNfR7wIDAQABAkEAkwWvxBoDJSLf91nrM8ZrqqqKIdgEYK/UOzLIn421FtlJjRmz
YE8fU+u4txzPf4SQ0nRzmbpE4jJwzu61maskkQIhAOF3EwLa/5dcz9WphcUcBO+R
PesSglgFvt29E2I8qt5rAiEA0hNgJNyIyxOcxcEioZdZxnJJun7bzjdhxFqgAgxE
M40CIFKXMuCd5njE5+FFyxnMTMaRNtRQoGysFiHV7C7VOGZnAiBDxDZOjcme4NvA
uzXFtMIkDvgTrhqP4jOqmKVnI7fYfQIhALDbkZJBbJZx4xYF+F2BOQXLQEQPD02J
436WmLWq+aLn
-----END PRIVATE KEY-----"""


class ScriptError(RuntimeError):
    pass


def now_shanghai() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))


def make_signature(y_value: str, time_value: str) -> str:
    """实现小程序 utils/sign.js: SHA256withRSA('@' + y + '&' + t + '%')."""
    message = f"@{y_value}&{time_value}%".encode("utf-8")
    key = RSA.import_key(SIGNING_KEY)
    return base64.b64encode(pkcs1_15.new(key).sign(SHA256.new(message))).decode("ascii")


def signed_headers(token: str = "") -> Tuple[Dict[str, str], str]:
    y_value = f"{random.random():.10f}"
    time_value = now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{APP_ID}/312/page-frame.html",
        "Content-Type": "application/json",
        "y": y_value,
        "t": time_value,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers, make_signature(y_value, time_value)


def parse_servers(raw: str) -> List[Tuple[str, str]]:
    result: List[Tuple[str, str]] = []
    for line in raw.splitlines():
        value = line.strip()
        if not value or value == "[object Object]":
            continue
        if "@" not in value:
            print(f"跳过无效 YYB_SERVER：{value}")
            continue
        server, ref = value.split("@", 1)
        server = server.strip().rstrip("/")
        ref = ref.strip()
        if not server or not ref:
            continue
        if not re.match(r"^https?://", server, re.I):
            server = f"http://{server}"
        result.append((server, ref))
    return result


def cache_path() -> Path:
    configured = os.getenv("MLGOGO_TOKEN_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent / "token_caches" / "mlgogo_sign_cache.json"


def read_cache() -> Dict[str, Any]:
    try:
        path = cache_path()
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
    except Exception:
        pass
    return {}


def write_cache(value: Dict[str, Any]) -> None:
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except Exception as exc:
        print(f"写入 token 缓存失败：{exc}")


def short(value: Any) -> str:
    text = str(value or "")
    return text if len(text) <= 12 else f"{text[:6]}***{text[-4:]}"


def json_response(response: requests.Response) -> Dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise ScriptError(f"HTTP {response.status_code} 返回非 JSON：{response.text[:200]}") from exc
    if not isinstance(value, dict):
        raise ScriptError(f"HTTP {response.status_code} 返回格式异常：{str(value)[:200]}")
    return value


class MlgogoClient:
    def __init__(self, server: str, ref: str, cache: Dict[str, Any]) -> None:
        self.server = server
        self.ref = ref
        self.cache = cache
        self.token = str(cache.get("token") or "")
        self.user_id = str(cache.get("userId") or "")
        self.session = requests.Session()

    def get_code(self) -> str:
        response = requests.post(
            f"{self.server}/wxapp/getCode",
            json={"ref": self.ref, "app_id": APP_ID},
            timeout=20,
        )
        data = json_response(response)
        result = data.get("data") or {}
        if isinstance(result, dict):
            result = result.get("result") or result
        code = result.get("code") if isinstance(result, dict) else None
        if data.get("code") != 0 or not code:
            raise ScriptError(f"获取微信 code 失败：{str(data)[:300]}")
        print("YYB 获取 code 成功")
        return str(code)

    def login(self) -> None:
        payload: Dict[str, Any] = {"code": self.get_code()}
        inviter = os.getenv("MLGOGO_INVITER_USER_ID", "").strip()
        if inviter:
            payload["inviterUserId"] = inviter
        headers, signature = signed_headers()
        payload["_s"] = signature
        response = self.session.post(f"{BASE_URL}/mp/login", json=payload, headers=headers, timeout=30)
        data = json_response(response)
        info = data.get("data")
        if response.status_code != 200 or data.get("code") != 0 or not isinstance(info, dict):
            raise ScriptError(f"马历登录失败 HTTP {response.status_code}：{str(data)[:400]}")
        self.token = str(info.get("token") or "")
        self.user_id = str(info.get("userId") or "")
        if not self.token:
            raise ScriptError("马历登录成功但未返回 token")
        self.cache.update(
            {
                "token": self.token,
                "userId": self.user_id,
                "openid": info.get("openid"),
                "userName": info.get("userName"),
                "updatedAt": int(time.time()),
            }
        )
        print(f"马历登录成功：{info.get('userName') or self.user_id or '未知用户'}")

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers, signature = signed_headers(self.token)
        data = dict(payload or {})
        params: Optional[Dict[str, Any]] = None
        if method.upper() == "GET":
            params = {"_s": signature}
        else:
            data["_s"] = signature
        response = self.session.request(
            method.upper(),
            f"{BASE_URL}{path}",
            headers=headers,
            params=params,
            json=None if method.upper() == "GET" else data,
            timeout=30,
        )
        result = json_response(response)
        if response.status_code in (401, 403) or result.get("code") in (445, 446):
            raise ScriptError("马历 token 已失效")
        return result

    def get_checkin(self) -> Dict[str, Any]:
        result = self.request("GET", "/mp/points/mall/checkin")
        if result.get("code") != 0:
            raise ScriptError(str(result.get("message") or result)[:300])
        return result.get("data") or {}

    def do_checkin(self) -> Dict[str, Any]:
        result = self.request("POST", "/mp/points/mall/checkin", {})
        if result.get("code") != 0:
            raise ScriptError(str(result.get("message") or result)[:300])
        return result.get("data") or {}


def run_account(server: str, ref: str, cache_all: Dict[str, Any]) -> str:
    account_cache = cache_all.get(ref)
    if not isinstance(account_cache, dict):
        account_cache = {}
    client = MlgogoClient(server, ref, account_cache)
    try:
        if not client.token:
            client.login()
        try:
            before = client.get_checkin()
        except ScriptError as exc:
            if "token 已失效" not in str(exc):
                raise
            print("缓存 token 已失效，重新登录")
            client.token = ""
            client.login()
            before = client.get_checkin()
        cache_all[ref] = client.cache
        if before.get("signedToday") is True:
            account = before.get("account") or {}
            return f"今日已签到，连续 {before.get('consecutiveDays', 0)} 天，当前可用积分 {account.get('availablePoints', '-')}"
        result = client.do_checkin()
        after = result.get("account") or {}
        record = result.get("record") or {}
        return (
            f"签到成功，获得 {result.get('points', record.get('totalPoints', 0))} 积分，"
            f"连续 {result.get('consecutiveDays', 0)} 天，当前可用积分 {after.get('availablePoints', '-')}"
        )
    finally:
        cache_all[ref] = client.cache


def main() -> None:
    accounts = parse_servers(os.getenv("YYB_SERVER", ""))
    if not accounts:
        print("未配置 YYB_SERVER，格式：yyb-go:8000@账号ID或OpenID，每行一个")
        return
    cache_all = read_cache()
    print(f"共读取 {len(accounts)} 个 YYB 账号")
    for index, (server, ref) in enumerate(accounts, 1):
        print(f"\n================ 账号 {short(ref)} ================")
        try:
            print(run_account(server, ref, cache_all))
        except Exception as exc:
            print(f"账号 {short(ref)} 执行失败：{exc}")
        write_cache(cache_all)
        if index < len(accounts):
            time.sleep(max(0, int(os.getenv("MLGOGO_ACCOUNT_INTERVAL", "2"))))


if __name__ == "__main__":
    main()
