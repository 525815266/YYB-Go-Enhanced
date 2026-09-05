#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
小牛电动小程序动态 code 版

功能：
  1. YYB Go 多账号或旧版多端口服务获取微信 code
  2. oauth2/token(grant_type=mini_code) 使用 code 换 token
  3. 每日分享(文章/片刻)，积分到账自动换帖
  4. 验证任务状态与积分流水
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER        YYB Go 地址@账号ID或OpenID，每行一个；配置后支持备注/昵称
  NIU_SERVERS       旧版 code 服务地址，每行一个；仅在未配置 YYB_SERVER 时使用
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  NIU_TOKEN         已有 token，可选，配置后跳过 code 登录
  NIU_AUTH_API      code 换 token 接口，默认小牛小程序 oauth2/token
  NIU_MAX_SHARE     每日分享上限，默认 2
  NIU_STATE_DIR     状态文件目录，默认脚本目录

说明：
  - 登录链路(小程序源码逆向): check_auth 用 wx code 换 open_id, 再调
    oauth2/token(grant_type=new_mini_code + open_id) 换 token, form 表单编码
  - 手机号登录(grant_type=mini_code + mini_code 手机组件码)需真实手机号授权,
    本地 code 服务只能出 wx code, 故采用 open_id 登录链路
  - code 换出的 token 实测可直接调用 app-api.niu.com 社区接口(token 头)，
    积分接口的 store token 通过 store.niu.com/api/auth/login 换取(x-token 头)
  - 小程序业务接口(app-api-miniapp-v6)响应为加密数据无法直接解析，故任务沿用原 App 接口
  - check_auth 仅做授权校验返回 open_id，登录以 oauth2/token 换 token 为准
  - 按服务端当日「每日分享」积分是否到账判定分享是否有效，自动换帖
  - 每账号独立记录已分享帖子，避免重复分享

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import base64
import hashlib
import json
import os
import random
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from yyb_account_guard import filter_accounts, update_from_result

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


APP_NAME = "小牛电动小程序"
APPID = "wx496829086cb0a118"
NIU_APP_ID = "niu_89cyuop8"

SERVERS = [
    "127.0.0.1:8088",
    "192.168.31.36:8088",
    "192.168.31.88:8088",
    "192.168.31.62:8088",
]


@dataclass
class AccountTarget:
    server: str
    ref: str = ""
    remark: str = ""
    nickname: str = ""
    index: int = 0

    @property
    def label(self) -> str:
        display = self.remark or self.nickname
        if display and self.ref:
            number = self.ref if self.ref.isdigit() else str(self.index or "?")
            return f"{display}（账号 {number}）"
        if self.ref:
            return f"账号 {self.ref if self.ref.isdigit() else (self.index or '?')}"
        return display or self.server


