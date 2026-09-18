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
红色火箭（华夏基金）小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /fundex-uc/uc/v1/getWxOpenIdAndUnionId 用 code 换 openId
  3. /fundex-uc/uc/v1/login 登录换 ticket（需 getPhoneNumber code，见限制说明）
  4. 查询签到日历（getRecordList）
  5. 每日签到（userSignIn）
  6. 查询总积分
  7. Ticket 本地缓存与自动刷新
  8. PushPlus 推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

⚠️ 登录限制（照源脚本如实说明）：
   - 全包唯一发 ticket 的入口 /fundex-uc/uc/v1/login 只接受 wx.getPhoneNumber
     的 code。本地 code 服务若支持手机号授权码可换取 ticket；若服务端
     返回 loginStatus=fail，请先在小程序内手动登录一次。
   - 请求签名需要 appSecret（wx.getUserCryptoManager 的 encryptKey），本地
     code 服务无法提供，code 版按无 appSecret 方式签名（签名串不含该字段），
     若服务端校验签名将失败，属已知限制。

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

import base64
import hashlib
import json
import os
import random
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "红色火箭小程序"
APPID = "wx1b44c3ad181bde16"

SERVERS = ["127.0.0.1:8088"]

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

BASE_URL = "https://index.amcfortune.com"
OPENID_URL = f"{BASE_URL}/fundex-uc/uc/v1/getWxOpenIdAndUnionId"
LOGIN_URL = f"{BASE_URL}/fundex-uc/uc/v1/login"
TOKEN_STATUS_URL = f"{BASE_URL}/fundex-uc/uc/v1/getTokenStatus"
SIGN_RECORD_URL = f"{BASE_URL}/fundex-activity/point/sign/getRecordList"
SIGN_IN_URL = f"{BASE_URL}/fundex-activity/point/sign/userSignIn"
TOTAL_POINT_URL = f"{BASE_URL}/fundex-activity/point/account/getTotalPoint"

PRO = "RedRocket"
PLA = "rr_Android"
VERSION = "1.46.0"
AGREEMENT = "阅读并同意用户协议、隐私政策，未注册的手机号认证后自动创建新账户"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hshjcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541923) XWEB/19823"
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
    print("║ 🚀 红色火箭动态 code 版                       ║")
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


