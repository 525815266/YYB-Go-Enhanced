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
华润壹票达小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /cyy_gatewayapi/mcommon/pub/v1/union_login 使用 code 换 access-token
  3. 未绑账号走 /union_login/authorization 手机号授权完成注册
  4. 读取签到日历（check_in_calendar，查询连续签到天数并验证 token）
  5. 每日签到（check_in，奖励与连续天数）
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

接口契约（照源脚本）：
  每个请求带公共 query（currency/lang/terminalSrc/utcOffset/ver）与固定请求头
  src / terminal-src / merchant-id / ver / utc-offset / front-trace-id，
  鉴权时再加 access-token；成功判定是 statusCode==200（不是 code），
  失败原因在 comments 字段。
  ⚠️ 手机号授权分支依赖 code 服务的 /wx/getphonenumber 接口提供
     encryptedData/iv/authCode；该接口不可用时仅支持已绑账号登录。

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       覆盖本地 code 服务地址，可选
  YPD_PHONE_LOGIN   默认 1：首登只回 authToken 时自动走手机号授权；置 0 则不做授权

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""


import json
import os
import random
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote
from uuid import uuid4

import requests


APP_NAME = "华润壹票达小程序"
APPID = "wx70c418a86bc52a9f"

SERVERS = [
    "127.0.0.1:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
PHONE_LOGIN = os.getenv("YPD_PHONE_LOGIN", "1") != "0"

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://crld.caiyicloud.com"
UNION_LOGIN_URL = f"{BASE_URL}/cyy_gatewayapi/mcommon/pub/v1/union_login"
UNION_AUTH_URL = f"{BASE_URL}/cyy_gatewayapi/mcommon/pub/v1/union_login/authorization"
CHECK_IN_URL = f"{BASE_URL}/cyy_gatewayapi/user/buyer/v1/check_in"
CALENDAR_URL = f"{BASE_URL}/cyy_gatewayapi/user/buyer/v1/check_in_calendar"

MERCHANT_ID = "6942616f50ef5900011a1d2e"
VER = "4.63.0"
SRC = "weixin_mini"
TERMINAL_SRC = "WEIXIN_MINI"
UTC_OFFSET = "480"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ybddcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; M2012K11AC Build/SKQ1.220303.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/134.0.6998.136 "
    "Mobile Safari/537.36 MicroMessenger/8.0.48.2580(0x28003036) MiniProgramEnv/android"
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


def is_ok(resp: Any) -> bool:
    """照源脚本：成功判定是 statusCode==200（不是 code）"""
    return isinstance(resp, dict) and to_float(resp.get("statusCode")) == 200


def msg_of(resp: Any) -> str:
    if isinstance(resp, dict):
        return str(resp.get("comments") or resp.get("errorCode") or json_preview(resp, 200))
    return str(resp)[:200]


def is_already_done(text: str) -> bool:
    return any(keyword in str(text or "") for keyword in ("已签", "已经签", "签到过", "重复", "已完成", "already"))


def is_auth_error(text: str) -> bool:
    return any(keyword in str(text or "") for keyword in ("登录", "token", "未授权", "未登录", "失效", "过期", "重新"))


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🎫 华润壹票达动态 code 版                    ║")
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


def get_phone_package(server: str) -> Dict[str, str]:
    """手机号授权包：authCode 用于 authCode 字段，raw 里的 encryptedData/iv 用于加密手机号"""
    result = {"authCode": "", "encryptPhoneNumber": "", "initVector": ""}
    url = f"http://{server}/wx/getphonenumber"
    print(f"📱 [授权] 请求手机号授权包: {url}")
    try:
        response = direct_session().post(
            url,
            json={"appid": APPID},
            timeout=60,
        )
        data = response.json()
        if not data.get("status"):
            print(f"❌ [授权] 取手机号授权包失败: {json_preview(data, 300)}")
            return result
        inner = data.get("data") or {}
        raw = inner.get("raw") or {}
        result["authCode"] = str(inner.get("code") or raw.get("code") or "")
        result["encryptPhoneNumber"] = str(raw.get("encryptedData") or "")
        result["initVector"] = str(raw.get("iv") or "")
        print("✅ [授权] 手机号授权包获取成功")
    except Exception as exc:
        print(f"❌ [授权] 取手机号授权包异常: {exc}")
    return result


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/0/page-frame.html",
        "src": SRC,
        "terminal-src": TERMINAL_SRC,
        "merchant-id": MERCHANT_ID,
        "ver": VER,
        "utc-offset": UTC_OFFSET,
        # front-trace-id 照源脚本：每请求随机 32 位 hex
        "front-trace-id": uuid4().hex + uuid4().hex[:32],
    }
    if token:
        headers["access-token"] = token
    return headers


