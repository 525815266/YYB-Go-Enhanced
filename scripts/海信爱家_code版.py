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
海信爱家AIoT小程序动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. 手机号授权登录 /weixintv/oauth/login4MiniAPPByPhone（需 wx_server 手机号授权 code）
  3. 用户信息查询（AES 解密手机号）
  4. 查询总积分 / 今日积分记录
  5. 每日签到（编排 sceneCode 动态解析签到 taskId）
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN       PushPlus token，可选
  QYWX_TOKEN           企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API            品赞代理提取 API，可选
  PROXY_TYPE           http / socks5，默认 http
  HISENSE_AIJIA_TOKEN  可选抓包兜底，格式 customerId#accessToken[#refreshToken]（按账号序号对齐）
  HISENSE_AIJIA_SIGN_TASK_ID  可选，固定签到 taskId（跳过编排解析）

⚠️ 登录依赖手机号授权 code（wx_server /wx/getphonenumber），若 code 服务不支持该功能，
   首次账号将无法登录；已配置 customerId#accessToken 抓包值或本地缓存有效时不受影响。

依赖：
  pip install requests pycryptodome
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
import uuid as _uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, urlencode

import requests

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None


APP_NAME = "海信爱家AIoT小程序"
APPID = "wxf488d623a17cd7b5"

SERVERS = [
    "127.0.0.1:8088",
]
if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
FIXED_SIGN_TASK_ID = os.getenv("HISENSE_AIJIA_SIGN_TASK_ID", "")

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

SIGN_APP_KEY = "commonweb"
SIGN_APP_SECRET = "MORZRbkuiWxjp+SM4vR_GxY4pZxLZ6rn"
MINI_ID = "105"
APP_PACKAGE = "com.hisense.miniapp-aiot"
APP_VERSION = "m_p.16.000"
DEFAULT_VALUE = "-1"
DEFAULT_LICENSE = "-1"
SOURCE_TYPE = 21
FEATURE_CODE = "86100300000100100000fffe"
PACKAGE_VERSION = "115"

POINT_BASE = "https://mobile-aiot.hismarttv.com"
WXTV_BASE = "https://public-wxtv.hismarttv.com"
MINI_MOBI_BASE = "https://mini-mobi.hismarttv.com"
ACCOUNT_BASE = "https://portal-account.hismarttv.com"
PROFILE_URL = f"{POINT_BASE}/MobileMiniAppAPI/s/6.3/account/getCustomerProfile"
POINTS_URL = f"{POINT_BASE}/AIoTPointsMall/gw/svc/HiScore/1.0/userPoints"
RECORDS_URL = f"{POINT_BASE}/AIoTPointsMall/gw/svc/HiScore/1.0/userPointRecords"
SCENE_URL = f"{WXTV_BASE}/vodapptv/5.10/sceneParams/data/get"
SIGN_STATUS_URL = f"{POINT_BASE}/AIoTPointsMall/gw/svc/HiVip/1.0/getCheckInStatus"
SIGN_IN_URL = f"{POINT_BASE}/AIoTPointsMall/gw/svc/HiVip/1.0/checkIn"
REFRESH_TOKEN_URL = f"{MINI_MOBI_BASE}/MobileMiniAppAPI/1.2/adapter/refreshToken"
PHONE_LOGIN_URL = f"{WXTV_BASE}/weixintv/oauth/login4MiniAPPByPhone"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hxajcookie.json")

USER_AGENT = "Mozilla/5.0 MicroMessenger MiniProgramEnv/Windows"


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
    print("║ 🏠 海信爱家AIoT小程序动态 code 版           ║")
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


def md5_hex(text: str) -> str:
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()


def rand_hex(length: int = 4) -> str:
    return format(random.randint(0, 16 ** length - 1), "x")


def pure_guid() -> str:
    return str(_uuid.uuid4()).replace("-", "")


def build_device_id() -> str:
    identifier = f"0{MINI_ID}0{format(int(time.time() * 1000), 'x')}{rand_hex(8)}{rand_hex(8)}"[:32]
    return f"{FEATURE_CODE}{identifier}"