def get_code(server: str, phone_code: bool = False) -> str | None:
    """本地 code 服务取码；phone_code=True 时请求手机号授权 code 端点"""
    endpoint = "/getphonenumber" if phone_code else "/login"
    url = f"http://{server}{endpoint}"
    print(f"🔐 [授权] 请求本地 code 服务: {url}")

    try:
        response = direct_session().get(
            f"http://{server}/login",
            params={"appId": APPID, "type": "getphonenumber" if phone_code else "login"},
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


def js_str(value: Any) -> str:
    """复刻 JS 字符串拼接语义，保证签名串一致（照源脚本移植）"""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return ",".join(js_str(x) for x in value)
    return str(value)


def make_signature(body: Dict[str, Any], nonce: str, timestamp: str) -> str:
    """照源脚本：signature = Base64(md5_hex(键名升序 k=v& 拼接))（无 appSecret 版本，见限制说明）"""
    fields = dict(body or {})
    fields.setdefault("nonce", nonce)
    fields.setdefault("timestamp", timestamp)
    raw = "".join(f"{k}={js_str(fields[k])}&" for k in sorted(fields.keys()))[:-1]
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return base64.b64encode(digest.encode("utf-8")).decode("utf-8")


def make_nonce() -> str:
    """照源脚本："" + (1e6*random).toFixed(0) + (1e6*random).toFixed(0)"""
    return f"{random.randint(0, 1000000)}{random.randint(0, 1000000)}"


def common_headers(token: str | None = None, user_id: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "ticket": token or "",
        "Bank-Type": "main",
        "pro": PRO,
        "ver": VERSION,
        "pla": PLA,
        "register_channel": "",
        "click_id": "",
        "user_id": user_id if token else "",
        "mini_program": "wechat",
        "Referer": f"https://servicewechat.com/{APPID}/xxx/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    return headers


def is_ok(data: Any) -> bool:
    """源脚本响应判定：code 为 0 / 200 成功"""
    return isinstance(data, dict) and str(data.get("code", "")) in ("0", "200")


def err_msg(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("msg") or data.get("message") or json_preview(data, 200))
    return str(data)


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [data.get("token"), data.get("ticket"), data.get("accessToken")]

    inner = safe_data(data)
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("ticket"),
            inner.get("accessToken"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """先 code -> openId，再 getPhoneNumber code -> ticket（见 docstring 限制说明）"""
    global ACCOUNT_STATE

    try:
        print("🔐 [登录] 使用 code 换 openId")
        openid_resp = request_with_proxy(
            "POST",
            OPENID_URL,
            headers=common_headers(),
            json={"code": code},
            proxies=proxies,
            server=server,
        )
        try:
            openid_data = openid_resp.json()
        except Exception:
            openid_data = {"raw": openid_resp.text[:800]}

        if not is_ok(openid_data):
            print(f"❌ [登录] 获取 openId 失败: {err_msg(openid_data)}")
            return None, openid_data

        data = safe_data(openid_data)
        openid = str(data.get("openId") or "")
        unionid = str(data.get("unionId") or "")
        if not openid:
            print(f"❌ [登录] 响应未包含 openId: {json_preview(openid_data)}")
            return None, openid_data

        ACCOUNT_STATE["openid"] = openid
        print(f"✅ [登录] openId 获取成功: {mask(openid)}")

        print("🔐 [登录] 使用手机号授权 code 换 ticket")
        phone_code = get_code(server, phone_code=True)
        if not phone_code:
            return None, openid_data

        timestamp = str(int(time.time() * 1000))
        nonce = make_nonce()
        body = {
            "loginWay": "miniprogram",
            "platform": "mini_fundex",
            "code": phone_code,
            "openId": openid,
            "unionId": unionid,
            "signAgreement": AGREEMENT,
            "registerChannel": "",
        }
        headers = common_headers()
        headers.update({
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": make_signature(body, nonce, timestamp),
            "key_version": "",
            "openid": openid,
        })
        login_resp = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=headers,
            json=body,
            proxies=proxies,
            server=server,
        )
        try:
            login_data = login_resp.json()
        except Exception:
            login_data = {"raw": login_resp.text[:800]}

        if not is_ok(login_data):
            print(f"❌ [登录] 登录失败: {err_msg(login_data)}")
            return None, login_data

        inner = safe_data(login_data)
        if str(inner.get("loginStatus", "")).lower() != "success":
            print(f"❌ [登录] {inner.get('loginDesc') or err_msg(login_data)}")
            return None, login_data

        token = inner.get("token") or ""
        if not token:
            print(f"❌ [登录] 登录成功但未返回 token: {json_preview(login_data)}")
            return None, login_data

        ACCOUNT_STATE["userId"] = str(inner.get("userId") or "")
        print(f"✅ [登录] ticket 获取成功: {mask(token)}")
        return str(token), login_data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


ACCOUNT_STATE: Dict[str, Any] = {"openid": "", "userId": ""}


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    query = dict(params or {})
    query["key"] = int(time.time() * 1000)
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token, ACCOUNT_STATE.get("userId", "")),
        params=query,
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
    nonce = make_nonce()
    timestamp = str(int(time.time() * 1000))
    headers = common_headers(token, ACCOUNT_STATE.get("userId", ""))
    headers.update({
        "nonce": nonce,
        "timestamp": timestamp,
        "signature": make_signature(payload, nonce, timestamp),
        "key_version": "",
        "openid": str(ACCOUNT_STATE.get("openid") or ""),
    })
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
            "msg": f"JSON解析失败: {response.text[:300]}",
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
                print(f"✅ [缓存] 使用 {server} ticket")
                return data["token"]
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, token: str, expire_time: str, user_id: str = "", openid: str = "") -> None:
    cache = load_token_cache()
    cache[server] = {
        "token": token,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
        "userId": user_id,
        "openId": openid,
    }
    save_token_cache(cache)


