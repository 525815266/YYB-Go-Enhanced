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
荷叶健康小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code，登录换 token（含缓存与自动刷新）
  2. 查询果园信息（水滴/进度）
  3. 果园每日打卡领水滴（sceneId=6）
  4. 浇水（RSA SHA1withRSA 签名 + RSA 长加密 secret，移植自源脚本）
  5. 自动做任务：浏览任务/盲盒/弹窗/场馆点击/各类型领水（collectWater）
  6. 每日签到抽奖活动签到（actId=9093）
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

⚠️ 登录接口为推断，未经真机验证，失败请抓包核对

环境变量：
  PLUSPLUS_TOKEN         PushPlus token，可选
  QYWX_TOKEN             企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API              品赞代理提取 API，可选
  PROXY_TYPE             http / socks5，默认 http
  CODE_SERVER            本地 code 服务地址，默认 127.0.0.1:8088
  HYJK_CHANNEL_CODE      渠道码，默认 130
  HYJK_BROWSE_SECONDS    浏览任务兜底停留秒数，可选
  HYJK_BROWSE_WAIT_PADDING 浏览任务等待补偿秒数，默认 1

依赖：
  pip install requests
  pip install pycryptodome   （RSA 签名/加密，源脚本 encryptlong+jsrsasign 的 Python 等价实现）
  socks5 代理需：
  pip install requests[socks]