def sign_string(data: Any, post_and_json: bool = False) -> str:
    """appKey/secret 签名：MD5(text + secret) 转 base64"""
    if post_and_json:
        text = data if isinstance(data, str) else json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(data, str):
        text = data
    else:
        parts = []
        for key, value in (data or {}).items():
            if value == "" or value is None:
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            parts.append(f"{key}={value}")
        text = "&".join(sorted(parts))
    return base64.b64encode(hashlib.md5(f"{text}{SIGN_APP_SECRET}".encode("utf-8")).digest()).decode("utf-8")


def encrypt_by_app_key(value: str) -> str:
    """AES-256-CBC，key=iv=secret（前16字节为 iv），base64 输出"""
    if not value or AES is None:
        return ""
    try:
        cipher = AES.new(SIGN_APP_SECRET.encode("utf-8"), AES.MODE_CBC, SIGN_APP_SECRET[:16].encode("utf-8"))
        raw = value.encode("utf-8")
        pad_len = 16 - len(raw) % 16
        padded = raw + bytes([pad_len]) * pad_len
        return base64.b64encode(cipher.encrypt(padded)).decode("utf-8")
    except Exception:
        return ""


def decrypt_by_app_key(value: str) -> str:
    if not value or AES is None:
        return ""
    try:
        decipher = AES.new(SIGN_APP_SECRET.encode("utf-8"), AES.MODE_CBC, SIGN_APP_SECRET[:16].encode("utf-8"))
        raw = base64.b64decode(value)
        decrypted = decipher.decrypt(raw)
        pad_len = decrypted[-1]
        return decrypted[:-pad_len].decode("utf-8")
    except Exception:
        return value


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/{PACKAGE_VERSION}/page-frame.html",
    }
    if token:
        headers["token"] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        token_info = inner.get("tokenInfo")
        if isinstance(token_info, dict):
            candidates.extend([token_info.get("token"), token_info.get("accessToken")])
        candidates.extend([
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def extract_customer_id(data: Any) -> str:
    if not isinstance(data, dict):
        return ""

    for key in ("customerId", "customer_id", "userId"):
        value = data.get(key)
        if value:
            return str(value)

    for key in ("data",):
        inner = data.get(key)
        if isinstance(inner, dict):
            token_info = inner.get("tokenInfo")
            if isinstance(token_info, dict):
                value = token_info.get("customerId")
                if value:
                    return str(value)
            for key2 in ("customerId", "customer_id", "userId"):
                value = inner.get(key2)
                if value:
                    return str(value)

    return ""


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """⚠️ 登录接口为推断：mini程序静默登录接口未在源脚本出现，按 login4MiniAPPByPhone 的
    兄弟接口 login4MiniAPP 推断实现；失败请抓包核对"""
    try:
        print("🔐 [登录] 使用 code 换 token（推断接口 login4MiniAPP）")
        now_ms = int(time.time() * 1000)
        payload = {
            "deviceId": build_device_id(),
            "code": code,
            "miniId": MINI_ID,
            "sid": str(_uuid.uuid4()),
        }
        payload["sign"] = sign_string(payload, True)
        payload["appKey"] = SIGN_APP_KEY
        response = request_with_proxy(
            "POST",
            f"{WXTV_BASE}/weixintv/oauth/login4MiniAPP?_t={now_ms}",
            headers=common_headers(),
            json=payload,
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

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def is_token_error(data: Any) -> bool:
    text = json_preview(data, 1000) or ""
    code = ""
    if isinstance(data, dict):
        code = str(data.get("resultCode") if data.get("resultCode") is not None else data.get("code", ""))
    return bool(__import__("re").search(r"token|登录|未登录|失效|access", text)) or code in ("A00001", "B0101", "B0102", "401", "401")


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


def signed_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any], gateway: bool = True) -> Dict[str, Any]:
    """业务签名 POST：gateway=True 时 X-Sign-For 头签名，否则 body 内 sign 字段"""
    now_ms = int(time.time() * 1000)
    body = {"_t": now_ms}
    body.update(payload)
    headers = common_headers(token)

    if gateway:
        headers["X-Sign-For"] = sign_string(body, True)
        headers["appKey"] = SIGN_APP_KEY
    else:
        body["sign"] = sign_string(body, True)
        body["appKey"] = SIGN_APP_KEY

    response = request_with_proxy(
        "POST",
        url,
        headers=headers,
        json=body,
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


def signed_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, params: Dict[str, Any]) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    query = {"_t": now_ms}
    query.update(params)
    headers = common_headers(token)
    headers["X-Sign-For"] = sign_string(query, True)
    headers["appKey"] = SIGN_APP_KEY

    response = request_with_proxy(
        "GET",
        f"{url}?{urlencode(query)}",
        headers=headers,
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


def get_cached_token(server: str) -> Dict[str, Any] | None:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} token")
                return data
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, token: str, customer_id: str = "") -> None:
    cache = load_token_cache()
    cache[server] = {
        "token": token,
        "customerId": customer_id,
        "expireTime": datetime.fromtimestamp(time.time() + 24 * 3600).isoformat(),
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def verify_token(server: str, token: str, proxies: Dict[str, str] | None) -> bool:
    now_s = int(time.time())
    resp = signed_get(server, PROFILE_URL, token, proxies, {
        "accessToken": token,
        "timeStamp": now_s,
        "randStr": pure_guid(),
    })
    return str(resp.get("resultCode", "1")) == "0"


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str]:
    """优先使用缓存 token（用户信息接口验证），失效自动 code 刷新；支持抓包兜底"""
    cached = get_cached_token(server)
    if cached:
        print("🔍 [缓存] 验证 token")
        try:
            if verify_token(server, cached["token"], proxies):
                print("✅ [缓存] token 有效")
                return cached["token"], cached.get("customerId", "")
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    env_token = os.getenv("HISENSE_AIJIA_TOKEN", "")
    accounts = [p.strip() for p in env_token.replace("\n", "&").split("&") if p.strip()]
    account_index = SERVERS.index(server) if server in SERVERS else 0
    if account_index < len(accounts):
        parts = accounts[account_index].split("#")
        if len(parts) >= 2:
            print(f"🔑 [登录] 使用 HISENSE_AIJIA_TOKEN 抓包凭证: {mask(parts[1])}")
            set_cached_token(server, parts[1], parts[0])
            return parts[1], parts[0]

    code = get_code(server)
    if not code:
        return None, ""

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, ""

    customer_id = extract_customer_id(raw_login or {})
    if not customer_id:
        print("⚠️ [登录] 未返回 customerId，积分接口可能失败")

    set_cached_token(server, token, customer_id)
    return token, customer_id


