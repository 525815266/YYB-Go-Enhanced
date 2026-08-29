#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 自动登录拿到会员信息
#       并缓存到本地 JSON；下次运行先读取缓存 token，并调用签到规则接口验证；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
印象星会员小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /login/wxCode 使用 code 换 token
  3. /login/wx/autoLogin 获取 loginToken、memberId、phoneNumber
  4. 每日签到
  5. 查询签到状态
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER        YYB 地址@账号 ID/OpenID，每行一个账号
  YYB_API_KEY       YYB API Key，可选
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  YXX_PLAZA_CODE    广场编码，默认 G001Z003C0018

依赖：
  pip install requests
  pip install pycryptodome
  socks5 代理需：
  pip install requests[socks]
"""

import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, parse_qs, urlparse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


APP_NAME = "印象星会员小程序"
APPID = "wxeee2a26f00bc7701"
PLAZA_CODE = os.getenv("YXX_PLAZA_CODE", "G001Z003C0018")
VERSION = "20260818150900"
SIGN_SECRET = "RLgF0BiQDcHfGhQeGrJMH66MCin6jD2q9+yiP9+/wC8="
AES_KEY = b"inplusCloud@!@#$"

_SERVER_ENV = os.getenv("YYB_SERVER", "").strip()
SERVERS = [item.strip() for item in _SERVER_ENV.replace(",", "\n").splitlines() if item.strip()]
YYB_API_KEY = os.getenv("YYB_API_KEY", "").strip()

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://crm.scpgroup.com.cn/yinli-minapp/api/v1"
LOGIN_URL = f"{BASE_URL}/login/wxCode"
AUTO_LOGIN_URL = f"{BASE_URL}/login/wx/autoLogin"
SIGN_RULES_URL = f"{BASE_URL}/signDay/rules"
SIGN_URL = f"{BASE_URL}/signDay/sign"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yxx_token_cache.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541923) XWEB/19823"
)

# Windows 控制台默认 GBK 无法编码 emoji/特殊字符，强制 stdout/stderr 为 UTF-8
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


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


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 💠 印象星会员动态 code 版                        ║")
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


def get_code(server: str) -> str | None:
    try:
        endpoint, ref = parse_yyb_entry(server)
        url = f"{endpoint}/wxapp/getCode"
        headers = {"X-API-Key": YYB_API_KEY} if YYB_API_KEY else {}
        print(f"🔐 [授权] YYB 获取 code（账号 {mask(ref)}）")
        response = direct_session().post(
            url,
            json={"ref": ref, "app_id": APPID},
            headers=headers,
            timeout=20,
        )
        data = response.json()
        result = data.get("data") or {}
        result = result.get("result") if isinstance(result, dict) else {}
        code = result.get("code") if isinstance(result, dict) else None
        if data.get("code") != 0 or not code:
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        print("✅ [授权] code 获取成功")
        return str(code)
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def parse_yyb_entry(raw: str) -> Tuple[str, str]:
    value = str(raw or "").strip()
    if "@" not in value:
        raise ValueError("YYB_SERVER 格式应为 地址@账号ID或OpenID")
    endpoint, ref = value.split("@", 1)
    endpoint = endpoint.strip().rstrip("/")
    ref = ref.strip()
    if not endpoint or not ref:
        raise ValueError("YYB_SERVER 缺少地址或账号标识")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"http://{endpoint}"
    return endpoint, ref


def common_headers(
    token: str = "",
    member_id: str = "",
    phone_number: str = "",
    open_id: str = "",
    security: bool = True,
) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "appid": APPID,
        "orgCode": PLAZA_CODE,
        "type": "0",
        "appType": "0",
        "plazaCode": PLAZA_CODE,
        "plazzaCode": PLAZA_CODE,
        "vUnionCode": "U001",
        "version": VERSION,
        "title": quote(APP_NAME),
        "shopId": "",
        "userId": "",
        "sysType": "0",
    }
    if token:
        headers["token"] = token
    if member_id:
        headers["memberId"] = member_id
    if phone_number:
        headers["phoneNumber"] = phone_number
    if open_id:
        headers["openId"] = open_id
    if security:
        headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        headers["Referer"] = f"https://servicewechat.com/{APPID}/686/page-frame.html"
    return headers


def extract_token(data: Any) -> Tuple[str, str]:
    if not isinstance(data, dict):
        return "", ""
    inner = data.get("data") or {}
    if isinstance(inner, dict):
        return str(inner.get("token") or ""), str(inner.get("openId") or "")
    return "", ""


def login_by_code(
    server: str,
    code: str,
    proxies: Dict[str, str] | None,
) -> Tuple[str | None, str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "GET",
            LOGIN_URL,
            headers=common_headers(security=False),
            params={
                "code": code,
                "type": "0",
                "plazaCode": "G001",
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        token, open_id = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, open_id, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None, None


def build_sign_header(
    url: str,
    params: Dict[str, Any],
    timestamp: str = "",
    nonce: str = "",
) -> Tuple[Dict[str, str], str]:
    if not timestamp:
        timestamp = str(int(time.time() * 1000))
    if not nonce:
        nonce = secrets.token_hex(4)

    merged = dict(params or {})
    if "?" in url:
        query = urlparse(url).query
        for key, values in parse_qs(query).items():
            merged[key] = values[0]

    sign_data = dict(merged)
    sign_data["timestamp"] = timestamp
    sign_data["nonce"] = nonce
    sign_data = {
        key: value
        for key, value in sign_data.items()
        if value is not None and value != ""
    }

    serialized = "&".join(f"{key}={value}" for key, value in sorted(sign_data.items()))
    sign = hmac.new(SIGN_SECRET.encode(), serialized.encode(), hashlib.sha256).hexdigest()

    return {
        "timestamp": timestamp,
        "nonce": nonce,
        "sign": sign,
    }, serialized


def encrypt_body(params: Dict[str, Any], timestamp: str) -> Dict[str, str]:
    plaintext = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    iv = timestamp + "000"
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv.encode())
    encrypted = base64.b64encode(
        cipher.encrypt(pad(plaintext.encode("utf-8"), 16))
    ).decode()
    return {
        "data": encrypted,
        "iv": iv,
    }


def auto_login_by_code(
    server: str,
    code: str,
    token: str,
    open_id: str,
    proxies: Dict[str, str] | None,
) -> Tuple[str | None, str | None, str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] autoLogin 获取会员信息")
        params = {
            "wxCode": code,
            "qrCode": "",
            "openId": "",
            "appId": "",
            "plazaCode": PLAZA_CODE,
            "type": "0",
            "qrCodeType": "",
            "scene": "",
            "expandingChannel": "",
            "activityId": "",
            "shareSource": "",
            "fromMember": "",
        }
        sign_headers, _ = build_sign_header(AUTO_LOGIN_URL, params)
        headers = common_headers(token=token, open_id=open_id)
        headers.update(sign_headers)
        body = encrypt_body(params, sign_headers["timestamp"])

        response = request_with_proxy(
            "POST",
            AUTO_LOGIN_URL,
            headers=headers,
            json=body,
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        inner = safe_data(data)
        login_token = inner.get("loginToken") or inner.get("token") or ""
        member_id = str(inner.get("memberId") or "")
        phone_number = str(inner.get("phoneNumber") or "")

        if login_token and member_id and phone_number:
            print(f"✅ [登录] loginToken 获取成功: {mask(login_token)}")
            print(f"✅ [会员] memberId: {member_id}, phone: {mask(phone_number)}")
            return login_token, member_id, phone_number, data

        print(f"❌ [登录] autoLogin 未返回完整会员信息: {json_preview(data)}")
        return None, None, None, data
    except Exception as exc:
        print(f"❌ [登录] autoLogin 请求异常: {exc}")
        return None, None, None, None


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


def get_cached_account(server: str) -> Dict[str, Any] | None:
    cache = load_token_cache()
    data = cache.get(server)
    if not data or not data.get("token") or not data.get("memberId") or not data.get("phoneNumber"):
        return None
    try:
        expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
        if time.time() * 1000 < expire - 3600 * 1000:
            print(f"✅ [缓存] 使用 {server} 缓存账号")
            return data
    except Exception as exc:
        print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_account(server: str, token: str, member_id: str, phone_number: str, raw: Dict[str, Any] | None) -> None:
    expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    if raw and isinstance(raw, dict):
        inner = safe_data(raw)
        expires_in = inner.get("expiresIn")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            expire_time = datetime.fromtimestamp(time.time() + expires_in).isoformat()

    cache = load_token_cache()
    cache[server] = {
        "token": token,
        "memberId": member_id,
        "phoneNumber": phone_number,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def is_token_error(message: Any) -> bool:
    return bool(
        str(message).find("token") >= 0
        or str(message).find("登录") >= 0
        or str(message).find("未授权") >= 0
        or str(message).find("未登录") >= 0
        or str(message).find("失效") >= 0
        or str(message).find("过期") >= 0
        or str(message).find("401") >= 0
        or str(message).find("403") >= 0
    )


def check_cached_account(
    server: str,
    account: Dict[str, Any],
    proxies: Dict[str, str] | None,
) -> bool:
    print("🔍 [缓存] 验证 token")
    try:
        resp = api_get(
            server,
            f"{SIGN_RULES_URL}?memberId={quote(account['memberId'])}&phoneNumber={quote(account['phoneNumber'])}",
            token=account["token"],
            member_id=account["memberId"],
            phone_number=account["phoneNumber"],
            proxies=proxies,
        )
        if resp.get("status") in (0, 200, "0", "200"):
            print("✅ [缓存] token 有效")
            return True
        print(f"⚠️ [缓存] token 已失效: {resp.get('message') or json_preview(resp, 300)}")
    except Exception as exc:
        print(f"⚠️ [缓存] 验证异常: {exc}")
    return False


def login_with_cache(
    server: str,
    proxies: Dict[str, str] | None,
) -> Tuple[str, str, str, Dict[str, Any] | None]:
    cached = get_cached_account(server)
    if cached and check_cached_account(server, cached, proxies):
        return cached["token"], cached["memberId"], cached["phoneNumber"], None

    code = get_code(server)
    if not code:
        return "", "", "", None

    token, open_id, _ = login_by_code(server, code, proxies)
    if not token:
        return "", "", "", None

    code = get_code(server)
    if not code:
        return "", "", "", None

    login_token, member_id, phone_number, raw = auto_login_by_code(server, code, token, open_id, proxies)
    if not login_token or not member_id or not phone_number:
        return "", "", "", raw

    set_cached_account(server, login_token, member_id, phone_number, raw)
    return login_token, member_id, phone_number, raw


def api_get(
    server: str,
    url: str,
    token: str,
    member_id: str,
    phone_number: str,
    proxies: Dict[str, str] | None,
) -> Dict[str, Any]:
    headers = common_headers(token=token, member_id=member_id, phone_number=phone_number)
    response = request_with_proxy(
        "GET",
        url,
        headers=headers,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "status": -1,
            "message": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(
    server: str,
    url: str,
    token: str,
    member_id: str,
    phone_number: str,
    params: Dict[str, Any],
    proxies: Dict[str, str] | None,
) -> Tuple[Dict[str, Any], str, str]:
    sign_headers, serialized = build_sign_header(url, params)
    headers = common_headers(token=token, member_id=member_id, phone_number=phone_number)
    headers.update(sign_headers)
    body = encrypt_body(params, sign_headers["timestamp"])

    response = request_with_proxy(
        "POST",
        url,
        headers=headers,
        json=body,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json(), sign_headers["timestamp"], serialized
    except Exception:
        return {
            "status": -1,
            "message": f"JSON解析失败: {response.text[:300]}",
        }, sign_headers["timestamp"], serialized


def do_sign(
    server: str,
    token: str,
    member_id: str,
    phone_number: str,
    proxies: Dict[str, str] | None,
) -> Tuple[str, bool]:
    rules = api_get(
        server,
        f"{SIGN_RULES_URL}?memberId={quote(member_id)}&phoneNumber={quote(phone_number)}",
        token=token,
        member_id=member_id,
        phone_number=phone_number,
        proxies=proxies,
    )
    if rules.get("status") not in (0, 200, "0", "200"):
        message = rules.get("message") or rules.get("msg") or json_preview(rules, 300)
        if is_token_error(message):
            return f"token失效: {message}", False
        return f"签到状态获取失败: {message}", False

    rule_data = safe_data(rules)
    if str(rule_data.get("isSign") or "").upper() == "Y":
        print("✅ [签到] 今日已签到")
        return "今日已签到", True

    point = rule_data.get("point")
    sign_url = f"{SIGN_URL}?memberId={quote(member_id)}"
    sign_resp, timestamp, serialized = api_post(
        server,
        sign_url,
        token=token,
        member_id=member_id,
        phone_number=phone_number,
        params={"memberId": member_id},
        proxies=proxies,
    )

    if sign_resp.get("status") in (0, 200, "0", "200"):
        message = str(safe_data(sign_resp) or sign_resp.get("message") or "签到成功")
        if point is not None:
            message = f"{message}，积分+{point}"
        print(f"✅ [签到] {message}")
        return message, True

    message = sign_resp.get("message") or sign_resp.get("msg") or json_preview(sign_resp, 300)
    if any(key in message for key in ("已签到", "重复签到", "签到过", "请勿重复签到")):
        print(f"✅ [签到] {message}")
        return message, True
    if is_token_error(message):
        return f"token失效: {message}", False
    print(f"⚠️ [签到] {message}")
    return f"签到失败: {message}", False


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "memberId": "-",
        "phone": "-",
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

    token, member_id, phone_number, raw = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw)}"
        return result

    result["token"] = mask(token)
    result["memberId"] = member_id
    result["phone"] = mask(phone_number)

    try:
        sign_msg, ok = do_sign(server, token, member_id, phone_number, proxies)
        result["signMsg"] = sign_msg
        result["success"] = ok
        if not ok and is_token_error(sign_msg):
            print("🔁 [刷新] token 失效，重新获取 code 后重试")
            code = get_code(server)
            if code:
                new_token, new_open_id, _ = login_by_code(server, code, proxies)
                if new_token:
                    code = get_code(server)
                    if code:
                        new_login_token, new_member_id, new_phone, raw2 = auto_login_by_code(
                            server, code, new_token, new_open_id, proxies
                        )
                        if new_login_token and new_member_id and new_phone:
                            set_cached_account(server, new_login_token, new_member_id, new_phone, raw2)
                            result["token"] = mask(new_login_token)
                            result["memberId"] = new_member_id
                            result["phone"] = mask(new_phone)
                            sign_msg, ok = do_sign(server, new_login_token, new_member_id, new_phone, proxies)
                            result["signMsg"] = sign_msg
                            result["success"] = ok
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result

    return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""💠 印象星会员签到任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🌍 来源：{res["server"]}
🌐 代理：{res["proxyStatus"]}
📡 出口IP：{res["proxyIp"]}
🔐 Token：{res["token"]}
👤 会员ID：{res["memberId"]}
📱 手机：{res["phone"]}
📝 签到：{res["signMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    if not SERVERS:
        print("❌ 未配置 YYB_SERVER，格式：地址@账号ID或OpenID，每行一个")
        return

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
                "memberId": "-",
                "phone": "-",
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
    print("║ 🏁 印象星任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("💠 印象星会员签到任务完成", build_notify(results))


if __name__ == "__main__":
    main()