"""

import base64
import hashlib
import json
import os
import random
import string
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.Hash import SHA1
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15


APP_NAME = "荷叶健康"
APPID = "wx776afedbfae3a228"

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

BASE_URL = "https://tuan.api.ybm100.com"
LOGIN_URL = f"{BASE_URL}/miniapp/login"  # ⚠️ 推断端点
MANOR_INFO_URL = f"{BASE_URL}/api/healthSquare/fruitManor/getMyManorInfo"
SIGN_RECORD_URL = f"{BASE_URL}/miniapp/marketing/signActivity/signRecord"
SIGN_URL = f"{BASE_URL}/miniapp/marketing/signActivity/sign"
WATERING_URL = f"{BASE_URL}/api/healthSquare/water/watering"
USER_OPERATION_URL = f"{BASE_URL}/api/healthSquare/user/userOperation"
TASK_LIST_NEW_URL = f"{BASE_URL}/api/healthSquare/task/getTaskListNew"
POPUP_URL = f"{BASE_URL}/api/healthSquare/fruitManor/getPopup"
VENUE_INFO_URL = f"{BASE_URL}/api/healthSquare/fruitManor/getVenueInfo"
BLIND_BOX_URL = f"{BASE_URL}/api/healthSquare/task/getBlindBoxInfo"
COLLECT_WATER_URL = f"{BASE_URL}/api/healthSquare/water/collectWater"
GO_FRUIT_GARDEN_URL = f"{BASE_URL}/healthSquare/herbalGarden/goFruitOrGarden"

CHANNEL_CODE = os.getenv("HYJK_CHANNEL_CODE", "130")

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hyjkcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_15 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.70(0x18004630) NetType/WIFI "
    "Language/zh_CN miniProgram/wx776afedbfae3a228"
)

# 源脚本内置 RSA 密钥（watering/collectWater 的 secret 计算）
RSA_PRIVATE_KEY_B64 = (
    "MIICeAIBADANBgkqhkiG9w0BAQEFAASCAmIwggJeAgEAAoGBAKIGSy/bFo4l1JZuVpNCX3ccjf3eBgeooWzz0QgUFZhKZhJX7PxdfMw79fKjwR5ZGkCNPlO4F"
    "/TA3jrtoHHeewN8l3t7f63EFLud/5Ls3KOfHHYnkAo9bHWBWav84XdparGD4M8IHtq9qSGP6nRCOgnt4yqAmX8dJfYp9vr87cn3AgMBAAECgYEAlwzbB5Bu5LKs"
    "EFppZ/wW2ArM7YIRiQ5TACoGFEv1HfcuVaeXDmdxs02rKzwzDEHxUYDcPFyCKPGtvK5QSBgsAUUBHb6uu0fNGUccGX31NRAfLuQ8fj3W0uvkoYlpDARuokDHhWN"
    "qWzI6f8bFHkewJwpjXCO8w1WkogTLiX9Gu3ECQQDd5J4jEDS5+7KaohYRoryyX939mzsZ4RC6ufsfzTJwSlnLyYHEbm0Cs+7gbBxRrioqApBMQPIIoa5ujm1C88"
    "MNAkEAuu3htlbpR1ZL9b3wUuf3el/D3i/k9XvSChfHQ1q46Y/eck2yEDH9Kv/ZUxEl4fR8mB2MONm9oc2l+chPd9uQEwJBALcWuNU9vgPoB0tIiuUqXoDgUY+80"
    "ltcNi2c3/Uxn3jAIK/iKU0nwJMGXQiYrBVJnEjlrKL+w7cTkZZvtwATmtECQC2JV4vQvkFHj3eMzqeTpKDmBVPx/OekQzV8N2l8B0G2b20O6kqxssevzeRDcCQMJ"
    "/HyeL88o8pvy3f+yQUcsosCQQDZXV8K7Ek0R/V3dAdUzoetFSlfjCGy9QKPruz7m+iXBASxiA0R7YGfJzc8jWpuv0pxujtB/awy22K/ggLAhkZU"
)
JAVA_PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDNQpS4ZeHRiIPFIdZgupShTHFlGOqFkT6XEqByvWqt2BvLo3a+YfzyJHOXyfX41OvbIkuIaycuxU9w7RHI1e"
    "7F3O7Io+XxncjyU3GR+ae2DEtLaG3o/rtpONF5q1jTN/Spu4GKXsjhHrP9xxMThLF6134NKAyQZfvOms0gS0zmxwIDAQAB"
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


def random_string(length: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🌿 荷叶健康小程序动态 code 版                ║")
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
        "host": "tuan.api.ybm100.com",
        "referer": "https://www.heyejk.com/",
        "apptype": "1",
        "user-agent": USER_AGENT,
        "appversion": "3.1.7",
        "origin": "https://www.heyejk.com",
        "sec-fetch-dest": "empty",
        "sec-fetch-site": "cross-site",
        "terminal": "h5",
        "usertype": "groupuser",
        "appname": "ykq-xcx",
        "accept-language": "zh-CN,zh-Hans;q=0.9",
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "accept-encoding": "gzip, deflate",
        "sec-fetch-mode": "cors",
        "userid": "",
    }
    if token:
        headers["token"] = token
    return headers


# ====================== RSA 签名/加密（移植自源脚本 encryptlong + jsrsasign） ======================
def wrap_pem(key_b64: str, label: str) -> str:
    lines = [key_b64[i:i + 64] for i in range(0, len(key_b64), 64)]
    return "-----BEGIN " + label + "-----\n" + "\n".join(lines) + "\n-----END " + label + "-----\n"


def obj_to_str(data: Dict[str, Any]) -> str:
    return "&".join(f"{key}={value}" for key, value in data.items())


def rsa_sign_message(data: Dict[str, Any]) -> str:
    """SHA1withRSA 签名（参数字典序排序后 k=v& 拼接），base64 输出"""
    text = obj_to_str(dict(sorted(data.items())))
    key = RSA.import_key(wrap_pem(RSA_PRIVATE_KEY_B64, "PRIVATE KEY"))
    signature = pkcs1_15.new(key).sign(SHA1.new(text.encode("utf-8")))
    return base64.b64encode(signature).decode()


def rsa_encrypt_long(text: str) -> str:
    """encryptLong 等价实现：按 117 字节分块 PKCS1v1.5 加密，hex 拼接后整体转 base64"""
    key = RSA.import_key(wrap_pem(JAVA_PUBLIC_KEY_B64, "PUBLIC KEY"))
    cipher = PKCS1_v1_5.new(key)
    max_len = key.size_in_bytes() - 11
    raw = text.encode("utf-8")
    hex_out = "".join(
        cipher.encrypt(raw[i:i + max_len]).hex()
        for i in range(0, len(raw), max_len)
    )
    return base64.b64encode(bytes.fromhex(hex_out)).decode()


def build_secret(data: Dict[str, Any]) -> str:
    """sign=签名&timestamp=毫秒时间戳 → RSA 加密 → URL encode，作为 ?secret= 参数"""
    sign = rsa_sign_message(data)
    payload = {"sign": sign, "timestamp": int(time.time() * 1000)}
    return quote(rsa_encrypt_long(obj_to_str(payload)), safe="")


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
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json={
                "code": code,
                "appid": APPID,
                "channelCode": CHANNEL_CODE,
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

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str] | None = None,
            params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=headers or common_headers(token),
        params=params,
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


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any],
             headers: Dict[str, str] | None = None, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=headers or common_headers(token),
        json=payload,
        params=params,
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


def api_post_secret(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any],
                    headers: Dict[str, str]) -> Dict[str, Any]:
    """带 RSA secret 的业务 POST（watering / collectWater）"""
    secret = build_secret(payload)
    return api_post(server, url, token, proxies, payload, headers=headers, params={"secret": secret})


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
    """优先使用缓存 token（果园信息接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            manor_resp = api_get(server, MANOR_INFO_URL, cache_token, proxies, params={"channelCode": CHANNEL_CODE})
            if manor_resp.get("code") == 0:
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