def normalize_server(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value.rstrip("/")


def load_account_targets() -> List[AccountTarget]:
    raw = os.getenv("YYB_SERVER", "").strip()
    if not raw:
        legacy = os.getenv("NIU_SERVERS", "").strip()
        servers = [line.strip() for line in legacy.splitlines() if line.strip()] if legacy else SERVERS
        return [AccountTarget(server=server, index=index) for index, server in enumerate(servers, 1)]

    accounts: List[AccountTarget] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "@" not in line:
            print(f"⚠️ [配置] YYB_SERVER 忽略无账号标识行：{line}")
            continue
        server, ref = (part.strip() for part in line.split("@", 1))
        if server and ref:
            accounts.append(AccountTarget(normalize_server(server), ref=ref, index=len(accounts) + 1))
    if not accounts:
        raise RuntimeError("YYB_SERVER 未读取到有效账号，格式：yyb-go:8000@账号ID或OpenID")
    load_account_labels(accounts)
    return accounts


def load_account_labels(accounts: List[AccountTarget]) -> None:
    grouped: Dict[str, List[AccountTarget]] = {}
    for account in accounts:
        grouped.setdefault(account.server, []).append(account)
    for server, rows in grouped.items():
        try:
            response = direct_session().get(server + "/accounts", timeout=10)
            payload = response.json()
            items = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(items, dict):
                items = items.get("accounts") or items.get("items") or []
            if not response.ok or not isinstance(items, list):
                continue
        except (requests.RequestException, ValueError):
            continue
        for account in rows:
            for item in items:
                if not isinstance(item, dict):
                    continue
                identifiers = {str(item.get("id") or ""), str(item.get("openid") or ""), str(item.get("uin") or "")}
                if account.ref in identifiers:
                    apply_account_label(account, item)
                    break


def apply_account_label(account: AccountTarget, item: Any) -> None:
    if not isinstance(item, dict):
        return
    account.remark = str(item.get("remark") or item.get("alias") or account.remark).strip()
    account.nickname = str(item.get("nickname") or account.nickname).strip()


CURRENT_ACCOUNTS: List[AccountTarget] = []

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

NIU_TOKEN = os.getenv("NIU_TOKEN", "").strip()
NIU_MAX_SHARE = int(os.getenv("NIU_MAX_SHARE", "2"))
DEFAULT_STATE_DIR = os.path.dirname(os.path.abspath(__file__))
NIU_STATE_DIR = os.getenv("NIU_STATE_DIR", DEFAULT_STATE_DIR)

AUTH_API = os.getenv(
    "NIU_AUTH_API",
    "https://account-miniapp.niucache.com/v3/api/oauth2/token",
)
CHECK_AUTH_API = os.getenv(
    "NIU_CHECK_AUTH_API",
    "https://account-miniapp.niucache.com/v3/api/auth/wx-mini/check_auth",
)
APP_API = os.getenv("NIU_APP_API", "https://app-api.niu.com")
STORE_API = os.getenv("NIU_STORE_API", "https://store.niu.com")

STORE_LOGIN_URL = f"{STORE_API}/api/auth/login"
RECOMMEND_URL = f"{APP_API}/community/api/posts/recommend/list"
SHARE_URL = f"{APP_API}/community/api/posts/shares"
DETAIL_URL = f"{APP_API}/community/api/posts/detail"
COMMENTS_URL = f"{APP_API}/community/api/posts/comments/list"
TASK_URL = f"{STORE_API}/api/integral/task"
INTEGRAL_LIST_URL = f"{STORE_API}/api/integral/list"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF "
    "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541b37) XWEB/20089"
)

# 服务器按北京时间(UTC+8)结算每日任务
SHANGHAI_OFFSET = 8 * 3600


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def mask(value: Any) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def json_preview(data: Any, limit: int = 800) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        return str(data)[:limit]


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🛵 小牛电动小程序动态 code 版                  ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(CURRENT_ACCOUNTS):<34}║")
    print("╚" + "═" * 50 + "╝")


def log_account_header(index: int, total: int, account: AccountTarget) -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 👤 账号 {account.label:<38}│")
    print(f"│ 🌍 来源 {account.server:<40}│")
    print("└" + "─" * 50 + "┘")


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def parse_proxy_response(text: Any) -> Dict[str, Any] | None:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)

    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        proxy_obj = None

        if isinstance(data.get("data"), list) and data["data"]:
            proxy_obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            proxy_obj = data["data"]
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif isinstance(data.get("result"), dict):
            proxy_obj = data["result"]

        if proxy_obj:
            host = proxy_obj.get("ip") or proxy_obj.get("host")
            port = proxy_obj.get("port")
            if host and port:
                return {
                    "host": str(host),
                    "port": int(port),
                    "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                    "password": proxy_obj.get("pass") or proxy_obj.get("password") or "",
                }
    except Exception:
        pass

    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            return {
                "host": parts[0],
                "port": int(parts[1]),
                "username": parts[2] if len(parts) > 2 else "",
                "password": parts[3] if len(parts) > 3 else "",
            }

    return None


