#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ========== 企业微信推送配置（可选） ==========
QYWX_TOKEN = __import__("os").getenv("QYWX_TOKEN", "")  # 企业微信机器人 Webhook key（机器人地址 ?key= 后面的值，留空不推送）

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
绝味鸭脖小程序30天连续签到挑战动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. 使用 code 换取活动身份 li（tokenSign）
  3. /xm/token/getUserToken 使用 li 换 xmToken（tokenSign + xmSign 双签名）
  4. 每日签到 /sign/action（xmSign 签名）
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

⚠️ 登录接口为推断：源脚本为抓包 URL 型（activityMultiport.html?li=...），仓库内未找到
   code 换 li 的登录接口，默认尝试 GET /wx/auth?code= 换取 li，未经真机验证，
   失败请抓包核对；也可将抓包 URL 中 li 参数的值填入环境变量 JWYB_LIS 直接使用。

环境变量：
  code 服务列表：127.0.0.1:8088（CODE_SERVER 可覆盖为单个地址）
  JWYB_LIS         可选，抓包 activityMultiport.html 链接里 li 参数的值，
                   多账号用 & 或换行分隔（与 code 服务账号顺序一致）
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

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
import re
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "绝味鸭脖小程序"
APPID = "wxf6ec04edb4802bec"

SERVERS = [
    "127.0.0.1:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://p3720226302625sh3s-saas.xiaoman-activity.meta-xuantan.com"
LOGIN_URL = f"{BASE_URL}/wx/auth"  # ⚠️ 推断的 code 换 li 接口
USER_TOKEN_URL = f"{BASE_URL}/xm/token/getUserToken"
SIGN_URL = f"{BASE_URL}/sign/action"
REFERER_URL = f"{BASE_URL}/activityMultiport.html"

# 源脚本内置的两组签名密钥（base64 原样保留）
BASE_KEY = base64.b64decode("dWgzJEhnJl5ISzg3NiVnYnhWRzdmJCVwPTBNfj5zMXg=").decode("utf-8")
TOKEN_KEY = base64.b64decode("SjdoOCZeQmdzNSNibio3aG4lIT1raDMwOCpidjIhc14=").decode("utf-8")

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jwybcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.56(0x1800383b) NetType/WIFI "
    "Language/zh_CN miniProgram/wxf6ec04edb4802bec"
)


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


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def md5_hex(text: Any) -> str:
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🦆 绝味鸭脖连续签到动态 code 版               ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(SERVERS):<34}║")
    print("╚" + "═" * 50 + "╝")


def log_account_header(index: int, total: int, server: str) -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 🌍 来源 {server:<40}│")
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



def send_qywx(title, content):
    """企业微信机器人推送（Webhook）。未配置 QYWX_TOKEN 时自动跳过。"""
    if not QYWX_TOKEN:
        print("[企业微信] 未配置 QYWX_TOKEN，跳过推送")
        return False
    key = QYWX_TOKEN.split("key=")[-1].strip()
    import json as _qywx_json, urllib.request as _qywx_urllib
    try:
        text = "%s\n%s" % (title, content)
        if len(text.encode("utf-8")) > 2000:
            text = text.encode("utf-8")[:2000].decode("utf-8", "ignore")
        payload = _qywx_json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
        req = _qywx_urllib.Request("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key,
                                   data=payload, headers={"Content-Type": "application/json"})
        res = _qywx_json.loads(_qywx_urllib.urlopen(req, timeout=10).read().decode("utf-8"))
        ok = res.get("errcode") == 0
        print("[企业微信] 推送%s errcode=%s errmsg=%s" % ("成功 ✓" if ok else "失败 ✗", res.get("errcode"), res.get("errmsg", "")))
        return ok
    except Exception as _exc:
        print("[企业微信] 推送异常:", _exc)
        return False
def send_pushplus(title: str, content: str) -> None:
    send_qywx(title, content)  # 企业微信推送（QYWX_TOKEN 未配置时自动跳过）
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