# ====================== 果园业务（移植自源脚本 Task 类） ======================
def normalize_task_number(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def user_operation(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str],
                   operate_type: int, operate_value: Dict[str, Any]) -> Dict[str, Any]:
    data = {
        "operateType": operate_type,
        "operateValue": json.dumps(operate_value, ensure_ascii=False, separators=(",", ":")),
        "channelCode": CHANNEL_CODE,
    }
    return api_post(server, USER_OPERATION_URL, token, proxies, data, headers=headers)


def get_task_list_new(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str]) -> Dict[str, Any]:
    return api_get(server, TASK_LIST_NEW_URL, token, proxies, headers=headers,
                   params={"channelCode": CHANNEL_CODE, "venueId": 5})


def get_popup_list(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str]) -> Dict[str, Any]:
    return api_get(server, POPUP_URL, token, proxies, headers=headers,
                   params={"channelCode": CHANNEL_CODE, "entranceType": 0})


def get_venue_info(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str]) -> Dict[str, Any]:
    return api_get(server, VENUE_INFO_URL, token, proxies, headers=headers, params={"channelCode": CHANNEL_CODE})


def get_blind_box_info(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str],
                       task_id: int) -> Dict[str, Any]:
    return api_get(server, BLIND_BOX_URL, token, proxies, headers=headers,
                   params={"channelCode": CHANNEL_CODE, "taskId": task_id})


def collect_water(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str],
                  payload: Dict[str, Any]) -> Dict[str, Any]:
    data = {"channelCode": CHANNEL_CODE, "nonce": random_string(6)}
    data.update(payload)
    return api_post_secret(server, COLLECT_WATER_URL, token, proxies, data, headers)


def go_fruit_or_garden(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str],
                       task_id: int) -> Dict[str, Any]:
    return api_post(server, GO_FRUIT_GARDEN_URL, token, proxies,
                    {"channelCode": CHANNEL_CODE, "taskId": int(task_id)}, headers=headers)


def do_water(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str],
             tree_id: int) -> None:
    try:
        data = {"channelCode": CHANNEL_CODE, "treeId": tree_id, "nonce": random_string(6)}
        result = api_post_secret(server, WATERING_URL, token, proxies, data, headers)
        if result.get("code") == 0:
            print(f"💧 [浇水] {(result.get('result') or {}).get('progressBarTips', '浇水成功')}")
        else:
            print(f"⚠️ [浇水] 浇水失败: {result.get('msg')}")
    except Exception as exc:
        print(f"⚠️ [浇水] 浇水异常: {exc}")