def build_proxy_dict(proxy_info: Dict[str, Any] | None) -> Dict[str, str] | None:
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info.get("username", "")
    password = proxy_info.get("password", "")

    auth = ""
    if username and password:
        auth = f"{quote(username)}:{quote(password)}@"

    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"

    print(f"🛠️ [代理] 生成 {scheme.upper()} 代理 {host}:{port}")

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def validate_proxy(proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    if not proxies:
        return False, ""

    try:
        response = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if response.status_code == 200:
            try:
                ip = response.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            print(f"✅ [代理] 验证通过，出口 IP: {ip}")
            return True, ip
    except Exception as exc:
        print(f"⚠️ [代理] 验证失败: {exc}")

    return False, ""


def get_valid_proxy(account_name: str) -> Tuple[Dict[str, str] | None, str]:
    if not PROXY_API:
        print(f"⚠️ [代理] {account_name} 未配置 PROXY_API，使用直连")
        return None, ""

    print(f"🌐 [代理] {account_name} 正在获取品赞代理...")

    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                print(f"⚠️ [代理] 第 {index} 次代理解析失败")
                continue

            print(f"✅ [代理] 提取到 {proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)

            ok, ip = validate_proxy(proxies)
            if ok:
                return proxies, ip

            print(f"⚠️ [代理] 第 {index} 次代理不可用")
        except Exception as exc:
            print(f"⚠️ [代理] 第 {index} 次获取代理异常: {exc}")

        if index < PROXY_RETRY_TIMES:
            sleep(2)

    print("⚠️ [代理] 获取失败，使用直连")
    return None, ""


def request_with_proxy(
    method: str,
    url: str,
    *,
    proxies: Dict[str, str] | None = None,
    server: str = "",
    **kwargs,
) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            print(f"⚠️ [代理] {server} 代理请求失败: {exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            print("🔁 [兜底] 切换直连重试")

    session = direct_session()
    return session.request(method, url, **kwargs)


def send_pushplus(title: str, content: str) -> None:
    if not PLUSPLUS_TOKEN:
        print("⚠️ [PushPlus] 未配置 PLUSPLUS_TOKEN，跳过推送")
        return

    try:
        requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": PLUSPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": "txt",
            },
            timeout=10,
        )
        print("✅ [PushPlus] 推送成功")
    except Exception as exc:
        print(f"❌ [PushPlus] 推送失败: {exc}")


def get_code(account: AccountTarget) -> str | None:
    server = account.server
    if account.ref:
        url = server.rstrip("/") + "/wxapp/getCode"
        method = "POST"
        request_kwargs = {"json": {"ref": account.ref, "app_id": APPID}}
    else:
        url = f"http://{server}/login"
        method = "GET"
        request_kwargs = {"params": {"appId": APPID}}
    print(f"🔐 [授权] 请求本地 code 服务: {url}")

    for attempt in range(1, 3):
        try:
            response = direct_session().request(method, url, timeout=20, **request_kwargs)
            data = response.json()
            if account.ref:
                nested = data.get("data") if isinstance(data, dict) else None
                if isinstance(nested, dict):
                    apply_account_label(account, nested.get("account"))
                    nested = nested.get("result") or nested.get("data") or nested
                code = str((nested.get("code") if isinstance(nested, dict) else None) or (data.get("code") if isinstance(data, dict) else "") or "").strip()
                valid = response.ok and bool(code) and code.lower() != "null"
            else:
                code = str(data.get("code") or "").strip()
                valid = data.get("err") == 0 and bool(code) and code.lower() != "null"
            if valid:
                print("✅ [授权] code 获取成功")
                return code
            print(f"❌ [授权] 第 {attempt} 次 code 获取失败: {json_preview(data)}")
        except Exception as exc:
            print(f"❌ [授权] 第 {attempt} 次 code 获取异常: {exc}")
        if attempt < 2:
            sleep(3)
    return None


def common_headers(token: str | None = None, token_header: str = "token") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/77/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers[token_header] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
        data.get("jwt"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("jwt"),
        ])

        token_obj = inner.get("token")
        if isinstance(token_obj, dict):
            candidates.extend([
                token_obj.get("access_token"),
                token_obj.get("accessToken"),
                token_obj.get("jwt"),
                token_obj.get("token"),
            ])

        user = inner.get("user")
        if isinstance(user, dict):
            candidates.extend([
                user.get("token"),
                user.get("accessToken"),
                user.get("access_token"),
                user.get("jwt"),
            ])

    for item in candidates:
        if isinstance(item, str) and item and item != "null":
            return item
        if isinstance(item, dict):
            for key in ("access_token", "accessToken", "jwt", "token"):
                value = item.get(key)
                if isinstance(value, str) and value and value != "null":
                    return value

    return None