def get_code(server: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url}")

    try:
        response = direct_session().get(
            url,
            params={"appId": APPID},
            timeout=20,
        )
        data = response.json()

        if data.get("err") != 0 or not data.get("code"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


# ====================== 小满活动签名（源脚本原样移植） ======================
def gen_uuid() -> str:
    return uuid.uuid4().hex.lower()


def obj_key_sort(params: Dict[str, Any]) -> Dict[str, Any]:
    ordered: Dict[str, Any] = {}
    for key in sorted(params.keys(), key=str):
        if key is None or key == "null":
            continue
        ordered[key] = params[key]
    return ordered


def js_value(value: Any) -> str:
    """模拟 JS 的值拼接规则：对象 JSON.stringify，null -> null，布尔小写。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def gen_token_sign(li: str) -> Dict[str, Any]:
    nonce = gen_uuid()
    timestamp = int(time.time() * 1000)
    token_sign = f"{li}{nonce}{timestamp}{TOKEN_KEY}"
    return {
        "nonceStr": nonce,
        "timestamp": timestamp,
        "tokenSign": md5_hex(token_sign),
    }


def gen_xm_sign(params: Dict[str, Any], xm_token: str = "") -> Dict[str, Any]:
    merged = dict(params or {})
    merged["nonceStr"] = gen_uuid()
    merged["xmTimestamp"] = int(time.time() * 1000)
    merged["xmToken"] = xm_token

    ordered = obj_key_sort(merged)
    joined = "".join(js_value(value) for value in ordered.values())
    sign = md5_hex(joined + BASE_KEY)

    return {
        "xmSign": sign,
        "nonceStr": merged["nonceStr"],
        "xmTimestamp": merged["xmTimestamp"],
        "xmToken": xm_token,
    }


def common_headers(state: Dict[str, Any] | None = None) -> Dict[str, str]:
    state = state or {}
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": REFERER_URL,
        "Content-Type": "application/json",
        "ri": "",
        "bdrk": "",
    }
    if state.get("xmToken"):
        headers["xmToken"] = state["xmToken"]
    return headers


def extract_token(data: Any) -> str | None:
    """从（推断的）登录响应中提取 li。"""
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("li"),
        data.get("tokenSign"),
        data.get("token"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("li"),
            inner.get("tokenSign"),
            inner.get("token"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> str | None:
    """⚠️ 推断接口：使用 code 换活动身份 li"""
    try:
        print("🔐 [登录] 使用 code 换 li（推断接口）")
        response = request_with_proxy(
            "GET",
            LOGIN_URL,
            headers=common_headers(),
            params={"code": code},
            proxies=proxies,
            server=server,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        li = extract_token(data)
        if li:
            print(f"✅ [登录] li 获取成功: {mask(li)}")
            return li

        print(f"❌ [登录] 未识别 li 字段: {json_preview(data)}")
        return None
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None


def api_get(
    server: str,
    url: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    params: Dict[str, Any] | None = None,
    sign_params: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    headers = common_headers(state)
    headers.update(gen_xm_sign(sign_params if sign_params is not None else (params or {}), state.get("xmToken", "")))
    response = request_with_proxy(
        "GET",
        url,
        headers=headers,
        params=params,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "desc": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(
    server: str,
    url: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    payload: Dict[str, Any],
    sign_params: Dict[str, Any] | None = None,
    extra_headers: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    headers = common_headers(state)
    if extra_headers:
        headers.update(extra_headers)
    headers.update(gen_xm_sign(sign_params if sign_params is not None else dict(payload), state.get("xmToken", "")))
    response = request_with_proxy(
        "POST",
        url,
        headers=headers,
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "desc": f"JSON解析失败: {response.text[:300]}",
        }


# ====================== Token缓存管理 ======================
def load_token_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        print(f"⚠️ [缓存] 读取失败: {exc}")
    return {}


def save_token_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print("✅ [缓存] Token保存成功")
    except Exception as exc:
        print(f"❌ [缓存] 保存失败: {exc}")


def get_cached_token(server: str) -> str | None:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} token")
                return data["token"]
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, token: str, expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def env_account_values(env_name: str) -> List[str]:
    """读取按 & 或换行分隔的多账号环境变量值列表。"""
    raw = os.getenv(env_name, "")
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[&\n]", raw) if item.strip()]


def default_expire_time() -> str:
    return datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()


def get_user_token(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> bool:
    """getUserToken：使用 li（tokenSign 签名）换 xmToken，成功后写入 state。"""
    li = state.get("li", "")
    if not li:
        print("❌ [token] 缺少 li，无法获取用户 token")
        return False

    query = gen_token_sign(li)
    resp = api_get(server, USER_TOKEN_URL, state, proxies, params=query, sign_params=query)
    if resp.get("code") != 0:
        print(f"❌ [token] 获取用户token失败: {resp.get('desc') or json_preview(resp, 300)}")
        return False

    xm_token = str(resp.get("data") or "")
    if not xm_token:
        print(f"❌ [token] 未返回 xmToken: {json_preview(resp, 300)}")
        return False

    state["xmToken"] = xm_token
    print(f"✅ [token] xmToken 获取成功: {mask(xm_token)}")
    return True


def login_with_cache(
    server: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    account_index: int = 1,
) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 li（getUserToken 验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        state["li"] = cache_token
        print("🔍 [缓存] 验证 li")
        if get_user_token(server, state, proxies):
            print("✅ [缓存] li 有效")
            return cache_token, None
        print("⚠️ [缓存] li 已失效，重新获取")

    env_lis = env_account_values("JWYB_LIS")
    if 0 <= account_index - 1 < len(env_lis):
        env_li = env_lis[account_index - 1]
        print(f"🔑 [登录] 使用环境变量 JWYB_LIS 第 {account_index} 个 li")
        state["li"] = env_li
        if get_user_token(server, state, proxies):
            set_cached_token(server, env_li, default_expire_time())
            return env_li, None
        print("⚠️ [登录] 环境 li 校验失败")

    code = get_code(server)
    if not code:
        return None, None

    li = login_by_code(server, code, proxies)
    if not li:
        return None, None

    state["li"] = li
    if not get_user_token(server, state, proxies):
        return None, None

    set_cached_token(server, li, default_expire_time())
    return li, None


def do_sign(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> Tuple[str, bool]:
    body = {"patchDate": datetime.now().strftime("%Y-%m-%d")}
    resp = api_post(
        server,
        SIGN_URL,
        state,
        proxies,
        payload=body,
        sign_params={**body, "xmToken": state.get("xmToken", "")},
        extra_headers={"functionid": "0"},
    )
    if resp.get("code") == 0:
        desc = str(resp.get("desc") or "成功")
        print(f"✅ [签到] {desc}")
        return f"签到成功: {desc}", True

    desc = str(resp.get("desc") or json_preview(resp, 300))
    print(f"❌ [签到] {desc}")
    return f"签到失败: {desc}", False


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "xmToken": "-",
        "signMsg": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    state: Dict[str, Any] = {"li": "", "xmToken": ""}

    li, raw_login = login_with_cache(server, state, proxies, index)
    if not li or not state.get("xmToken"):
        result["error"] = f"登录失败: {json_preview(raw_login) if raw_login else '获取 li/xmToken 失败'}"
        return result

    result["token"] = mask(li)
    result["xmToken"] = mask(state["xmToken"])

    try:
        # 源脚本在 getUserToken 与签到之间随机等待 30-60s
        wait_time = random.randint(30, 60)
        print(f"⏳ [延迟] 签到前等待 {wait_time}s")
        sleep(wait_time)

        sign_msg, sign_ok = do_sign(server, state, proxies)
        result["signMsg"] = sign_msg
        result["success"] = sign_ok
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🦆 绝味鸭脖连续签到动态 code 任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🔑 身份：{res["token"]}
🎟️ 凭证：{res["xmToken"]}
📝 签到：{res["signMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "xmToken": "-",
                "signMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 绝味鸭脖任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🦆 绝味鸭脖任务完成", build_notify(results))


# --- YYB compatibility layer (managed) ---
import os as _yyb_os
import json as _yyb_json

def _yyb_accounts():
    result = []
    for line in _yyb_os.getenv("YYB_SERVER", "").splitlines():
        line = line.strip()
        if not line or "@" not in line or line == "[object Object]":
            continue
        endpoint, ref = (part.strip() for part in line.split("@", 1))
        if endpoint and ref:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = "http://" + endpoint
            result.append(endpoint.rstrip("/") + "@" + ref)
    return result


def _yyb_parts(server):
    value = str(server).strip()
    if "@" not in value:
        return value.rstrip("/"), ""
    return value.rsplit("@", 1)[0].rstrip("/"), value.rsplit("@", 1)[1]


def _yyb_appid(args, kwargs):
    appid = kwargs.get("appid") or kwargs.get("app_id")
    if not appid and args and isinstance(args[0], str):
        appid = args[0]
    if not appid:
        appid = globals().get("APPID") or globals().get("APP_ID") or ""
    if isinstance(appid, (list, tuple)):
        appid = appid[0] if appid else ""
    return str(appid)


def _yyb_json_request(server, path, appid, payload=None):
    import requests
    endpoint, ref = _yyb_parts(server)
    if not endpoint or not ref or not appid:
        raise RuntimeError("YYB 参数不完整：需要 地址@账号ID 和 app_id")
    headers = {}
    api_key = _yyb_os.getenv("YYB_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    response = requests.post(
        endpoint + path,
        json={"ref": ref, "app_id": str(appid), **(payload or {})},
        headers=headers,
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("YYB 返回非 JSON") from exc
    if response.status_code >= 400:
        raise RuntimeError(str(body.get("message") or body.get("msg") or body))
    return body


def _yyb_find_code(value):
    if isinstance(value, dict):
        if value.get("code") not in (None, "", "null", "invalid") and isinstance(value.get("code"), str):
            return value["code"]
        for child in value.values():
            found = _yyb_find_code(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _yyb_find_code(child)
            if found:
                return found
    return None


def _yyb_code(server, *args, **kwargs):
    body = _yyb_json_request(server, "/wxapp/getCode", _yyb_appid(args, kwargs))
    code = _yyb_find_code(body)
    if not code:
        raise RuntimeError(str(body.get("msg") or body.get("message") or "YYB 未返回 wx.login code"))
    return str(code)


def _yyb_phone(server, *args, **kwargs):
    body = _yyb_json_request(server, "/wxapp/getPhoneNumber", _yyb_appid(args, kwargs))
    return body.get("result") or body.get("data") or body


if _yyb_accounts():
    SERVERS = _yyb_accounts()


_yyb_original_get_code = get_code


def get_code(server, *args, **kwargs):
    if "@" in str(server):
        phone_code = kwargs.get("phone_code") is True or (args and args[0] is True)
        if phone_code:
            body = _yyb_phone(server)
            code = _yyb_find_code(body)
            if not code:
                raise RuntimeError("YYB 未返回手机号授权 code")
            return str(code)
        return _yyb_code(server, *args, **kwargs)
    return _yyb_original_get_code(server, *args, **kwargs)


if "get_code_for" in globals():
    _yyb_original_get_code_for = get_code_for

    def get_code_for(server, appid):
        if "@" in str(server):
            return _yyb_code(server, appid)
        return _yyb_original_get_code_for(server, appid)


def get_phone_payload(server, *args, **kwargs):
    if "@" in str(server):
        return _yyb_phone(server, *args, **kwargs)
    raise RuntimeError("当前账号不是 YYB_SERVER 格式，无法获取手机号授权包")


# Adapt the two common source-specific phone helpers when present.
if "get_phone_number_payload" in globals():
    get_phone_number_payload = get_phone_payload
if "get_phone_authorize" in globals():
    get_phone_authorize = get_phone_payload
if "get_phone_data" in globals():
    get_phone_data = get_phone_payload
if "get_phone_package" in globals():
    _yyb_original_get_phone_package = get_phone_package

    def get_phone_package(server, *args, **kwargs):
        if "@" not in str(server):
            return _yyb_original_get_phone_package(server, *args, **kwargs)
        body = _yyb_phone(server, *args, **kwargs)
        # YYB keeps the upstream response shape. Do not invent encryptedData/iv.
        if isinstance(body, dict):
            result = body.get("result") if isinstance(body.get("result"), dict) else body
            data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
            raw = data.get("raw") if isinstance(data, dict) and isinstance(data.get("raw"), dict) else data
            if isinstance(raw, dict):
                return {
                    "authCode": str(raw.get("code") or data.get("code") or ""),
                    "encryptPhoneNumber": str(raw.get("encryptedData") or ""),
                    "initVector": str(raw.get("iv") or ""),
                }
        return {"authCode": "", "encryptPhoneNumber": "", "initVector": ""}
if "get_userinfo_blob" in globals():
    _yyb_original_get_userinfo_blob = get_userinfo_blob

    def get_userinfo_blob(server):
        if "@" not in str(server):
            return _yyb_original_get_userinfo_blob(server)
        body = _yyb_json_request(server, "/wx/getuserinfo", _yyb_appid((), {}))
        info = body.get("user_info")
        # /wx/getuserinfo is a profile endpoint; it is not a source of
        # encryptedData/iv/signature. Never fabricate those fields.
        if not isinstance(info, dict):
            return None
        return {"userInfo": info, "rawData": _yyb_json.dumps(info, ensure_ascii=False), "errMsg": "getUserInfo:ok"}
# --- end YYB compatibility layer ---

if __name__ == "__main__":
    main()