def flatten_task_list(task_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not task_result or task_result.get("code") != 0:
        return []
    root = task_result.get("result")
    if isinstance(root, list):
        return [item for item in root if isinstance(item, dict)]
    if isinstance(root, dict):
        task_list = root.get("list")
        if isinstance(task_list, list):
            if any(isinstance(item, dict) and "taskId" in item for item in task_list):
                return [item for item in task_list if isinstance(item, dict)]
            flattened: List[Dict[str, Any]] = []
            for group in task_list:
                if isinstance(group, dict) and isinstance(group.get("taskList"), list):
                    flattened.extend(item for item in group["taskList"] if isinstance(item, dict))
            return flattened
    return []


def resolve_task_event_type(item: Dict[str, Any]) -> int:
    task_type = normalize_task_number(item.get("taskType"))
    browse_type = normalize_task_number(item.get("browseType"))
    water_event_type = normalize_task_number(item.get("waterEventType"))
    if task_type == 50:
        if browse_type == 1:
            return 11
        if browse_type == 2:
            return 12
        if browse_type == 3:
            return 13
        return 11
    if task_type in (60, 61):
        return 7
    if task_type == 70:
        return 5
    if task_type == 80:
        return 3
    if task_type == 90:
        return 4
    if task_type in (110, 120):
        return water_event_type
    return water_event_type or normalize_task_number(item.get("eventType"))


def resolve_task_water_num(item: Dict[str, Any]) -> float | None:
    for value in (item.get("reward"), item.get("waterNum"), item.get("waterConf"), item.get("taskRewardNum")):
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if num > 0:
            return num
    return None


def claim_task_water(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str],
                     item: Dict[str, Any], tag: str = "claim") -> None:
    task_id = normalize_task_number(item.get("taskId"))
    event_type = resolve_task_event_type(item)
    if not event_type:
        print(f"⚠️ [任务] {tag} taskId={task_id} 跳过: 缺少eventType(taskType={normalize_task_number(item.get('taskType'))})")
        return
    payload: Dict[str, Any] = {"extTask": 0, "eventType": event_type}
    if task_id:
        payload["taskId"] = task_id
    water_num = resolve_task_water_num(item)
    if water_num:
        payload["waterNum"] = water_num
    result = collect_water(server, token, proxies, headers, payload)
    print(f"💦 [领水] {tag} collectWater(taskId={task_id},eventType={event_type},waterNum={water_num or 0}): {result.get('msg') or result.get('code')}")


def log_task_item(item: Dict[str, Any], prefix: str = "task") -> None:
    print(
        f"🔍 [任务] {prefix} taskId={normalize_task_number(item.get('taskId'))}, "
        f"taskType={normalize_task_number(item.get('taskType'))}, taskStatus={normalize_task_number(item.get('taskStatus'))}, "
        f"browseType={normalize_task_number(item.get('browseType'))}, waterEventType={normalize_task_number(item.get('waterEventType'))}"
    )


def build_browse_seconds_candidates(item: Dict[str, Any]) -> List[float]:
    candidates: List[float] = []
    for value in (item.get("seconds"), item.get("browseSeconds"), item.get("needSeconds"),
                  item.get("taskSeconds"), os.getenv("HYJK_BROWSE_SECONDS")):
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if num >= 0:
            candidates.append(num)
    return list(dict.fromkeys([0, 20, 30, 60] + candidates))


def resolve_browse_target_seconds(item: Dict[str, Any]) -> int:
    for value in (item.get("needSeconds"), item.get("seconds"), item.get("browseSeconds"),
                  item.get("taskSeconds"), os.getenv("HYJK_BROWSE_SECONDS")):
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if num > 0:
            return int(num)
    return 20


