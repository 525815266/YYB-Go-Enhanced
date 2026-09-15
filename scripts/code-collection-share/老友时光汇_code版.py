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
老友时光汇小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. 登录接口使用 code 换 x-token
  3. 每日签到（userIsSign 查询 + userSign 提交）
  4. 活动答题（startAnswer 自动提交答案 -> submitExam 领奖）
  5. 查询积分，满 50 自动兑换（credits-exchange）
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       覆盖本地 code 服务地址，可选

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]

⚠️ 登录接口为推断，未经真机验证，失败请抓包核对
   （源脚本为 x-token 抓包型，无登录调用，登录端点 /api/login 为推断）
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


APP_NAME = "老友时光汇小程序"
APPID = "wxa973bdd2c6278631"

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

BASE_URL = "https://api.zijinzhaoyao.com/api"
LOGIN_URL = f"{BASE_URL}/login"
SIGN_STATUS_URL = f"{BASE_URL}/userIsSign"
SIGN_URL = f"{BASE_URL}/userSign"
ACTIVITY_LIST_URL = f"{BASE_URL}/v3/activity-list/columns"
START_ANSWER_URL = f"{BASE_URL}/v2/startAnswer"
SUBMIT_ANSWER_URL = f"{BASE_URL}/submitAnswer"
SUBMIT_EXAM_URL = f"{BASE_URL}/v2/submitExam"
VISIT_LOG_URL = f"{BASE_URL}/userVisitLog"
USER_CREDITS_URL = f"{BASE_URL}/v2/getUserCredits"
CREDITS_EXCHANGE_URL = f"{BASE_URL}/v3/credits-exchange"

AES_KEY = "Kj8mN2pQ9rS5tU7vW3xY1zA4bC6dE8fG"
AES_IV = "H7nM4kL9pQ2rS5tU"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lysghcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541923) XWEB/19823"
)


# ====================== AES-256-CBC（纯标准库实现，免 pycryptodome 依赖） ======================
_SBOX = [
0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16]
_RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36]


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1b) & 0xff
    return a