def common_query() -> Dict[str, str]:
    return {
        "currency": "CNY",
        "lang": "zh",
        "terminalSrc": TERMINAL_SRC,
        "utcOffset": UTC_OFFSET,
        "ver": VER,
    }


def base_body() -> Dict[str, Any]:
    return {"src": SRC, "merchantId": MERCHANT_ID, "ver": VER, "appId": APPID}


def month_range_ms() -> Tuple[int, int]:
    """本月区间（毫秒），日历接口要"""
    now = datetime.now()
    begin = datetime(now.year, now.month, 1)
    # 用下月 1 号减 1 秒的方式拿到本月最后一天，避免大小月问题
    if now.month == 12:
        end = datetime(now.year, 12, 31, 23, 59, 59)
    else:
        next_month = datetime(now.year, now.month + 1, 1)
        end = datetime.fromtimestamp(next_month.timestamp() - 1)
    return int(begin.timestamp() * 1000), int(end.timestamp() * 1000)


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

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    query = common_query()
    if params:
        query.update(params)
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token),
        params=query,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "statusCode": -1,
            "comments": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token),
        params=common_query(),
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "statusCode": -1,
            "comments": f"JSON解析失败: {response.text[:300]}",
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


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token（union_login）")
        first = api_post(server, UNION_LOGIN_URL, "", proxies, {
            **base_body(),
            "unionType": TERMINAL_SRC,
            "wxParam": {"code": code},
            "deviceInfo": {"volcWebId": ""},
        })
        if not is_ok(first):
            print(f"❌ [登录] union_login 失败: {msg_of(first)}")
            return None, first

        inner = safe_data(first)
        token = str(inner.get("accessToken") or "")
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, first

        # 只回 authToken 说明这个微信号还没在壹票达绑过，要用手机号授权把账号建起来
        auth_token = str(inner.get("authToken") or "")
        open_id = str(inner.get("openId") or "")
        if not auth_token or not open_id:
            print(f"❌ [登录] union_login 既没给 accessToken 也没给 authToken: {json_preview(first)}")
            return None, first

        if not PHONE_LOGIN:
            print("❌ [登录] 该微信号还没在壹票达绑定（服务端只回 authToken）。把环境变量 YPD_PHONE_LOGIN 设为 1 可自动走手机号授权，或在小程序里手动登录一次")
            return None, first

        print("⚠️ [登录] 未绑账号，走手机号授权完成注册")
        phone = get_phone_package(server)
        if not phone["authCode"] and not phone["encryptPhoneNumber"]:
            print("❌ [登录] 手机号授权包里既没有 authCode 也没有 encryptedData，无法完成 authorization")
            return None, first

        second = api_post(server, UNION_AUTH_URL, "", proxies, {
            **base_body(),
            "unionType": TERMINAL_SRC,
            "authToken": auth_token,
            "openId": open_id,
            "wxParam": {
                "openId": open_id,
                "encryptPhoneNumber": phone["encryptPhoneNumber"],
                "initVector": phone["initVector"],
                "authCode": phone["authCode"],
            },
            "invitePageId": "",
            "deviceInfo": {"volcWebId": ""},
        })
        if not is_ok(second):
            print(f"❌ [登录] 手机号授权失败: {msg_of(second)}")
            return None, second

        token = str(safe_data(second).get("accessToken") or "")
        if not token:
            print(f"❌ [登录] 授权成功但没返回 accessToken: {json_preview(second)}")
            return None, second

        print(f"✅ [登录] token 获取成功: {mask(token)}（手机号授权）")
        return token, second
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（签到日历接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            calendar_resp = query_calendar(server, cache_token, proxies, need_log=False)
            if calendar_resp:
                print("✅ [缓存] token 有效")
                return cache_token, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, raw_login

    expire_time = None
    if raw_login and isinstance(raw_login, dict):
        inner = raw_login.get("data")
        if isinstance(inner, dict):
            expire_time = inner.get("expireTime") or inner.get("expire_time")
            expires_in = inner.get("expiresIn")
            if not expire_time and isinstance(expires_in, (int, float)) and expires_in > 0:
                expire_time = datetime.fromtimestamp(time.time() + expires_in).isoformat()
    if not expire_time:
        expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    elif not isinstance(expire_time, str):
        expire_time = datetime.fromtimestamp(expire_time / 1000).isoformat()
    set_cached_token(server, token, expire_time)
    return token, raw_login