def login_headers() -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/77/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    return headers


def check_auth(server: str, code: str, proxies: Dict[str, str] | None) -> str | None:
    """check_auth 用 wx code 换取 open_id(小程序真实授权流程)"""
    print("🔐 [授权] check_auth 用 code 换取 open_id")
    try:
        response = request_with_proxy(
            "POST",
            CHECK_AUTH_API,
            headers=common_headers(),
            json={
                "mp_code": code,
                "app_id": NIU_APP_ID,
                "mini_app_id": APPID,
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        status = data.get("status") if isinstance(data, dict) else None
        desc = data.get("desc") if isinstance(data, dict) else ""
        inner = data.get("data") if isinstance(data, dict) else None
        open_id = inner.get("open_id") if isinstance(inner, dict) else None

        if status == 0 and open_id:
            print(f"✅ [授权] open_id 获取成功: {mask(open_id)}")
            return open_id

        if status == 20021:
            print("⚠️ [授权] 授权频繁被限流，请 10 分钟后重试")
        elif status in (40029, 40163, 40242):
            print("⚠️ [授权] code 不合法或已过期，请确认本地 code 服务有有效 code")
        else:
            print(f"⚠️ [授权] check_auth 失败: {desc or '未知错误'} (status={status})")

        print(f"❌ [授权] 未识别 open_id 字段: {json_preview(data)}")
        return None
    except Exception as exc:
        print(f"❌ [授权] check_auth 请求异常: {exc}")
        return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        open_id = check_auth(server, code, proxies)
        if not open_id:
            return None, {"desc": "check_auth 未返回 open_id"}

        print("🔐 [登录] 使用 open_id 换 token (grant_type=new_mini_code)")
        response = request_with_proxy(
            "POST",
            AUTH_API,
            headers=login_headers(),
            data={
                "scope": "base",
                "open_id": open_id,
                "mini_app_id": APPID,
                "grant_type": "new_mini_code",
                "mini_code": "",
                "mp_code": "",
                "app_id": NIU_APP_ID,
                "account": "",
                "country_code": "",
                "captcha": "",
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        status = data.get("status") if isinstance(data, dict) else None
        desc = data.get("desc") if isinstance(data, dict) else ""

        if status == 20021:
            print("⚠️ [登录] 登录频繁被限流，请 10 分钟后重试")
        elif status == 20052:
            print("⚠️ [登录] 请重新进入微信小程序后重试(20052)")
        elif status in (40029, 40163, 40242):
            print("⚠️ [登录] code 不合法或已过期，请确认本地 code 服务有有效 code")
        else:
            print(f"⚠️ [登录] 登录失败: {desc or '未知错误'} (status={status})")

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None,
            token_header: str = "token") -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token, token_header),
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None,
             payload: Dict[str, Any], token_header: str = "token") -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token, token_header),
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("exp")
    except Exception:
        return None