def auto_browse_task(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str],
                     item: Dict[str, Any]) -> None:
    task_id = normalize_task_number(item.get("taskId"))
    target_seconds = resolve_browse_target_seconds(item)
    try:
        wait_padding = float(os.getenv("HYJK_BROWSE_WAIT_PADDING") or 1)
    except ValueError:
        wait_padding = 1.0

    start_res = user_operation(server, token, proxies, headers, 5, {"taskId": task_id, "seconds": 0})
    print(f"👀 [浏览] 浏览任务(taskId={task_id}) 起始上报seconds=0: {start_res.get('msg') or start_res.get('code')}")

    wait_ms = max(0, int((target_seconds + wait_padding) * 1000))
    if wait_ms > 0:
        print(f"👀 [浏览] 浏览任务(taskId={task_id}) 等待{int(wait_ms / 1000)}秒以满足停留时长")
        sleep(0.062)  # 源脚本 $.wait(62) 原样保留

    target_res = user_operation(server, token, proxies, headers, 5, {"taskId": task_id, "seconds": target_seconds})
    print(f"👀 [浏览] 浏览任务(taskId={task_id}) 目标上报seconds={target_seconds}: {target_res.get('msg') or target_res.get('code')}")

    seconds_candidates = [s for s in build_browse_seconds_candidates(item) if s != 0 and s != target_seconds]
    for seconds in seconds_candidates:
        op_res = user_operation(server, token, proxies, headers, 5, {"taskId": task_id, "seconds": int(seconds)})
        print(f"👀 [浏览] 浏览任务(taskId={task_id}) 兜底上报seconds={int(seconds)}: {op_res.get('msg') or op_res.get('code')}")
        sleep(0.4)


def auto_blind_box(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str],
                   task_id: int) -> None:
    try:
        box_resp = get_blind_box_info(server, token, proxies, headers, task_id)
        box_list = box_resp.get("result") if isinstance(box_resp.get("result"), list) else []
        for box in box_list:
            if not isinstance(box, dict):
                continue
            if normalize_task_number(box.get("status")) in (0, 1):
                blind_box_id = normalize_task_number(box.get("blindBoxId"))
                if not blind_box_id:
                    continue
                op_res = user_operation(server, token, proxies, headers, 4, {"taskId": task_id, "blindBoxId": blind_box_id})
                print(f"📦 [盲盒] 盲盒(taskId={task_id}, blindBoxId={blind_box_id}) 上报: {op_res.get('msg') or op_res.get('code')}")
                sleep(0.5)
    except Exception as exc:
        print(f"⚠️ [盲盒] 盲盒自动化异常(taskId={task_id}): {exc}")


def auto_today_full_water_task(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str],
                               today_full_water_task_id: int) -> None:
    try:
        if not today_full_water_task_id:
            return
        op_res = user_operation(server, token, proxies, headers, 6,
                                {"taskId": today_full_water_task_id, "status": 1})
        print(f"🪣 [满水] fullWater(taskId={today_full_water_task_id}) 上报: {op_res.get('msg') or op_res.get('code')}")
        sleep(0.5)
    except Exception as exc:
        print(f"⚠️ [满水] fullWater自动化异常: {exc}")


def auto_popups(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str]) -> None:
    try:
        popup_resp = get_popup_list(server, token, proxies, headers)
        popup_root = popup_resp.get("result") or {}
        items = popup_root.get("list") if isinstance(popup_root, dict) else None
        for popup in items or []:
            if not isinstance(popup, dict):
                continue
            popup_type = normalize_task_number(popup.get("popupType"))
            water_num = normalize_task_number(popup.get("waterNum"))
            op_res = user_operation(server, token, proxies, headers, 7,
                                    {"popupType": popup_type, "waterNum": water_num})
            print(f"🪟 [弹窗] popupType={popup_type} 上报: {op_res.get('msg') or op_res.get('code')}")
            sleep(0.5)
    except Exception as exc:
        print(f"⚠️ [弹窗] popup自动化异常: {exc}")