def check_ticket(server: str, ticket: str, proxies: Dict[str, str] | None) -> bool:
    """照源脚本 getTokenStatus：data.status=="0" 有效，newToken 轮换"""
    try:
        resp = api_post(server, TOKEN_STATUS_URL, ticket, proxies, {"token": ticket, "source": "miniProgram"})
        if not is_ok(resp):
            return False
        data = safe_data(resp)
        if str(data.get("status", "")) != "0":
            return False
        if data.get("newToken"):
            cache = load_token_cache()
            entry = cache.get(server) or {}
            entry["token"] = data["newToken"]
            entry["updateTime"] = datetime.now().isoformat()
            cache[server] = entry
            save_token_cache(cache)
            print("✅ [缓存] ticket 已轮换")
        if data.get("userId"):
            ACCOUNT_STATE["userId"] = str(data["userId"])
        return True
    except Exception as exc:
        print(f"⚠️ [缓存] 校验 ticket 异常: {exc}")
        return False


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 ticket（getTokenStatus 接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        entry = load_token_cache().get(server) or {}
        ACCOUNT_STATE["openid"] = str(entry.get("openId") or "")
        ACCOUNT_STATE["userId"] = str(entry.get("userId") or "")
        print("🔍 [缓存] 验证 ticket")
        if check_ticket(server, cache_token, proxies):
            print("✅ [缓存] ticket 有效")
            return cache_token, None
        print("⚠️ [缓存] ticket 已失效，重新登录")

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
    set_cached_token(server, token, expire_time, ACCOUNT_STATE.get("userId", ""), ACCOUNT_STATE.get("openid", ""))
    return token, raw_login


def parse_sign_state(data: Any) -> Tuple[Any, Any, Any]:
    """照源脚本：从 getRecordList 响应提取今日是否已签、连续天数与今日积分"""
    signed, days, today_point = None, None, None
    if isinstance(data, dict):
        days = data.get("continuousDays")
        today = data.get("today")
        records = data.get("signRecordList")
        if isinstance(records, list):
            for item in records:
                if not isinstance(item, dict):
                    continue
                if today and item.get("signDate") != today:
                    continue
                value = item.get("signIn")
                if isinstance(value, bool):
                    signed = value
                elif isinstance(value, (int, str)) and str(value) in ("0", "1"):
                    signed = str(value) == "1"
                today_point = item.get("point")
                break
    return signed, days, today_point


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "pointMsg": "-",
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
        # 签到日历
        record_resp = api_get(server, SIGN_RECORD_URL, token, proxies)
        signed, days, today_point = None, None, None
        if is_ok(record_resp):
            signed, days, today_point = parse_sign_state(safe_data(record_resp))
            print(f"🔍 [签到] 今日已签={signed} 连续天数={days} 今日可得={today_point}")
        else:
            print(f"⚠️ [签到] 查询签到状态失败: {err_msg(record_resp)}")

        if signed is True:
            msg = f"今日已签到{f'，连续 {days} 天' if days is not None else ''}"
            result["signMsg"] = msg
            print(f"✅ [签到] {msg}")
        else:
            wait_time = random.randint(2, 5)
            print(f"⏳ [签到] 签到前等待 {wait_time}s")
            sleep(wait_time)

            sign_resp = api_post(server, SIGN_IN_URL, token, proxies, {})
            if is_ok(sign_resp):
                data = safe_data(sign_resp)
                gained = None
                if isinstance(data, dict):
                    gained = data.get("point") or data.get("points") or data.get("addPoint")
                gained = gained or today_point
                result["signMsg"] = "签到成功" + (f"，+{gained} 积分" if gained else "")
                print(f"🎉 [签到] {result['signMsg']}")
            else:
                detail = err_msg(sign_resp)
                if any(k in detail for k in ("已签", "重复", "已参与", "已领取")):
                    result["signMsg"] = "今日已签到"
                    print(f"✅ [签到] {result['signMsg']}（{detail}）")
                else:
                    result["signMsg"] = f"签到失败: {detail}"
                    print(f"❌ [签到] {result['signMsg']}")

        sleep(random.randint(1, 3))

        # 总积分
        point_resp = api_get(server, TOTAL_POINT_URL, token, proxies)
        if is_ok(point_resp):
            data = safe_data(point_resp)
            point_value = None
            if isinstance(data, dict):
                for key in ("totalPoint", "total", "point", "points", "balance"):
                    if data.get(key) is not None:
                        point_value = data[key]
                        break
            else:
                point_value = data
            result["pointMsg"] = f"当前积分: {point_value if point_value is not None else '未知'}"
            print(f"💰 [积分] {result['pointMsg']}")
        else:
            result["pointMsg"] = err_msg(point_resp)
            print(f"⚠️ [积分] {result['pointMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🚀 红色火箭任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
📝 签到：{res["signMsg"]}
💰 积分：{res["pointMsg"]}
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
                "pointMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 红色火箭任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🚀 红色火箭任务完成", build_notify(results))


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
