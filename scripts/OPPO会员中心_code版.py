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
OPPO 小程序会员 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. /user/pre/auth 使用 code 换 sessionId/encryptedSession/openId
  3. 查询会员信息（积分/成长值/等级）与签到入口
  4. 从签到 H5 页发现当期签到活动 id（按月轮换，发现失败回落内置 id）
  5. 每日积分签到（cumulativeSignIn/signIn）
  6. 签到后复查会员积分
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import json
import os
import random
import re
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "OPPO 小程序会员"
APPID = "wxe705c556754a1de2"

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

MINI_API = "https://omoapplet-api-cn.heytap.com"
H5_API = "https://hd.opposhop.cn"
APP_VERSION = "361"
BUSINESS = 1
# 仅作回落：2026-06 那期，已过期；当期 activityId 运行时从签到 H5 页发现
FALLBACK_SIGN_ACTIVITY_ID = "2061050217641549824"
FALLBACK_CREDITS_ADD_ACTION_ID = "1788913e6d9e4683b8b9ab0088733560"
SIGN_PAGE = "https://hd.opposhop.cn/bp/b371ce270f7509f0?nightModelEnable=true&utm_source=huiyuanwx&utm_medium=me_qiandao"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oppocookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) MicroMessenger/3.9.12 MiniProgramEnv/Windows WindowsWechat/WMPF"
)
# 抓 H5 页面用手机端微信 UA，跟小程序 web-view 里的环境一致
H5_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/107.0.0.0 Mobile Safari/537.36 MicroMessenger/8.0.30"
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


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 📱 OPPO 小程序会员 code 版                   ║")
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


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://servicewechat.com/{APPID}/{APP_VERSION}/page-frame.html",
    }
    if token:
        headers["sessionId"] = token
    return headers


def mini_headers(session: Dict[str, str]) -> Dict[str, str]:
    """小程序 API 请求头（照源脚本 miniHeaders）"""
    return {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "s_channel": "oppo",
        "source_type": "2",
        "s_version": "010000",
        "spCallSource": "oppohy",
        "Referer": f"https://servicewechat.com/{APPID}/{APP_VERSION}/page-frame.html",
        "sessionId": session.get("sessionId", ""),
        "NEWOPPOSID": session.get("encryptedSession", ""),
        "openid": session.get("openId", ""),
        "sa_distinct_id": session.get("openId", ""),
        "constToken": session.get("sessionId", ""),
    }


def h5_headers(session: Dict[str, str]) -> Dict[str, str]:
    """签到 H5 活动页请求头（照源脚本 h5Headers）"""
    cookie = "; ".join([
        f"NEWOPPOSID={quote(session.get('encryptedSession', ''))}",
        f"sessionId={quote(session.get('sessionId', ''))}",
        f"openid={quote(session.get('openId', ''))}",
    ])
    return {
        "User-Agent": H5_USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": H5_API,
        "Referer": SIGN_PAGE,
        "sessionId": session.get("sessionId", ""),
        "NEWOPPOSID": session.get("encryptedSession", ""),
        "openid": session.get("openId", ""),
        "sa_distinct_id": session.get("openId", ""),
        "constToken": session.get("sessionId", ""),
        "Cookie": cookie,
    }


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("sessionId"),
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("sessionId"),
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            f"{MINI_API}/user/pre/auth",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Referer": f"https://servicewechat.com/{APPID}/{APP_VERSION}/page-frame.html",
            },
            json={"code": code},
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        if response.status_code != 200 or str(data.get("ret")) != "1":
            print(f"❌ [登录] 登录失败: {json_preview(data)}")
            return None, data

        info = safe_data(data)
        session_id = str(info.get("sessionId") or "")
        if not session_id:
            print(f"❌ [登录] 登录响应缺少 sessionId: {json_preview(data)}")
            return None, data

        print(f"✅ [登录] token 获取成功: {mask(session_id)}")
        return session_id, {
            "encryptedSession": str(info.get("encryptedSession") or ""),
            "openId": str(info.get("openId") or ""),
        }
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token),
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


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token),
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