def auto_venue_clicks(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str]) -> None:
    try:
        venue_resp = get_venue_info(server, token, proxies, headers)
        items = (venue_resp.get("result") or {}).get("list") or []
        for venue in items:
            if not isinstance(venue, dict):
                continue
            if normalize_task_number(venue.get("clickStatus")) == 0 and venue.get("id"):
                op_res = user_operation(server, token, proxies, headers, 10, {"venueId": int(venue["id"])})
                print(f"🏟️ [场馆] venueId={venue['id']} 上报: {op_res.get('msg') or op_res.get('code')}")
                sleep(0.5)
    except Exception as exc:
        print(f"⚠️ [场馆] venue自动化异常: {exc}")


def auto_tasks(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str]) -> int:
    handled = 0
    try:
        first_task_resp = get_task_list_new(server, token, proxies, headers)
        first_task_list = flatten_task_list(first_task_resp)
        print(f"📋 [任务] taskList数量: {len(first_task_list)}")
        for item in first_task_list:
            task_type = normalize_task_number(item.get("taskType"))
            task_status = normalize_task_number(item.get("taskStatus"))
            task_id = normalize_task_number(item.get("taskId"))
            if not task_id:
                continue
            log_task_item(item, "scan")

            if task_type == 50 and task_status in (0, 3):
                auto_browse_task(server, token, proxies, headers, item)
                handled += 1
                continue

            if task_type == 70 and task_status == 0:
                op_res = user_operation(server, token, proxies, headers, 9, {"taskId": task_id})
                print(f"📋 [任务] task70(taskId={task_id}) 上报: {op_res.get('msg') or op_res.get('code')}")
                sleep(0.5)
                handled += 1
                continue

            if task_type == 80 and task_status in (0, 3):
                auto_blind_box(server, token, proxies, headers, task_id)
                handled += 1
                continue

            if task_type in (60, 61) and task_status == 0:
                claim_task_water(server, token, proxies, headers, item, "task60/61直领")
                sleep(0.5)
                handled += 1
                continue

            if task_type == 90 and task_status in (0, 3):
                claim_task_water(server, token, proxies, headers, item, "task90直领")
                sleep(0.5)
                handled += 1
                continue

            if task_type in (110, 120) and task_status in (0, 3):
                jump_res = go_fruit_or_garden(server, token, proxies, headers, task_id)
                print(f"📋 [任务] task{task_type}(taskId={task_id}) goFruitOrGarden: {jump_res.get('msg') or jump_res.get('code')}")
                sleep(0.5)
                handled += 1
                continue

            if task_status == 1:
                claim_task_water(server, token, proxies, headers, item, "立即领奖")
                sleep(0.5)
                handled += 1
                continue

            if task_status not in (2, 10000):
                print(f"⚠️ [任务] 未适配任务: taskType={task_type}, taskStatus={task_status}, taskId={task_id}")

        second_task_resp = get_task_list_new(server, token, proxies, headers)
        second_task_list = flatten_task_list(second_task_resp)
        for item in second_task_list:
            if normalize_task_number(item.get("taskStatus")) != 1:
                continue
            claim_task_water(server, token, proxies, headers, item, "二次领奖")
            sleep(0.5)
            handled += 1
    except Exception as exc:
        print(f"⚠️ [任务] task自动化异常: {exc}")
    return handled