def jwt_sub(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub")
    except Exception:
        return None


def show_token_status(token: str) -> None:
    exp = jwt_exp(token)
    if exp:
        left = exp - int(time.time())
        if left <= 0:
            print(f"  [Token] 已过期")
        else:
            print(f"  [Token] 有效期至 {time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))} "
                  f"(剩余 {left // 86400} 天)")
    else:
        print(f"  [Token] 非 JWT, 未做过期检查")


def store_login(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """用主 token 换取 store 积分 token, 失败时直接复用主 token"""
    r = api_post(server, STORE_LOGIN_URL, token, proxies, {"access_token": token})
    if r.get("status") in (200, 0):
        store_token = (r.get("data") or {}).get("token")
        if store_token:
            print(f"✅ [登录] store token 获取成功: {mask(store_token)}")
            return store_token
    print(f"⚠️ [登录] store 换 token 失败, x-token 直接使用主 token: {json_preview(r, 200)}")
    return token


def today_cn() -> str:
    # 固定按北京时间(UTC+8)取日期, 避免本机时区已是 UTC+8 时重复加偏移
    return time.strftime("%Y-%m-%d",
                         time.gmtime(time.time() + SHANGHAI_OFFSET))


def state_path_for(server: str, token: str) -> str:
    sub = jwt_sub(token)
    ident = sub[:12] if sub else hashlib.md5(server.encode()).hexdigest()[:12]
    return os.path.join(NIU_STATE_DIR, f"niu_shared_{ident}.json")


def load_state(server: str, token: str) -> Dict[str, Any]:
    path = state_path_for(server, token)
    state = {"shared": {}}  # {post_id: "YYYY-MM-DD"}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("shared"), dict):
                state["shared"] = data["shared"]
        except Exception:
            pass
    state["_path"] = path
    return state


def save_state(state: Dict[str, Any]) -> None:
    path = state.get("_path")
    out = {"shared": state["shared"]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_task_status(server: str, store_token: str, proxies: Dict[str, str] | None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    r = api_get(server, f"{TASK_URL}?type=2&version=v2", store_token, proxies,
                token_header="x-token")
    status = {}
    for t in (r.get("data") or []):
        if "每日分享" in t.get("name", ""):
            status["share"] = t.get("status")
    return status, r


def get_today_share_count(server: str, store_token: str, proxies: Dict[str, str] | None) -> int:
    """查询 store 当日「每日分享」积分到账条数(按北京时间)"""
    today = today_cn()
    count = 0
    for page in range(1, 5):
        r = api_get(server, f"{INTEGRAL_LIST_URL}?page={page}&limit=50",
                    store_token, proxies, token_header="x-token")
        items = r.get("data") or []
        if not items:
            break
        for it in items:
            add_time = str(it.get("add_time") or "")
            if add_time < today:
                return count
            if add_time.startswith(today) and "每日分享" in str(it.get("mark") or ""):
                count += 1
    return count


def fetch_recommend(server: str, token: str, proxies: Dict[str, str] | None,
                    pages: int = 10) -> List[Tuple[int, str]]:
    posts = []
    for page in range(1, pages + 1):
        r = api_get(server, f"{RECOMMEND_URL}?page={page}&page_size=20&version=0",
                    token, proxies)
        items = (r.get("data") or {}).get("items") or []
        if not items:
            break
        for it in items:
            pid = it.get("id")
            if pid:
                title = (it.get("title") or it.get("content") or "")[:40]
                posts.append((pid, title))
    return posts


def share_post(server: str, token: str, proxies: Dict[str, str] | None,
               post_id: int) -> Dict[str, Any]:
    return api_post(server, SHARE_URL, token, proxies, {"id": int(post_id)})


def simulate_browse(server: str, token: str, proxies: Dict[str, str] | None,
                    post_id: int) -> None:
    time.sleep(1)
    try:
        api_get(server, f"{DETAIL_URL}?id={int(post_id)}&version=2", token, proxies)
        time.sleep(1)
        api_get(server, f"{COMMENTS_URL}?hide_hot=1&page=1&page_size=20"
                        f"&parent_id={int(post_id)}&parent_type=1&version=2",
                token, proxies)
    except Exception:
        pass


def pick_new_posts(server: str, token: str, proxies: Dict[str, str] | None,
                   state: Dict[str, Any], limit: int) -> List[int]:
    exclude = set(state["shared"].keys())
    new_posts = []
    for pid, title in fetch_recommend(server, token, proxies):
        if str(pid) not in exclude:
            new_posts.append(pid)
            print(f"  候选帖子: {pid} {title}")
            if len(new_posts) >= limit:
                break
    return new_posts


def record_share(state: Dict[str, Any], post_id: int, today: str) -> None:
    state["shared"][str(post_id)] = today


def wait_share_points(server: str, store_token: str,
                        proxies: Dict[str, str] | None, before: int,
                        max_wait: int = 25) -> int:
    """轮询等待「每日分享」积分到账, 最多 max_wait 秒, 返回最新到账条数"""
    waited = 0
    while waited < max_wait:
        time.sleep(5)
        waited += 5
        count = get_today_share_count(server, store_token, proxies)
        if count > before:
            return count
    return before


def do_daily_share(server: str, token: str, store_token: str,
                   proxies: Dict[str, str] | None, state: Dict[str, Any],
                   post_id: int | None = None, force: bool = False) -> str:
    print("📝 [分享] 每日分享(文章/片刻)")
    today = today_cn()

    done = 0 if force else get_today_share_count(server, store_token, proxies)
    if done >= NIU_MAX_SHARE:
        msg = f"今日「每日分享」积分已到账 {done}/{NIU_MAX_SHARE} 条, 已达标, 跳过"
        print(f"✅ [分享] {msg}")
        return msg

    if post_id is not None:
        targets = [post_id]
    else:
        targets = pick_new_posts(server, token, proxies, state,
                                 NIU_MAX_SHARE - done)

    if not targets:
        msg = "推荐列表中没有未分享过的新帖子, 请检查 token 或稍后重试"
        print(f"⚠️ [分享] {msg}")
        return msg

    shared: List[int] = []
    for pid in targets:
        r = share_post(server, token, proxies, pid)
        ok = (r.get("_error") is None
              and (r.get("code") in (None, 0, 200)
                   or "成功" in str(r.get("msg", "")) + str(r.get("desc", ""))
                   or r.get("data") is not None))
        if not ok:
            print(f"❌ [分享] posts/shares {pid}: {json_preview(r, 300)} "
                  f"(接口失败, 不计入记录, 下轮可重试)")
            time.sleep(1)
            continue

        print(f"📤 [分享] posts/shares {pid}: 提交成功, 等待积分到账确认...")
        simulate_browse(server, token, proxies, pid)
        time.sleep(1)
        new_done = wait_share_points(server, store_token, proxies, done)
        if new_done > done:
            print(f"✅ [分享] [OK] {pid} 积分已到账 (今日 {new_done}/{NIU_MAX_SHARE})")
            done = new_done
        else:
            print(f"⚠️ [分享] [X] {pid} 积分未到账, 该帖此前可能已分享过, 标记后换下一篇")
        # 无论是否到账都记录, 避免下次重复尝试该帖
        record_share(state, pid, today)
        save_state(state)
        shared.append(pid)
        time.sleep(1)
        if done >= NIU_MAX_SHARE:
            break

    return f"分享 {len(shared)} 篇, 今日积分到账 {done}/{NIU_MAX_SHARE} 条"


def verify(server: str, store_token: str, proxies: Dict[str, str] | None) -> str:
    print("📋 [验证] 任务状态与积分流水")
    time.sleep(2)
    _, r = get_task_status(server, store_token, proxies)
    lines: List[str] = []
    for t in (r.get("data") or []):
        if "每日分享" in t.get("name", ""):
            st = {0: "未完成", 1: "进行中", 2: "已完成"}.get(t.get("status"),
                                                           t.get("status"))
            line = f"[{st}] {t.get('name')} ({t.get('point')}分)"
            lines.append(line)
            print(f"  {line}")

    r2 = api_get(server, f"{INTEGRAL_LIST_URL}?page=1&limit=5", store_token,
                 proxies, token_header="x-token")
    print("  最近积分流水:")
    for item in (r2.get("data") or [])[:5]:
        line = (f"{item.get('add_time')} {item.get('mark')} "
                f"+{item.get('number')} 余额{item.get('balance')}")
        lines.append(line)
        print(f"    {line}")

    return "；".join(lines) if lines else "未获取到任务/流水数据"


def run_account(index: int, total: int, account: AccountTarget) -> Dict[str, Any]:
    server = account.server
    result = {
        "server": server,
        "accountLabel": account.label,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "codeMsg": "-",
        "loginMsg": "-",
        "shareMsg": "-",
        "pointsMsg": "-",
        "error": "",
    }

    log_account_header(index, total, account)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    if NIU_TOKEN:
        token = NIU_TOKEN
        result["codeMsg"] = "使用环境变量 NIU_TOKEN"
        result["loginMsg"] = f"token 读取成功: {mask(token)}"
        print(f"🔐 [登录] 读取环境变量 NIU_TOKEN: {mask(token)}")
    else:
        code = get_code(account)
        result["codeMsg"] = "code 获取成功" if code else "code 获取失败"
        if not code:
            result["error"] = "获取 code 失败"
            return result

        result["accountLabel"] = account.label
        if account.remark or account.nickname:
            print(f"👤 [YYB] {account.label}")

        token, raw_login = login_by_code(server, code, proxies)
        if not token:
            result["error"] = f"登录失败: {json_preview(raw_login)}"
            return result
        result["loginMsg"] = f"code 换 token 成功: {mask(token)}"

    result["token"] = mask(token)
    show_token_status(token)

    try:
        store_token = store_login(server, token, proxies)

        state = load_state(server, token)
        print(f"📁 [状态] 已记录历史分享 {len(state['shared'])} 篇: {state['_path']}")

        result["shareMsg"] = do_daily_share(server, token, store_token,
                                            proxies, state)
        result["pointsMsg"] = verify(server, store_token, proxies)

        result["success"] = True
        return result
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🛵 小牛电动小程序任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}：{res.get("accountLabel") or res["server"]}
🌍 来源：{res["server"]}
🌐 代理：{res["proxyStatus"]}
📡 出口IP：{res["proxyIp"]}
🔐 Token：{res["token"]}
🔑 Code：{res["codeMsg"]}
📝 登录：{res["loginMsg"]}
📤 分享：{res["shareMsg"]}
📋 积分：{res["pointsMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    global CURRENT_ACCOUNTS
    try:
        CURRENT_ACCOUNTS = load_account_targets()
        CURRENT_ACCOUNTS = filter_accounts(CURRENT_ACCOUNTS, lambda account: account.ref, app_id=APP_ID, log=print)
    except Exception as exc:
        print(f"❌ [配置] {exc}")
        return
    log_title()

    results: List[Dict[str, Any]] = []

    for index, account in enumerate(CURRENT_ACCOUNTS, 1):
        try:
            result = run_account(index, len(CURRENT_ACCOUNTS), account)
            update_from_result(account.ref, result, app_id=APP_ID)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {account.label} 执行异常: {exc}")
            results.append({
                "server": account.server,
                "accountLabel": account.label,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "codeMsg": "-",
                "loginMsg": "-",
                "shareMsg": "-",
                "pointsMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(CURRENT_ACCOUNTS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 小牛电动任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🛵 小牛电动小程序任务完成", build_notify(results))


if __name__ == "__main__":
    main()