def mini_request(
    server: str,
    session: Dict[str, str],
    method: str,
    path: str,
    proxies: Dict[str, str] | None,
    params: Dict[str, Any] | None = None,
    payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    url = f"{MINI_API}{path}"
    if method.upper() == "GET":
        response = request_with_proxy("GET", url, headers=mini_headers(session), params=params, proxies=proxies, server=server)
    else:
        response = request_with_proxy("POST", url, headers=mini_headers(session), json=payload if payload is not None else {}, proxies=proxies, server=server)

    if response.status_code != 200:
        raise Exception(f"{path} HTTP {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
    except Exception:
        raise Exception(f"{path} 响应非JSON: {response.text[:300]}")

    ret = data.get("ret")
    if ret is not None and str(ret) != "1":
        raise Exception(f"{path} 失败: {data.get('errMsg') or data.get('message') or json_preview(data, 200)}")
    return data


def h5_request(
    server: str,
    session: Dict[str, str],
    method: str,
    path: str,
    proxies: Dict[str, str] | None,
    params: Dict[str, Any] | None = None,
    payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    url = f"{H5_API}{path}"
    if method.upper() == "GET":
        response = request_with_proxy("GET", url, headers=h5_headers(session), params=params, proxies=proxies, server=server)
    else:
        response = request_with_proxy("POST", url, headers=h5_headers(session), json=payload if payload is not None else {}, proxies=proxies, server=server)

    if response.status_code != 200:
        raise Exception(f"{path} HTTP {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
    except Exception:
        raise Exception(f"{path} 响应非JSON: {response.text[:300]}")

    if not (data.get("code") == 200 or data.get("succeed") is True):
        raise Exception(f"{path} 失败: {data.get('message') or data.get('errorMessage') or json_preview(data, 200)}")
    return data


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


def set_cached_token(server: str, token: str, expire_time: str, extra: Dict[str, Any] | None = None) -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    if extra:
        cache[server].update(extra)
    save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（会员信息接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            entry = load_token_cache().get(server, {}) or {}
            session = {
                "sessionId": cache_token,
                "encryptedSession": str(entry.get("encryptedSession") or ""),
                "openId": str(entry.get("openId") or ""),
            }
            member_resp = mini_request(server, session, "GET", "/member/info", proxies, params={"sessionId": cache_token})
            if str(member_resp.get("ret")) == "1":
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

    expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    set_cached_token(server, token, expire_time, extra=raw_login if isinstance(raw_login, dict) else None)
    return token, raw_login


def query_member(server: str, session: Dict[str, str], proxies: Dict[str, str] | None) -> Dict[str, Any]:
    member_resp = mini_request(server, session, "GET", "/member/info", proxies, params={"sessionId": session.get("sessionId", "")})
    member = safe_data(member_resp)
    base: Dict[str, Any] = {}
    try:
        base_resp = mini_request(server, session, "GET", "/member/baseInfo", proxies, params={"sessionId": session.get("sessionId", "")})
        base = safe_data(base_resp)
    except Exception:
        pass

    user_name = member.get("userName") or base.get("userName") or "未知"
    phone = base.get("pnumber") or ""
    point_amount = member.get("pointAmount") if member.get("pointAmount") is not None else 0
    growth_value = member.get("growthValue") if member.get("growthValue") is not None else 0
    grade_code = member.get("gradeCode") or "未知"
    print(f"👤 [会员] 用户: {user_name}，积分: {point_amount}，成长值: {growth_value}，等级: {grade_code}")
    if phone:
        print(f"👤 [会员] 手机号: {phone}")
    return member


def query_entrance(server: str, session: Dict[str, str], proxies: Dict[str, str] | None) -> None:
    result = mini_request(server, session, "GET", "/activity/signIn/entrance", proxies, params={"sessionId": session.get("sessionId", "")})
    data = safe_data(result)
    started = "已开启" if data.get("signInIsStarted") else "未开启"
    sign_days = data.get("signInDays") if data.get("signInDays") is not None else "-"
    print(f"🚪 [签到入口] {started}，连续/累计天数: {sign_days}")


def discover_sign_activity(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str | None]:
    """从签到 H5 页发现当期活动 id（只读），失败返回 None 回落内置 id"""
    try:
        response = request_with_proxy(
            "GET",
            SIGN_PAGE,
            headers={
                "User-Agent": H5_USER_AGENT,
                "Accept": "text/html,*/*",
                "Referer": SIGN_PAGE,
            },
            proxies=proxies,
            server=server,
        )
        html = response.text if response.status_code == 200 else ""
        if not html:
            print(f"⚠️ [签到活动] 页面不可读 HTTP {response.status_code}，回落到内置 id")
            return None, None

        anchor = re.search(r'"type"\s*:\s*"SignIn"', html)
        if not anchor:
            print("⚠️ [签到活动] 页面里没有 SignIn 楼层（活动可能真的下线了），回落到内置 id")
            return None, None

        segment = html[anchor.start():anchor.start() + 2000]
        id_match = re.search(r'"activityInfo"\s*:\s*\{[^{}]*"activityId"\s*:\s*"(\d+)"', segment)
        name_match = re.search(r'"activityName"\s*:\s*"((?:[^"\\]|\\.)*)"', segment)
        action_match = re.search(r'"creditsAddActionId"\s*:\s*"([0-9a-f]{32})"', segment)
        if not id_match:
            print("⚠️ [签到活动] SignIn 楼层里没有 activityId，回落到内置 id")
            return None, None

        name = name_match.group(1) if name_match else ""
        print(f"✅ [签到活动] {name or '未命名'} (activityId={id_match.group(1)})")
        return id_match.group(1), (action_match.group(1) if action_match else None)
    except Exception as exc:
        print(f"⚠️ [签到活动] 发现失败: {exc}，回落到内置 id")
        return None, None


def award_type_name(award_type: Any) -> str:
    mapping = {0: "无奖励", 1: "积分", 2: "优惠券", 3: "抽奖机会"}
    try:
        return mapping.get(int(award_type), f"类型{award_type}")
    except (TypeError, ValueError):
        return f"类型{award_type}"


def get_sign_detail(server: str, session: Dict[str, str], activity_id: str, action_id: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    result = h5_request(server, session, "GET", "/api/cn/oapi/marketing/cumulativeSignIn/getSignInDetail", proxies, params={
        "activityId": activity_id,
        "creditsAddActionId": action_id,
        "business": BUSINESS,
    })
    return safe_data(result)


def query_sign_detail(server: str, session: Dict[str, str], activity_id: str, action_id: str, proxies: Dict[str, str] | None) -> Tuple[Dict[str, Any], bool]:
    detail = get_sign_detail(server, session, activity_id, action_id, proxies)
    awards = detail.get("baseAwards") if isinstance(detail.get("baseAwards"), list) else []

    today = datetime.now().strftime("%Y-%m-%d")
    award: Dict[str, Any] = {}
    for item in awards:
        if isinstance(item, dict) and str(item.get("signTime") or "")[:10] == today:
            award = item
            break
    if not award and awards and isinstance(awards[0], dict):
        award = awards[0]

    signed = to_float(award.get("status")) == 1
    award_desc = str(award.get("awardValue", "-"))
    if award.get("awardType") is not None:
        award_desc += award_type_name(award.get("awardType"))
    sign_day_num = detail.get("signInDayNum") if detail.get("signInDayNum") is not None else 0
    state_text = "今日已签" if signed else "今日未签"
    print(f"📅 [签到详情] {state_text}，已签天数: {sign_day_num}，今日奖励: {award_desc}")
    return detail, signed


def sign_in(server: str, session: Dict[str, str], activity_id: str, action_id: str, proxies: Dict[str, str] | None) -> str:
    detail, signed = query_sign_detail(server, session, activity_id, action_id, proxies)
    if signed:
        print("✅ [签到] 今日已签到，跳过")
        return "今日已签到"

    result = h5_request(server, session, "POST", "/api/cn/oapi/marketing/cumulativeSignIn/signIn", proxies, payload={
        "activityId": activity_id,
        "captchaCode": "",
        "creditsAddActionId": action_id,
        "business": BUSINESS,
    })
    data = safe_data(result)
    if data.get("receiveStatus") is False:
        msg = data.get("receiveFailMsg") or result.get("message") or "未知原因"
        print(f"❌ [签到] 签到失败: {msg}")
        return f"签到失败: {msg}"

    award_desc = str(data.get("awardValue", "-"))
    if data.get("awardType") is not None:
        award_desc += award_type_name(data.get("awardType"))
    print(f"✅ [签到] 签到成功，获得 {award_desc}")
    query_sign_detail(server, session, activity_id, action_id, proxies)
    return f"签到成功，获得 {award_desc}"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "memberMsg": "-",
        "signMsg": "-",
        "pointsMsg": "-",
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
        entry = load_token_cache().get(server, {}) or {}
        session = {
            "sessionId": token,
            "encryptedSession": str(entry.get("encryptedSession") or ""),
            "openId": str(entry.get("openId") or ""),
        }

        member = query_member(server, session, proxies)
        result["memberMsg"] = f"{member.get('userName') or '未知'} / 等级 {member.get('gradeCode') or '未知'}"

        query_entrance(server, session, proxies)

        activity_id, action_id = discover_sign_activity(server, proxies)
        if not activity_id:
            activity_id = FALLBACK_SIGN_ACTIVITY_ID
            print(f"⚠️ [签到活动] 使用内置 activityId: {activity_id}（活动 id 按月轮换，若提示活动已结束属正常，等新一期）")
        if not action_id:
            action_id = FALLBACK_CREDITS_ADD_ACTION_ID

        sign_msg = sign_in(server, session, activity_id, action_id, proxies)
        result["signMsg"] = sign_msg

        member = query_member(server, session, proxies)
        point_amount = member.get("pointAmount") if member.get("pointAmount") is not None else 0
        result["pointsMsg"] = str(point_amount)

        result["success"] = sign_msg == "今日已签到" or sign_msg.startswith("签到成功")
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""📱 OPPO 小程序会员任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
👤 会员：{res["memberMsg"]}
📝 签到：{res["signMsg"]}
💰 积分：{res["pointsMsg"]}
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
                "memberMsg": "-",
                "signMsg": "-",
                "pointsMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 OPPO 小程序会员任务执行完成               ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("📱 OPPO 小程序会员任务完成", build_notify(results))


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
