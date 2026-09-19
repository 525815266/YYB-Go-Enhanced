#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 兼容 GBK 终端：强制 stdout/stderr 使用 UTF-8（不影响排版与格式）
import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ========== 企业微信推送配置（可选） ==========
QYWX_TOKEN = __import__("os").getenv("QYWX_TOKEN", "")  # 企业微信机器人 Webhook key（机器人地址 ?key= 后面的值，留空不推送）

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
益禾堂小程序（企迈 qmai 平台 + 兑吧 duiba 活动）签到动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /account-center/oauth/mini-app-login 使用 code 换 qm-user-token
     （企迈全站 AES-GCM 加密契约，同平台实测脚本验证）
  3. member/redirect 获取兑吧活动落地页地址（取 302 Set-Cookie 会话）
  4. getToken 执行混淆 JS 动态计算签到 token（PyExecJS + 本机 JS 运行时）
  5. doSign 每日签到
  6. PushPlus / 企业微信推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests pycryptodome PyExecJS
  getToken 需执行混淆 JS，机器上要有可用 JS 运行时（如 node）
  socks5 代理需：
  pip install requests[socks]

⚠️ 源脚本为抓包 qm-user-token 型（无登录调用），登录采用企迈平台
   AES-GCM 加密登录契约（同平台 qmai 脚本实测通过）；源脚本 getToken 返回
   混淆 JS 需 eval 执行取 window['620fa72t']，本版改用 PyExecJS 执行。
"""

import base64
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

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None

try:
    import execjs
except ImportError:
    execjs = None


APP_NAME = "益禾堂小程序"
APPID = "wx4080846d0cec2fd5"

SERVERS = [
    "10.30.9.183:8088",
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

QMAI_BASE_URL = "https://webapi.qmai.cn/web"
QMAI_LOGIN_URL = f"{QMAI_BASE_URL}/account-center/oauth/mini-app-login"
QMAI_REDIRECT_URL = f"{QMAI_BASE_URL}/catering/crm/member/redirect"

ACTIVITY_PAGE_URL = "https://86019.activity-12.m.duiba.com.cn/chw/visual-editor/skins?id=203576"
ACTIVITY_TOKEN_URL = "https://86019-activity.dexfu.cn/chw/ctoken/getToken"
ACTIVITY_SIGN_URL = "https://86019-activity.dexfu.cn/sign/component/doSign"
SIGN_OPERATING_ID = "326649747164581"
STORE_ID = "203009"

# —— 企迈全站 AES-GCM 加密固定参数（源自解包 requestEncryptSdk，同平台脚本验证）——
KEY_RAW = "mN6KpXq8Sv2WxYz9LdFcRgHjMnBvCtDxZaS3QwE5rT0yU7I4O1A"
KEY_VERSION = "1.0.0"
META_HEADER = "QM-Encrypt-Meta"
CACHE_DIR = os.environ.get("CODE_CACHE_DIR", os.path.join(os.path.expanduser("~"), "Documents", "写代码"))

os.makedirs(CACHE_DIR, exist_ok=True)

COOKIE_FILE = os.path.join(CACHE_DIR, "yhtcookie.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf254173b) XWEB/19027"
)
# 签到页请求 UA（源脚本 doSign 使用）
SIGN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781 NetType/WIFI MiniProgramEnv/Windows "
    "WindowsWechat/WMPF XWEB/50249"
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
    print("║ 🧋 益禾堂签到动态 code 版                     ║")
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
    """企迈平台固定头（参考源脚本 redirect 请求头与同平台契约）。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "v=1.0",
        "Content-Type": "application/json",
        "xweb_xhr": "1",
        "qm-from-type": "catering",
        "qm-from": "wechat",
        "scene": "1101",
        "store-id": STORE_ID,
        "multi-store-id": "",
        "accept-language": "zh-CN",
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "Referer": f"https://servicewechat.com/{APPID}/517/page-frame.html",
    }
    if token:
        headers["qm-user-token"] = token
    return headers