def query_calendar(server: str, token: str, proxies: Dict[str, str] | None, need_log: bool = True) -> bool:
    """只读，用来报连续天数 + 校验 token 是否还活着。不判断今天签没签（照源脚本说明，靠签到接口幂等）"""
    begin, end = month_range_ms()
    resp = api_get(server, CALENDAR_URL, token, proxies, params={
        "src": SRC,
        "merchantId": MERCHANT_ID,
        "appId": APPID,
        "pageSource": "TASK_CENTER",
        "beginDate": str(begin),
        "endDate": str(end),
    })
    if not is_ok(resp):
        if need_log:
            print(f"⚠️ [日历] 读取签到日历失败: {msg_of(resp)}")
        return False
    if need_log:
        streak = safe_data(resp).get("streakCheckInDays", "?")
        print(f"📅 [日历] 签到状态: 连续 {streak} 天")
    return True


def do_sign(server: str, token: str, proxies: Dict[str, str] | None, retry: bool = True) -> str:
    resp = api_post(server, CHECK_IN_URL, token, proxies, base_body())
    if is_ok(resp):
        data = safe_data(resp)
        rewards = []
        for reward in (data.get("rewardAggPackage") or []):
            if not isinstance(reward, dict):
                continue
            reward_type = str(reward.get("rewardType") or "")
            type_text = "积分" if reward_type == "POINT" else reward_type
            rewards.append(f"{reward.get('reward')}{type_text}")
        reward_text = " ".join(rewards)
        streak = data.get("streakCheckInDays", "?")
        sign_msg = f"签到成功{'，获得 ' + reward_text if reward_text else ''}（连续 {streak} 天）"
        print(f"✅ [签到] {sign_msg}")
        return sign_msg

    msg = msg_of(resp)
    if is_already_done(msg):
        sign_msg = f"今日已签到（{msg}）"
        print(f"✅ [签到] {sign_msg}")
        return sign_msg
    if retry and is_auth_error(msg):
        print("⚠️ [签到] 会话失效，重新登录后重试")
        new_token, _ = login_with_cache(server, proxies)
        if new_token:
            return do_sign(server, new_token, proxies, retry=False)
    sign_msg = f"签到失败: {msg}"
    print(f"❌ [签到] {sign_msg}")
    return sign_msg


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "calendarMsg": "-",
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

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        # 读取签到日历（连续天数）
        if query_calendar(server, token, proxies):
            result["calendarMsg"] = "签到日历读取正常"
        else:
            result["calendarMsg"] = "签到日历读取失败"

        # 每日签到
        wait_time = random.randint(2, 5)
        print(f"⏳ [签到] 提交前等待 {wait_time}s")
        sleep(wait_time)
        result["signMsg"] = do_sign(server, token, proxies)

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🎫 华润壹票达任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
📅 日历：{res["calendarMsg"]}
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
                "signMsg": "-",
                "calendarMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 华润壹票达任务执行完成                    ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🎫 华润壹票达任务完成", build_notify(results))


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