def query_points(server: str, token: str, customer_id: str, proxies: Dict[str, str] | None) -> int:
    payload = {
        "customerId": str(customer_id),
        "mobile": "",
        "accessToken": token,
        "appType": MINI_ID,
    }
    resp = signed_post(server, POINTS_URL, token, proxies, payload)
    if str(resp.get("resultCode", "1")) != "0":
        print(f"⚠️ [积分] 查询失败: {json_preview(resp, 300)}")
        return 0
    total = safe_data(resp).get("totalScore", 0)
    print(f"💰 [积分] 总积分: {total}")
    return int(to_float(total))


def query_today_records(server: str, token: str, customer_id: str, proxies: Dict[str, str] | None) -> int:
    now = datetime.now()
    start = int(datetime(now.year, now.month, now.day).timestamp() * 1000)
    end = start + 24 * 3600 * 1000 - 1
    payload = {
        "customerId": str(customer_id),
        "mobile": "",
        "accessToken": token,
        "appType": MINI_ID,
        "pageNo": "1",
        "pageSize": "100",
        "startTime": start,
        "endTime": end,
    }
    resp = signed_post(server, RECORDS_URL, token, proxies, payload)
    if str(resp.get("resultCode", "1")) != "0":
        return 0
    entities = safe_data(resp).get("entities")
    if not isinstance(entities, list):
        entities = []
    income = sum(int(to_float(item.get("score"))) for item in entities if isinstance(item, dict) and str(item.get("type")) == "1")
    print(f"💰 [积分] 今日获得积分: {income}")
    return income


