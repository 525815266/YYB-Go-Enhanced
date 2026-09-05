#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PP停车动态 code 版

功能：
  1. YYB Go 多账号或旧版多端口服务获取微信 code
  2. /user/oauth 使用 code 换 token
  3. 查询今日签到状态
  4. 未签到时执行每日签到
  5. 查询 PP 积分余额
  6. 查询任务列表
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底
  9. 看视频得积分（每日两次）

环境变量：
  YYB_SERVER        YYB Go 地址@账号ID或OpenID，每行一个；配置后支持备注/昵称
  PP_SERVERS        旧版 code 服务地址，每行一个；仅在未配置 YYB_SERVER 时使用
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
"""

import base64
import json
import os
import random
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, quote, urlsplit

import requests
from yyb_account_guard import filter_accounts, update_from_result


APP_NAME = "PP停车小程序"
APPID = "wxa204074068ad40ef"

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
        legacy = os.getenv("PP_SERVERS", "").strip()
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

BASE_URL = "https://user-api.4pyun.com/rest/2.0/"
CHECKIN_PURPOSE = "app:user:checkin"
ADVERTISING_PURPOSE = "reward:motivate:advertising"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541c37) XWEB/25364"
)

# 小程序 request 层使用的对称混淆 key。
ENCRYPT_KEY = "riegh^ee:w0fok5je5eeS{eecaes1nep"

NEED_APPID_GET = {
    "parking/park/list",
    "parking/billing",
    "payment/channel/list",
    "captcha/create",
    "bonus/reward/task/status",
    "bonus/reward/task/list",
    "bonus/reward/acquire",
    "parking/reserve/list",
    "parking/recharge/prepare",
    "parking/change",
    "member/runtime/list",
    "parking/vehicle/autopay",
    "parking/reserve/runtime",
    "parking/group/recharge/prepare",
    "parking/runtime/summary",
    "payment/wallet",
    "user/app/rt/summary",
    "payment/entrust/matching",
}

NEED_APPID_POST = {
    "user/oauth",
    "parking/passport/enter",
    "parking/reserve/apply",
    "mall/trade/billing",
    "user/bind/mobile",
    "parking/payment/direct",
    "parking/recharge/payment",
    "parking/change",
    "bonus/reward/task/complete",
    "member/member/bind",
    "payment/wallet",
    "user/service/execute",
    "payment/trade/create/batch",
    "parking/group/recharge/payment",
    "bonus/event/token/summary",
    "bonus/event/apply",
    "parking/recharge/discount",
    "plus/member/subscribe/apply",
}

NEED_IDENTITY_GET = {
    "parking/billing",
    "parking/passport/status",
    "coupon/list",
    "parking/mcoupon/token/verify",
    "parking/change",
    "reward/balance",
    "parking/runtime/summary",
    "parking/reserve/state",
    "parking/reserve/list",
    "parking/reserve/runtime",
    "parking/recharge/prepare",
    "member/member/profile",
    "parking/collection/list",
    "parking/vehicle/autopay",
    "parking/parking/vip/apply/list",
    "parking/group/recharge/prepare",
    "payment/user/trade/list",
    "parking/payment/list",
    "payment/entrust/mathcing",
}

NEED_IDENTITY_POST = {
    "parking/passport/enter",
    "parking/reserve/apply",
    "bonus/event/apply",
    "parking/change",
    "user/bind/mobile",
    "parking/mcoupon/token/grant",
    "parking/recharge/payment",
    "member/member/bind",
    "parking/payment/direct",
    "bonus/event/token/summary",
    "payment/trade/create",
    "parking/recharge/discount",
}


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


def to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🅿️ PP停车动态 code 版                      ║")
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


def base_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/1040/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["Authorization"] = token
    return headers


def _is_empty(value: Any) -> bool:
    return (
        value is None
        or value == "undefined"
        or value == "null"
        or value == ""
        or (isinstance(value, (list, tuple, dict)) and len(value) == 0)
    )


def filter_empty_data(data: Any) -> Any:
    if isinstance(data, dict):
        result: Dict[str, Any] = {}
        for key, value in data.items():
            if _is_empty(value):
                continue
            if isinstance(value, list):
                filtered = [item for item in value if not _is_empty(item)]
                if filtered:
                    result[key] = filtered
            elif isinstance(value, dict):
                nested = filter_empty_data(value)
                if not _is_empty(nested):
                    result[key] = nested
            else:
                result[key] = value
        return result

    if isinstance(data, list):
        return [item for item in data if not _is_empty(item)]

    return data


def pp_encode(value: Any) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif not isinstance(value, str):
        value = str(value)

    raw = value.encode("utf-8")
    key_length = len(ENCRYPT_KEY)
    out = bytearray(len(raw))

    for index, byte in enumerate(raw):
        key_index = (len(raw) - index) % key_length
        out[index] = (ord(ENCRYPT_KEY[key_index]) ^ (~byte)) & 0xFF

    return base64.b64encode(bytes(out)).decode("ascii")


def encrypt_get_params(params: Dict[str, Any]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []

    for key, value in params.items():
        encrypted_key = pp_encode(key)

        if isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    item = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                pairs.append((encrypted_key, pp_encode(item)))
        elif isinstance(value, (dict, list)):
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            pairs.append((encrypted_key, pp_encode(encoded)))
        else:
            pairs.append((encrypted_key, pp_encode(value)))

    return pairs


def build_get_query(
    path: str,
    params: Dict[str, Any] | None,
    identity: str | None = None,
) -> str:
    data = dict(params or {})

    if path in NEED_APPID_GET:
        data["app_id"] = APPID
    if path in NEED_IDENTITY_GET:
        data["identity"] = identity or ""

    data = filter_empty_data(data) or {}
    encrypted_pairs = encrypt_get_params(data)

    return "&".join(
        f"{quote(key, safe='')}={quote(value, safe='')}"
        for key, value in encrypted_pairs
    )


def build_post_body(
    path: str,
    payload: Dict[str, Any] | None,
    identity: str | None = None,
) -> str:
    data = dict(payload or {})

    if path in NEED_APPID_POST:
        data["app_id"] = APPID
    if path in NEED_IDENTITY_POST:
        data["identity"] = identity or ""

    data = filter_empty_data(data) or {}
    return pp_encode(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def parse_response(response: requests.Response) -> Dict[str, Any]:
    try:
        return response.json()
    except Exception:
        return {
            "code": "-1",
            "message": f"JSON解析失败: {response.text[:300]}",
            "hint": "",
        }


def api_ok(resp: Dict[str, Any]) -> bool:
    return isinstance(resp, dict) and str(resp.get("code")) == "1001"


def api_payload(resp: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(resp, dict):
        return {}
    payload = resp.get("payload")
    return payload if isinstance(payload, dict) else {}


def api_message(resp: Dict[str, Any]) -> str:
    if not isinstance(resp, dict):
        return str(resp)

    for key in ("message", "msg", "title", "content", "hint"):
        value = resp.get(key)
        if value:
            return str(value)

    return json_preview(resp, 300)


def pp_get(
    server: str,
    path: str,
    token: str | None,
    proxies: Dict[str, str] | None,
    params: Dict[str, Any] | None = None,
    identity: str | None = None,
) -> Dict[str, Any]:
    query = build_get_query(path, params, identity)
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"

    headers = base_headers(token)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    response = request_with_proxy(
        "GET",
        url,
        headers=headers,
        proxies=proxies,
        server=server,
    )
    return parse_response(response)


def pp_post(
    server: str,
    path: str,
    token: str | None,
    proxies: Dict[str, str] | None,
    payload: Dict[str, Any] | None = None,
    identity: str | None = None,
) -> Dict[str, Any]:
    body = build_post_body(path, payload, identity)
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"

    headers = base_headers(token)
    headers["content-type"] = "application/json;charset=utf-8"

    response = request_with_proxy(
        "POST",
        url,
        headers=headers,
        data=body,
        proxies=proxies,
        server=server,
    )
    return parse_response(response)


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    token_obj = data.get("access_token")
    if isinstance(token_obj, dict):
        token_type = token_obj.get("type") or "Bearer"
        token_value = token_obj.get("value")
        if token_value:
            return f"{token_type} {token_value}"

    inner = data.get("data")
    if isinstance(inner, dict):
        return extract_token(inner)

    for key in ("token", "accessToken", "access_token", "jwt"):
        value = data.get(key)
        if value and value != "null":
            return str(value)

    return None


def login_by_code(
    server: str,
    code: str,
    proxies: Dict[str, str] | None,
) -> Tuple[str | None, str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        resp = pp_post(
            server,
            "user/oauth",
            None,
            proxies,
            {"oauth_code": code, "oauth_app_id": APPID, "unlock": 0},
        )

        if not api_ok(resp):
            print(f"❌ [登录] {api_message(resp)}")
            return None, None, resp

        payload = api_payload(resp)
        token = extract_token(payload)
        identity = payload.get("identity") or ""

        if not token:
            print(f"❌ [登录] 未识别 token 字段: {json_preview(payload)}")
            return None, None, resp

        print(f"✅ [登录] token 获取成功: {mask(token)}")
        return token, str(identity), resp
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None, None


def get_user_info(
    server: str,
    token: str,
    proxies: Dict[str, str] | None,
) -> Tuple[str, Dict[str, Any]]:
    resp = pp_get(server, "user/whoami", token, proxies)
    if not api_ok(resp):
        print(f"⚠️ [用户] 获取用户信息失败: {api_message(resp)}")
        return "", {}

    payload = api_payload(resp)
    nickname = payload.get("nickname") or payload.get("mobile") or payload.get("openid") or ""
    print(f"👤 [用户] {nickname or '未知用户'}")
    return str(nickname), payload


def get_sign_status(
    server: str,
    token: str,
    proxies: Dict[str, str] | None,
) -> Dict[str, Any]:
    resp = pp_get(
        server,
        "bonus/reward/task/status",
        token,
        proxies,
        {"purpose": CHECKIN_PURPOSE},
    )
    if not api_ok(resp):
        print(f"⚠️ [签到] 查询签到状态失败: {api_message(resp)}")
        return {}
    return api_payload(resp)


def sign_in(
    server: str,
    token: str,
    proxies: Dict[str, str] | None,
) -> Dict[str, Any]:
    resp = pp_get(
        server,
        "bonus/reward/acquire",
        token,
        proxies,
        {"purpose": CHECKIN_PURPOSE, "subscribe": 0},
    )
    if not api_ok(resp):
        print(f"⚠️ [签到] {api_message(resp)}")
        return {}
    return api_payload(resp)


def get_balance(
    server: str,
    token: str,
    identity: str,
    proxies: Dict[str, str] | None,
) -> Dict[str, Any]:
    resp = pp_get(
        server,
        "reward/balance",
        token,
        proxies,
        {"user_id": identity, "user_type": 1},
        identity=identity,
    )
    if not api_ok(resp):
        print(f"⚠️ [余额] 获取余额失败: {api_message(resp)}")
        return {}
    return api_payload(resp)


def get_task_list(
    server: str,
    token: str,
    proxies: Dict[str, str] | None,
) -> List[Dict[str, Any]]:
    resp = pp_get(server, "bonus/reward/task/list", token, proxies)
    if not api_ok(resp):
        print(f"⚠️ [任务] 获取任务列表失败: {api_message(resp)}")
        return []

    row = api_payload(resp).get("row")
    if not isinstance(row, list):
        return []

    return [item for item in row if isinstance(item, dict)]


def extract_voucher(referer_url: str | None) -> str:
    if not referer_url:
        return ""
    parsed = urlsplit(referer_url)
    values = parse_qs(parsed.query).get("voucher", [])
    return str(values[0]) if values else ""


def complete_bonus_task(
    server: str,
    token: str,
    proxies: Dict[str, str] | None,
    purpose: str,
    voucher: str,
) -> Dict[str, Any]:
    resp = pp_post(
        server,
        "bonus/reward/task/complete",
        token,
        proxies,
        {"purpose": purpose, "voucher": voucher},
    )
    return resp


def get_advertising_task(
    tasks: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    for task in tasks:
        if task.get("purpose") == ADVERTISING_PURPOSE:
            return task
    return None


def acquire_bonus_task(
    server: str,
    token: str,
    proxies: Dict[str, str] | None,
    purpose: str,
    voucher: str,
) -> Dict[str, Any]:
    return pp_get(
        server,
        "bonus/reward/acquire",
        token,
        proxies,
        {"purpose": purpose, "voucher": voucher, "subscribe": 0},
    )


def run_account(index: int, total: int, account: AccountTarget) -> Dict[str, Any]:
    server = account.server
    result = {
        "server": server,
        "accountLabel": account.label,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "nickname": "-",
        "signMsg": "-",
        "adMsg": "-",
        "balance": "-",
        "level": "-",
        "taskMsg": "-",
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

    code = get_code(account)
    if not code:
        result["error"] = "获取 code 失败"
        return result

    result["accountLabel"] = account.label
    if account.remark or account.nickname:
        print(f"👤 [YYB] {account.label}")

    token, identity, raw_login = login_by_code(server, code, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        nickname, _ = get_user_info(server, token, proxies)
        result["nickname"] = nickname or "-"

        sign_status = get_sign_status(server, token, proxies)
        progress = to_int(sign_status.get("progress"))
        combo = to_int(sign_status.get("combo"))

        if progress == 2:
            result["signMsg"] = f"今日已签到，连续 {combo} 天"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            sign_resp = sign_in(server, token, proxies)
            if sign_resp:
                message = sign_resp.get("message") or "签到成功"
                value = sign_resp.get("value") or sign_resp.get("grant_value") or 0
                result["signMsg"] = f"{message}（+{value} 积分）"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                result["signMsg"] = "签到失败"
                print(f"⚠️ [签到] 签到失败")

        # ---- 看视频得积分 ----
        ad_msg_parts: List[str] = []
        tasks = get_task_list(server, token, proxies)
        ad_task = get_advertising_task(tasks)
        if not ad_task:
            result["adMsg"] = "未找到看视频任务"
            print(f"⚠️ [视频] 未找到看视频任务")
        else:
            repeat_limit = to_int(ad_task.get("repeat_limit"))
            achieve_count = to_int(ad_task.get("achieve_count"))
            remaining = max(0, repeat_limit - achieve_count)
            if remaining == 0:
                result["adMsg"] = f"今日已完成 {repeat_limit} 次"
                print(f"✅ [视频] {result['adMsg']}")
            else:
                print(f"🎬 [视频] 开始执行看视频任务，剩余 {remaining} 次")
                for idx in range(1, remaining + 1):
                    if idx > 1:
                        tasks = get_task_list(server, token, proxies)
                        ad_task = get_advertising_task(tasks) or ad_task

                    voucher = extract_voucher(ad_task.get("referer_url"))
                    if not voucher:
                        ad_msg_parts.append(f"第{idx}次: 无 voucher")
                        print(f"⚠️ [视频] 第{idx}次: 无 voucher")
                        continue

                    wait = random.randint(3, 6)
                    print(f"⏳ [视频] 第 {idx} 次等待 {wait}s")
                    sleep(wait)

                    resp = complete_bonus_task(server, token, proxies, ADVERTISING_PURPOSE, voucher)
                    if not api_ok(resp):
                        ad_msg_parts.append(f"第{idx}次失败: {api_message(resp)}")
                        print(f"❌ [视频] 第{idx}次失败: {api_message(resp)}")
                    else:
                        acquire_resp = acquire_bonus_task(
                            server, token, proxies, ADVERTISING_PURPOSE, voucher
                        )
                        if api_ok(acquire_resp):
                            payload = api_payload(acquire_resp)
                            msg = payload.get("message") or "获得积分"
                            value = payload.get("value") or payload.get("grant_value") or 0
                            ad_msg_parts.append(f"第{idx}次: {msg} +{value}")
                            print(f"✅ [视频] 第{idx}次: {msg}（+{value} 积分）")
                        else:
                            ad_msg_parts.append(f"第{idx}次领取失败: {api_message(acquire_resp)}")
                            print(f"❌ [视频] 第{idx}次领取失败: {api_message(acquire_resp)}")

                    if idx < remaining:
                        sleep(random.randint(2, 4))

                result["adMsg"] = "；".join(ad_msg_parts) if ad_msg_parts else "无结果"

        balance_data = get_balance(server, token, identity, proxies)
        balance = balance_data.get("balance") or 0
        total_value = balance_data.get("total_value") or balance_data.get("balance") or 0
        level_info = balance_data.get("level") or {}
        level_name = level_info.get("name") or level_info.get("short_name") or "-"

        result["balance"] = f"{balance} / 累计 {total_value}"
        result["level"] = level_name
        print(f"🪙 [余额] 当前积分: {balance}，累计积分: {total_value}")
        print(f"🏅 [等级] {level_name}")

        tasks = get_task_list(server, token, proxies)
        todo_count = sum(1 for task in tasks if to_int(task.get("progress")) != 2)
        result["taskMsg"] = f"共 {len(tasks)} 个任务，待完成 {todo_count} 个"
        print(f"🗂️ [任务] {result['taskMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🅿️ PP停车多账号任务结果

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
👤 昵称：{res["nickname"]}
📝 签到：{res["signMsg"]}
🎬 视频：{res["adMsg"]}
🪙 积分：{res["balance"]}
🏅 等级：{res["level"]}
🗂️ 任务：{res["taskMsg"]}
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
        CURRENT_ACCOUNTS = filter_accounts(CURRENT_ACCOUNTS, lambda account: account.ref, app_id=APPID, log=print)
    except Exception as exc:
        print(f"❌ [配置] {exc}")
        return
    log_title()

    results: List[Dict[str, Any]] = []

    for index, account in enumerate(CURRENT_ACCOUNTS, 1):
        try:
            result = run_account(index, len(CURRENT_ACCOUNTS), account)
            update_from_result(account.ref, result, app_id=APPID)
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
                "nickname": "-",
                "signMsg": "-",
                "adMsg": "-",
                "balance": "-",
                "level": "-",
                "taskMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(CURRENT_ACCOUNTS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 PP停车任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🅿️ PP停车多账号任务完成", build_notify(results))


if __name__ == "__main__":
    main()