def get_fruit_info(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str]) -> Dict[str, Any]:
    """查询果园信息 + 果园打卡 + 浇水，返回状态摘要"""
    summary = {"water": "-", "sign": "-", "waterCount": 0}
    resp = api_get(server, MANOR_INFO_URL, token, proxies, headers=headers,
                   params={"channelCode": CHANNEL_CODE})
    if resp.get("code") != 0:
        print(f"⚠️ [果园] 查询失败[{resp.get('msg')}]")
        return summary

    result = resp.get("result") or {}
    kettle_water = result.get("kettleWater")
    print(f"🌳 [果园] [{kettle_water}]-[{result.get('progressBarTips')}]")
    summary["water"] = str(kettle_water)

    tree_id = int(to_float(result.get("treeId")))
    user_id = result.get("userId") or ""
    headers["userid"] = str(user_id or "")

    # 果园打卡领水滴（sceneId=6）
    try:
        record_resp = api_post(server, SIGN_RECORD_URL, token, proxies,
                               {"sceneId": 6, "channelCode": ""}, headers=headers)
        if record_resp.get("code") == 0:
            is_signed = (record_resp.get("result") or {}).get("todaySignStatusDesc") == "已签到"
            if is_signed:
                summary["sign"] = "今日已打卡"
                print("✅ [打卡] 果园今日打卡: 已打卡")
            else:
                sign_resp = api_post(server, SIGN_URL, token, proxies,
                                     {"sceneId": 6, "channelCode": ""}, headers=headers)
                if str(sign_resp.get("code")) == "0":
                    summary["sign"] = f"打卡成功[{sign_resp.get('msg')}]"
                    print(f"✅ [打卡] 打卡成功[{sign_resp.get('msg')}]")
                else:
                    summary["sign"] = f"打卡失败:{sign_resp.get('msg')}"
                    print(f"❌ [打卡] 打卡失败:{sign_resp.get('msg')}")
        else:
            summary["sign"] = f"获取签到状态失败[{record_resp.get('msg')}]"
            print(f"⚠️ [打卡] 获取签到状态失败[{record_resp.get('msg')}]")
    except Exception as exc:
        print(f"⚠️ [打卡] 果园打卡异常: {exc}")

    today_full_water_task_id = int(to_float(result.get("todayFullWaterTaskId") or 0))

    water_times = int(to_float(kettle_water)) // 10
    for _ in range(water_times):
        sleep(3)
        do_water(server, token, proxies, headers, tree_id)
        summary["waterCount"] += 1

    # 任务自动化（含满水任务上报）
    auto_today_full_water_task(server, token, proxies, headers, today_full_water_task_id)
    auto_popups(server, token, proxies, headers)
    auto_venue_clicks(server, token, proxies, headers)
    summary["taskCount"] = auto_tasks(server, token, proxies, headers)
    return summary


def get_sign_info(server: str, token: str, proxies: Dict[str, str] | None, headers: Dict[str, str]) -> str:
    """签到抽奖活动（actId=9093）"""
    resp = api_post(server, SIGN_RECORD_URL, token, proxies,
                    {"actId": "9093", "channelCode": CHANNEL_CODE, "adornId": "217"}, headers=headers)
    if resp.get("code") != 0:
        print(f"⚠️ [签到] 获取签到状态失败[{resp.get('msg')}]")
        return f"获取签到状态失败[{resp.get('msg')}]"

    is_signed = (resp.get("result") or {}).get("todaySignStatusDesc") == "已签到"
    print(f"📝 [签到] 今日签到: {'已签到' if is_signed else '未签到'}")
    if is_signed:
        return "今日已签到"

    sign_resp = api_post(server, SIGN_URL, token, proxies,
                         {"actId": "9093", "channelCode": CHANNEL_CODE, "adornId": "217"}, headers=headers)
    if str(sign_resp.get("code")) == "0":
        print(f"✅ [签到] 签到成功[{sign_resp.get('msg')}]")
        return f"签到成功[{sign_resp.get('msg')}]"
    print(f"❌ [签到] 签到失败:{sign_resp.get('msg')}")
    return f"签到失败:{sign_resp.get('msg')}"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "gardenMsg": "-",
        "taskMsg": "-",
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
        headers = common_headers(token)

        garden = get_fruit_info(server, token, proxies, headers)
        result["gardenMsg"] = f"水滴:{garden['water']} 打卡:{garden['sign']} 浇水:{garden['waterCount']}次"

        sign_msg = get_sign_info(server, token, proxies, headers)
        result["signMsg"] = sign_msg

        result["taskMsg"] = f"完成任务/领水 {garden.get('taskCount', 0)} 项"

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🌿 荷叶健康任务结果

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
🌳 果园：{res["gardenMsg"]}
🎯 任务：{res["taskMsg"]}
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
                "gardenMsg": "-",
                "taskMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 荷叶健康任务执行完成                  ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🌿 荷叶健康任务完成", build_notify(results))


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