def get_sign_task_id(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    if FIXED_SIGN_TASK_ID:
        return FIXED_SIGN_TASK_ID
    resp = signed_get(server, SCENE_URL, token, proxies, {
        "accessToken": token,
        "type": 2,
        "sceneCode": "STATIC_AILIFE_SIGN_INFO",
    })
    if str(resp.get("resultCode", "1")) != "0":
        print(f"⚠️ [签到] 编排查询失败: {json_preview(resp, 300)}")
        return ""

    def find_task_id(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        mp_sign = value.get("mpSignInfo")
        if isinstance(mp_sign, dict) and mp_sign.get("taskId"):
            return str(mp_sign["taskId"])
        for inner in value.values():
            found = find_task_id(inner)
            if found:
                return found
        return ""

    task_id = find_task_id(resp.get("data") or resp)
    if task_id:
        print(f"✅ [签到] 签到任务ID: {task_id}")
    else:
        print("⚠️ [签到] 未解析到签到 taskId")
    return task_id


def query_sign_status(server: str, token: str, customer_id: str, task_id: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    payload = {
        "deviceId": build_device_id(),
        "appPackageName": APP_PACKAGE,
        "appVersionName": DEFAULT_VALUE,
        "appVersionCode": DEFAULT_VALUE,
        "license": DEFAULT_LICENSE,
        "appVersion": APP_VERSION,
        "deviceExt": "Windows",
        "userType": "1",
        "customerId": str(customer_id),
        "mobile": "",
        "accessToken": token,
        "userId": str(customer_id),
        "taskId": task_id,
        "sourceType": SOURCE_TYPE,
    }
    resp = signed_post(server, SIGN_STATUS_URL, token, proxies, payload)
    if str(resp.get("code", "1")) != "0":
        print(f"⚠️ [签到] 状态查询失败: {json_preview(resp, 300)}")
        return {}
    status = resp.get("checkInStatus") or {}
    signed_today = "已签" if status.get("signedToday") else "未签"
    days = status.get("keepSigningDays", 0)
    print(f"📅 [签到] 今日{signed_today}，连续{days}天")
    return status


def do_sign(server: str, token: str, customer_id: str, task_id: str, status: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
    if status.get("signedToday"):
        print("✅ [签到] 今日已签到")
        return "今日已签到"

    rewards = status.get("rewardSignTaskList")
    days = int(to_float(status.get("keepSigningDays", 0)))
    reward = {}
    if isinstance(rewards, list) and rewards:
        reward = rewards[max(0, days)] if days < len(rewards) else rewards[0]
    if reward and reward.get("rewardType") is not None and str(reward.get("rewardType")) != "130":
        print("⚠️ [签到] 当前奖励类型异常，跳过签到")
        return "奖励类型异常，跳过"

    now_ms = int(time.time() * 1000)
    payload = {
        "deviceId": build_device_id(),
        "appPackageName": APP_PACKAGE,
        "appVersionName": DEFAULT_VALUE,
        "appVersionCode": DEFAULT_VALUE,
        "license": DEFAULT_LICENSE,
        "appVersion": APP_VERSION,
        "deviceExt": "Windows",
        "userType": "1",
        "customerId": str(customer_id),
        "mobile": "",
        "accessToken": token,
        "userId": str(customer_id),
        "returnCheckInStatus": False,
        "requestId": f"ailife_sign-{now_ms}-{rand_hex()}",
        "taskId": task_id,
        "reportToGroup": 1,
        "requestTime": str(now_ms),
    }
    resp = signed_post(server, SIGN_IN_URL, token, proxies, payload)
    if str(resp.get("code", "1")) == "0":
        msg = f"签到成功，预计获得{reward.get('rewardNum', '')}积分" if reward.get("rewardNum") else "签到成功"
        print(f"✅ [签到] {msg}")
        return msg

    message = resp.get("message") or resp.get("msg") or json_preview(resp, 300)
    import re as _re
    if _re.search(r"已签|重复|today", str(message)):
        print(f"✅ [签到] 今日已签到: {message}")
        return "今日已签到"
    print(f"❌ [签到] 签到失败: {message}")
    return f"签到失败: {message}"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
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

    token, customer_id = login_with_cache(server, proxies)
    if not token:
        result["error"] = "登录失败: 未获取到 accessToken"
        return result

    result["token"] = mask(token)

    try:
        query_points(server, token, customer_id, proxies)
        query_today_records(server, token, customer_id, proxies)

        task_id = get_sign_task_id(server, token, proxies)
        if task_id:
            status = query_sign_status(server, token, customer_id, task_id, proxies)
            result["signMsg"] = do_sign(server, token, customer_id, task_id, status, proxies)
        else:
            result["signMsg"] = "未获取到签到 taskId"

        total_score = query_points(server, token, customer_id, proxies)
        result["pointsMsg"] = f"总积分 {total_score}"

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🏠 海信爱家AIoT任务结果

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
    print("║ 🏁 海信爱家AIoT任务执行完成                ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🏠 海信爱家AIoT任务完成", build_notify(results))


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