def _expand_key(key: bytes):
    words = [list(key[i:i + 4]) for i in range(0, 32, 4)]
    for i in range(8, 60):
        temp = list(words[i - 1])
        if i % 8 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[t] for t in temp]
            temp[0] ^= _RCON[i // 8 - 1]
        elif i % 8 == 4:
            temp = [_SBOX[t] for t in temp]
        words.append([words[i - 8][j] ^ temp[j] for j in range(4)])
    return words


def _encrypt_block(block: bytes, words) -> bytes:
    state = list(block)

    def add_round_key(rnd: int) -> None:
        for c in range(4):
            for j in range(4):
                state[4 * c + j] ^= words[rnd * 4 + c][j]

    add_round_key(0)
    for rnd in range(1, 15):
        state = [_SBOX[b] for b in state]
        new = list(state)
        for r in range(1, 4):
            for c in range(4):
                new[r + 4 * c] = state[r + 4 * ((c + r) % 4)]
        state = new
        if rnd != 14:
            for c in range(4):
                a = state[4 * c:4 * c + 4]
                t = a[0] ^ a[1] ^ a[2] ^ a[3]
                state[4 * c + 0] ^= t ^ _xtime(a[0] ^ a[1])
                state[4 * c + 1] ^= t ^ _xtime(a[1] ^ a[2])
                state[4 * c + 2] ^= t ^ _xtime(a[2] ^ a[3])
                state[4 * c + 3] ^= t ^ _xtime(a[3] ^ a[0])
        add_round_key(rnd)
    return bytes(state)


def aes256_cbc_encrypt_hex(plaintext: str, key: str, iv: str) -> str:
    """AES-256-CBC PKCS7 加密后转 hex（等价 Node aes-256-cbc + hex 输出）"""
    data = plaintext.encode("utf-8")
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    words = _expand_key(key.encode("utf-8"))
    prev = iv.encode("utf-8")
    out = b""
    for i in range(0, len(data), 16):
        blk = bytes(b ^ p for b, p in zip(data[i:i + 16], prev))
        prev = _encrypt_block(blk, words)
        out += prev
    return out.hex()


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
    print("║ 🧧 老友时光汇小程序动态 code 版              ║")
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
    """老友时光汇请求头：x-token 放 header，code/deviceid 按源脚本动态填充"""
    headers = {
        "Host": "api.zijinzhaoyao.com",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/json;charset=UTF-8",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referrer-Policy": "unsafe-url",
        "xweb_xhr": "1",
        "User-Agent": USER_AGENT,
        "miniprogram-environment": "wechat",
        "wxapp-version": "1.0.3",
        "x-requested-with": "XMLHttpRequest",
        "project-name": "yl",
        "x-tt-device-id": f"android-{os.urandom(12).hex()}",
        "deviceid": "",
        "Origin": "https://servicewechat.com",
        "Referer": f"https://servicewechat.com/{APPID}/13/page-frame.html",
        "Cookie": f"acw_tc={int(time.time() * 1000)}_{random.randint(1000, 9999)};",
        "code": "",
    }
    if token:
        headers["x-token"] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("x-token"),
        data.get("xToken"),
        data.get("jwt"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("x-token"),
            inner.get("xToken"),
            inner.get("jwt"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 x-token")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json={"code": code},
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


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any], extra_headers: Dict[str, str] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token) | (extra_headers or {}),
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "message": f"JSON解析失败: {response.text[:300]}",
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


def parse_jwt_user_id(token: str) -> str | None:
    """照源脚本：解析 x-token (JWT) 第二段 base64 得 userID"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("userID")
    except Exception:
        return None


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（积分接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            credits_resp = api_post(server, USER_CREDITS_URL, cache_token, proxies, {})
            if credits_resp.get("code") == 0:
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


def get_device_id_header(code: str = "") -> Dict[str, str]:
    """照源脚本 getAES：code/t/c 组 JSON 后 AES-256-CBC 加密为 deviceid"""
    random_offset = random.randint(31, 40)
    now_ms = int(time.time() * 1000)
    ad_start_time = now_ms + random_offset * 1000  # 假设广告 35 秒前开始
    c = int((now_ms - ad_start_time) / 1000) if ad_start_time else 0
    if not code:
        code = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))
    device_id_obj = {
        "code": code,
        "t": int(now_ms / 1000),
        "c": c,
    }
    plaintext = jdump(device_id_obj)
    device_id = aes256_cbc_encrypt_hex(plaintext, AES_KEY, AES_IV)
    return {"code": code, "deviceid": device_id}


def jdump(obj: Any) -> str:
    """模拟 JS JSON.stringify 的紧凑输出"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def get_random_code() -> str:
    return "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))


def do_sign(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """照源脚本 sign()：userIsSign 查询 -> 未签则 userSign 提交"""
    status_resp = api_post(server, SIGN_STATUS_URL, token, proxies, {})
    if status_resp.get("data") is False:
        wait_time = random.randint(1, 3)
        sleep(wait_time)
        # 照源脚本：查询后 code 固定填 'adsadada'，deviceid 用其 AES 结果
        sign_headers = get_device_id_header("adsadada")
        sign_resp = api_post(server, SIGN_URL, token, proxies, {}, extra_headers=sign_headers)
        if sign_resp.get("code") == 0:
            print("✅ [签到] 签到成功")
            return "签到成功"
        msg = sign_resp.get("message") or json_preview(sign_resp, 200)
        print(f"⚠️ [签到] {msg}")
        return f"签到失败: {msg}"

    print("✅ [签到] 今日已签到")
    return "今日已签到"


def do_submit_answer(server: str, token: str, proxies: Dict[str, str] | None, activity_id: Any, exam_id: Any, answer: Any, number: Any) -> bool:
    resp = api_post(server, SUBMIT_ANSWER_URL, token, proxies, {
        "id": activity_id,
        "examId": exam_id,
        "answer": answer,
        "number": number,
    })
    if resp.get("code") == 0:
        correct = safe_data(resp).get("isCorrect") is True
        print(f"✅ [答题] 提交答案请求结果：{'正确' if correct else '错误'}")
        return True
    print(f"⚠️ [答题] 提交答案失败，{resp.get('message') or json_preview(resp, 200)}")
    return False


def do_submit_exam(server: str, token: str, proxies: Dict[str, str] | None, activity_id: Any, exam_id: Any) -> bool:
    code = get_random_code()
    exam_headers = get_device_id_header(code)
    resp = api_post(server, SUBMIT_EXAM_URL, token, proxies, {
        "id": activity_id,
        "examId": exam_id,
    }, extra_headers=exam_headers)
    if resp.get("code") == 0:
        data = safe_data(resp)
        print(f"✅ [答题] 提交最终结果奖励：积分 {data.get('credits')}，现金 {data.get('money')} 元")
        return True
    print(f"⚠️ [答题] 提交最终结果失败，{resp.get('message') or json_preview(resp, 200)}")
    return False


def do_save_log(server: str, token: str, proxies: Dict[str, str] | None, user_id: Any, activity_id: Any) -> bool:
    try:
        api_post(server, VISIT_LOG_URL, token, proxies, {
            "visit_url": f"/pages/crushPage/game/index?isNeedAd=1&adOrder=2&id={activity_id}&type=1",
            "user_id": user_id,
            "channel_id": 0,
            "event_type": "submit_activity",
        })
        return True
    except Exception as exc:
        print(f"⚠️ [答题] 保存日志请求失败: {exc}")
        return False


def do_start_answer(server: str, token: str, proxies: Dict[str, str] | None, user_id: Any, activity_id: Any) -> bool:
    """照源脚本 startAnswer：开始答题 -> 提交答案 -> 保存日志 -> 提交试卷"""
    resp = api_post(server, START_ANSWER_URL, token, proxies, {"id": activity_id})
    if resp.get("code") != 0:
        print(f"⚠️ [答题] 开始答题失败：{resp.get('message') or json_preview(resp, 200)}")
        return False

    data = safe_data(resp)
    question_num = data.get("questionNum")
    exam_id = data.get("examId")
    question = data.get("question")
    answer = question.get("answer") if isinstance(question, dict) else None

    if not (activity_id and exam_id and answer):
        print("⚠️ [答题] 获取答案失败")
        return False

    res1 = do_submit_answer(server, token, proxies, activity_id, exam_id, answer, question_num)
    sleep(random.randint(5, 10))
    res2 = do_save_log(server, token, proxies, user_id, activity_id)
    sleep(random.randint(10, 15))
    res3 = do_submit_exam(server, token, proxies, activity_id, exam_id)
    return res1 and res2 and res3


def get_user_credits(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[str, float]:
    resp = api_post(server, USER_CREDITS_URL, token, proxies, {})
    if resp.get("code") == 0:
        amount = to_float(safe_data(resp).get("credits"))
        print(f"✅ [积分] 获取用户积分成功: {amount:g}")
        if amount > 50:
            print("✅ [兑换] 积分可以兑换，开始自动兑换")
            exchange_resp = api_post(server, CREDITS_EXCHANGE_URL, token, proxies, {"amount": amount})
            if exchange_resp.get("code") == 0:
                msg = f"兑换成功：约 {amount / 10:g} 元"
                print(f"✅ [兑换] {msg}")
                return msg, amount
            msg = f"兑换失败: {json_preview(exchange_resp, 200)}"
            print(f"⚠️ [兑换] {msg}")
            return msg, amount
        return f"当前积分 {amount:g}", amount

    msg = resp.get("message") or json_preview(resp, 200)
    print(f"⚠️ [积分] 获取积分失败，{msg}")
    return f"获取积分失败: {msg}", 0.0


def do_activities(server: str, token: str, proxies: Dict[str, str] | None, user_id: Any) -> str:
    """照源脚本 getActivityList：活动列表 -> 逐个答题 -> 查积分"""
    resp = api_post(server, ACTIVITY_LIST_URL, token, proxies, {"column": 1}, extra_headers={"project-name": "xld"})
    if resp.get("code") != 0:
        msg = f"获取活动列表失败: {resp.get('message') or json_preview(resp, 200)}"
        print(f"⚠️ [活动] {msg}")
        return msg

    activity_list = safe_data(resp)
    if not isinstance(activity_list, list) or not activity_list:
        print("⚠️ [活动] 无可用活动")
        return "无可用活动"

    summary_parts: List[str] = []
    for activity in activity_list:
        if not isinstance(activity, dict):
            continue
        activity_id = activity.get("id")
        title = activity.get("title")
        end_time = datetime.fromtimestamp(to_float(activity.get("endTime"))).strftime("%Y-%m-%d %H:%M:%S")
        left_money = to_float(activity.get("leftMoney"))
        times = int(to_float(activity.get("times")) - to_float(activity.get("count")))
        if left_money > 0 and times > 0 and to_float(activity.get("endTime")) * 1000 > time.time() * 1000:
            print(f"✅ [活动] 获取到活动:【 {title}】，结束时间: {end_time}，剩余金额: {left_money:g}，开始答题")
            all_times = 0
            test_times = 0
            while times > all_times and test_times < 10:
                print(f"⏳ [答题] 第{all_times + 1}次答题")
                test_times += 1
                if test_times >= 10:
                    print("⚠️ [答题] 已连续答题10次未成功，请稍后再试")
                    break
                if not do_start_answer(server, token, proxies, user_id, activity_id):
                    sleep(random.randint(5, 10))
                    continue
                all_times += 1
                sleep(random.randint(5, 10))

            credits_msg, _ = get_user_credits(server, token, proxies)
            summary_parts.append(f"{title}:{credits_msg}")
        else:
            msg = f"活动:【{title}】在{end_time}已结束|剩余{left_money:g}元|剩余答题{times}次"
            print(f"⚠️ [活动] 获取到活动: ❌：{msg}")
            summary_parts.append(title or msg)

    return "；".join(summary_parts) if summary_parts else "无可用活动"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "activityMsg": "-",
        "creditsMsg": "-",
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
        user_id = parse_jwt_user_id(token)

        # 每日签到
        result["signMsg"] = do_sign(server, token, proxies)

        sleep(random.randint(1, 3))

        # 活动答题 + 积分兑换
        result["activityMsg"] = do_activities(server, token, proxies, user_id)

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🧧 老友时光汇小程序任务结果

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
🎯 活动：{res["activityMsg"]}
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
                "activityMsg": "-",
                "creditsMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 老友时光汇任务执行完成                    ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🧧 老友时光汇任务完成", build_notify(results))


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