# ========== 业务辅助函数（照源脚本） ==========
def b64relax(value: str) -> bytes:
    """宽松 base64 解码（自动补齐 padding）。"""
    return base64.b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def derive_key(raw: str) -> bytes:
    """解包 M()：宽松 base64 解码，非 32 字节则取前 32 补零。"""
    try:
        b = b64relax(raw)
    except Exception:
        b = raw.encode("utf-8")
    if len(b) == 32:
        return b
    out = bytearray(32)
    out[: min(len(b), 32)] = b[:32]
    return bytes(out)


def gcm_encrypt(plaintext: str, iv: bytes) -> str:
    """AES-256-GCM：返回 base64(ciphertext + 16字节tag)。"""
    cipher = AES.new(KEY, AES.MODE_GCM, nonce=iv)
    enc, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return base64.b64encode(enc + tag).decode("utf-8")


def gcm_decrypt(payload_b64: str, iv: bytes) -> str:
    buf = base64.b64decode(payload_b64)
    tag = buf[-16:]
    data = buf[:-16]
    cipher = AES.new(KEY, AES.MODE_GCM, nonce=iv)
    return cipher.decrypt_and_verify(data, tag).decode("utf-8")


KEY = derive_key(KEY_RAW)


def qmai_request(
    method: str,
    url: str,
    body: Dict[str, Any],
    token: str = "",
    extra_headers: Dict[str, str] | None = None,
    proxies: Dict[str, str] | None = None,
    server: str = "",
) -> Dict[str, Any]:
    """企迈加密请求：AES-GCM 请求体 + QM-Encrypt-Meta 头，响应加密时自动解密。"""
    if AES is None:
        return {"status": False, "code": -1, "message": "缺少 pycryptodome，请先 pip install pycryptodome"}

    payload_obj = dict(body or {})
    if not payload_obj.get("appid"):
        payload_obj["appid"] = APPID
    iv = os.urandom(12)
    ts = int(time.time() * 1000)
    meta = base64.b64encode(
        json.dumps({
            "version": KEY_VERSION,
            "timestamp": ts,
            "iv": base64.b64encode(iv).decode("utf-8"),
        }).encode("utf-8")
    ).decode("utf-8")

    headers = common_headers(token)
    headers[META_HEADER] = meta
    if extra_headers:
        headers.update(extra_headers)

    response = request_with_proxy(
        method,
        url,
        headers=headers,
        json={"payload": gcm_encrypt(json.dumps(payload_obj), iv)},
        proxies=proxies,
        server=server,
    )
    try:
        data = response.json()
    except Exception:
        return {"status": False, "code": -1, "message": f"JSON解析失败: {response.text[:300]}"}

    if isinstance(data, dict) and isinstance(data.get("payload"), str):
        rmeta = response.headers.get(META_HEADER) or response.headers.get(META_HEADER.lower())
        if not rmeta:
            return {"status": False, "code": -1, "message": "响应加密但缺少 QM-Encrypt-Meta"}
        try:
            meta_obj = json.loads(b64relax(rmeta).decode("utf-8"))
            return json.loads(gcm_decrypt(data["payload"], b64relax(meta_obj["iv"])))
        except Exception as exc:
            return {"status": False, "code": -1, "message": f"响应解密失败: {exc}"}

    return data


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

        user = inner.get("user")
        if isinstance(user, dict):
            candidates.extend([
                user.get("token"),
                user.get("accessToken"),
                user.get("access_token"),
                user.get("jwt"),
            ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """code 换 qm-user-token（照抓包 HAR 坐实的明文接口）

    HAR 实测：POST /web/account-center/oauth/mini-app-login
      body {"code":<wx.login code>,"eVersion":"1.0","appid":<APPID>}
      -> {"code":0,"data":{"token":"...","user":{...}}}
    这一步只需明文 JSON（无需 AES-GCM 加密），本地 code 服务完全可用。
    """
    try:
        print("🔐 [登录] 使用 code 换 qm-user-token（mini-app-login）")
        headers = dict(common_headers())
        headers.update({
            "Qm-From-Type": "catering",
            "Qm-From": "wechat",
            "store-id": STORE_ID,
            "Accept": "v=1.0",
        })
        response = request_with_proxy(
            "POST",
            QMAI_LOGIN_URL,
            headers=headers,
            json={"code": code, "eVersion": "1.0", "appid": APPID},
            proxies=proxies,
            server=server,
        )
        try:
            data = response.json()
        except Exception:
            return None, {"raw": response.text[:300]}
        if int(data.get("code") or 0) != 0:
            print(f"❌ [登录] 接口返回失败: {json_preview(data)}")
            return None, data

        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str | None, proxies: Dict[str, str] | None) -> Dict[str, Any]:
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
            "status": False,
            "code": -1,
            "message": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(
    server: str,
    url: str,
    token: str | None,
    proxies: Dict[str, str] | None,
    payload: Dict[str, Any],
    extra_headers: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """业务 POST：企迈接口走 AES-GCM 加密请求。"""
    return qmai_request(
        "POST",
        url,
        payload,
        token=token or "",
        extra_headers=extra_headers,
        proxies=proxies,
        server=server,
    )


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


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（member/redirect 接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            redirect_resp = api_post(server, QMAI_REDIRECT_URL, cache_token, proxies, {"redirectUrl": ACTIVITY_PAGE_URL})
            if redirect_resp.get("status") is True and redirect_resp.get("data"):
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


def fetch_activity_cookie(server: str, activity_url: str, proxies: Dict[str, str] | None) -> str:
    """访问活动落地页，取 302 Set-Cookie 中 wdata4/w_ts/_ac/wdata3/dcustom 组成会话。"""
    try:
        response = request_with_proxy(
            "GET",
            activity_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            proxies=proxies,
            server=server,
            allow_redirects=False,
        )
        set_cookies: List[str] = []
        raw = getattr(response, "raw", None)
        header_obj = getattr(raw, "headers", None)
        if header_obj is not None:
            try:
                set_cookies = header_obj.getlist("Set-Cookie")
            except Exception:
                set_cookies = []
        if not set_cookies:
            merged = response.headers.get("Set-Cookie", "")
            if merged:
                set_cookies = [merged]

        joined = "".join(set_cookies)
        parts = re.findall(r"(?:wdata4|w_ts|_ac|wdata3|dcustom)=[^;]*;", joined)
        if not parts:
            print(f"⚠️ [活动] 未提取到活动 Cookie: {json_preview(set_cookies, 300)}")
            return ""
        if len(parts) < 5:
            print(f"⚠️ [活动] 活动 Cookie 不完整（{len(parts)}/5），继续尝试")
        print("✅ [活动] 获取活动 token（Cookie）成功")
        return "".join(parts)
    except Exception as exc:
        print(f"❌ [活动] 获取活动 Cookie 异常: {exc}")
        return ""


def get_activity_key(server: str, session_cookie: str, proxies: Dict[str, str] | None) -> str:
    """getToken：返回混淆 JS，执行后取 window['3fd0cbet']（HAR 抓包坐实的固定键）

    逆向结论（ProxyPin 抓包 + Node 执行验证）：
      · 服务端返回的混淆 JS 会在浏览器里 eval 出一串 window[k]=v 赋值
      · 其中固定键 window['3fd0cbet'] 的值就是 doSign 需要的 token
      · 该键在多次请求中稳定不变（实测两次均为同一键名）
      · 优先用 execjs/Node 执行；若不可用，则退化用正则从 eval 产物里提取
    """
    ts = int(time.time() * 1000)
    try:
        response = request_with_proxy(
            "POST",
            ACTIVITY_TOKEN_URL,
            headers={
                "User-Agent": SIGN_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://86019-activity.dexfu.cn",
                "Referer": f"https://86019-activity.dexfu.cn/sign/component/page?signOperatingId={SIGN_OPERATING_ID}",
                "Cookie": session_cookie,
            },
            data={"timestamp": ts},
            proxies=proxies,
            server=server,
        )
        result = response.json()
        if not result.get("success"):
            print(f"❌ [签到] getToken 失败: {json_preview(result, 300)}")
            return ""
        raw_js = str(result.get("token") or "")
    except Exception as exc:
        print(f"❌ [签到] getToken 异常: {exc}")
        return ""

    # Node/execjs 执行（修掉旧式八进制字面量后再 eval）
    if execjs is not None:
        try:
            fixed_code = re.sub(r"\b0([0-7]+)\b", r"0o\1", raw_js)
            context = execjs.compile("var window = {};\n" + fixed_code)
            key = context.eval("window['3fd0cbet']")
            if key:
                print("✅ [签到] 获取签到 token 成功")
                return str(key)
        except Exception as exc:
            print(f"⚠️ [签到] JS 执行失败，改用正则提取: {str(exc)[:80]}")

    # 兜底：直接从 eval 产物里正则提取固定键
    m = re.search(r"window\[['\"]3fd0cbet['\"]\]\s*=\s*['\"]([^'\"]+)['\"]", raw_js)
    if not m:
        # 再兜底：先解出 eval 字符串再匹配
        m2 = re.search(r"window\[['\"]([0-9a-f]{6,10})['\"]\]\s*=\s*['\"]([^'\"]+)['\"]", raw_js)
        if m2:
            print("⚠️ [签到] 未找到 3fd0cbet 键，取首个候选项")
            return m2.group(2)
    if m:
        print("✅ [签到] 获取签到 token 成功（正则）")
        return m.group(1)

    print("❌ [签到] 无法从 getToken 响应解析 token")
    return ""


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
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

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        # 1. member/redirect 获取活动落地页地址
        redirect_resp = api_post(server, QMAI_REDIRECT_URL, token, proxies, {"redirectUrl": ACTIVITY_PAGE_URL})
        if not (redirect_resp.get("status") is True and redirect_resp.get("data")):
            result["error"] = f"获取活动地址失败: {json_preview(redirect_resp, 300)}"
            print(f"❌ [活动] {result['error']}")
            return result
        activity_url = str(redirect_resp["data"])
        print(f"🎯 [活动] 活动地址: {activity_url}")

        sleep(random.uniform(1.0, 2.0))

        # 2. 访问活动页取会话 Cookie
        session_cookie = fetch_activity_cookie(server, activity_url, proxies)
        if not session_cookie:
            result["error"] = "获取活动会话 Cookie 失败"
            print(f"❌ [活动] {result['error']}")
            return result

        sleep(random.uniform(1.0, 2.0))

        # 3. getToken 动态计算签到 token
        key = get_activity_key(server, session_cookie, proxies)
        if not key:
            result["error"] = ("getToken 失败：该签到 token 由兑吧反爬组件在浏览器上下文生成"
                           "（键名随机、依赖浏览器指纹），纯脚本无法复现；"
                           "请改用带浏览器的方案或直接在小程序内签到")
            print(f"❌ [签到] {result['error']}")
            return result

        sleep(random.uniform(1.0, 2.0))

        # 4. doSign 签到
        sign_resp = request_with_proxy(
            "POST",
            f"{ACTIVITY_SIGN_URL}?_={int(time.time() * 1000)}",
            headers={
                "User-Agent": SIGN_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://86019-activity.dexfu.cn",
                "Referer": f"https://86019-activity.dexfu.cn/sign/component/page?signOperatingId={SIGN_OPERATING_ID}",
                "accept-language": "zh-CN,zh;q=0.9",
                "Cookie": session_cookie,
            },
            data={
                "signOperatingId": SIGN_OPERATING_ID,
                "token": key,
            },
            proxies=proxies,
            server=server,
        )
        try:
            sign_json = sign_resp.json()
        except Exception:
            sign_json = {"success": False, "data": sign_resp.text[:300]}

        if sign_json.get("success") is True:
            sign_data = sign_json.get("data")
            if isinstance(sign_data, dict) and sign_data.get("signResult") not in (None, ""):
                result["signMsg"] = f"签到成功，获得{sign_data['signResult']}积分"
            elif sign_data:
                result["signMsg"] = f"签到成功: {json_preview(sign_data, 200)}"
            else:
                result["signMsg"] = "签到成功"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            preview = json_preview(sign_json, 300)
            if re.search(r"已签|已经签|签到过|重复|已完成", preview):
                result["signMsg"] = "今日已签到"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                result["signMsg"] = f"签到失败: {preview}"
                print(f"❌ [签到] {result['signMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🧋 益禾堂任务结果

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
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 益禾堂任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🧋 益禾堂任务完成", build_notify(results))


# YYB_SERVER 多账号适配：必须在 main() 前安装，避免首轮运行使用旧 code 服务。
from yyb_compat import install as _install_yyb
_install_yyb(globals())

if __name__ == "__main__":
    main()
