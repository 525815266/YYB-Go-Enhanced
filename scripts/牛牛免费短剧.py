#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
牛牛免费短剧 App 动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /api/auth/wxlogin 使用 code 换 token
  3. 每日签到
  4. 做任务：新用户、宝箱、看广告、吃饭补签、分享、点赞、收藏、播放上报
  5. 查询余额/金币
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN      PushPlus token，可选
  PROXY_API           品赞代理提取 API，可选
  PROXY_TYPE          http / socks5，默认 http
  YYB_SERVER          YYB 地址@账号 ID/OpenID，每行一个账号
  YYB_API_KEY         YYB API Key，可选
  NIUNIU_RECOMMEND    邀请码，可选
  NIUNIU_AD_MAX       看广告最大次数，默认 60
  NIUNIU_WATCH_SHORT_MAX  看短剧最大上报次数，默认 90

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
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, urlencode

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


APP_NAME = "牛牛免费短剧"
APPID = "wxf9dff9c61e8d3ee0"
DEVICE_ID = "04ac35443df46561"
DEFAULT_MOVIE_ID = 85480
DEFAULT_TYPE_ID = "S1"

_SERVER_ENV = os.getenv("YYB_SERVER", "").strip()
SERVERS = [item.strip() for item in _SERVER_ENV.replace(",", "\n").splitlines() if item.strip()]
YYB_API_KEY = os.getenv("YYB_API_KEY", "").strip()

RECOMMEND = os.getenv("NIUNIU_RECOMMEND", "")
AD_MAX = int(os.getenv("NIUNIU_AD_MAX", "60"))
WATCH_SHORT_MAX = int(os.getenv("NIUNIU_WATCH_SHORT_MAX", "90"))
HISTORY_MAX = int(os.getenv("NIUNIU_HISTORY_MAX", "5"))

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_RETRY_TIMES = 3
REQUEST_RETRY_DELAY = 2
REQUEST_TIMEOUT = 30

BASE_URL = "https://new.tianjinzhitongdaohe.com"
LOGIN_URL = f"{BASE_URL}/api/auth/wxlogin"
USER_INFO_URL = f"{BASE_URL}/api/v1/app/user/userInfo"
WELFARE_LIST_URL = f"{BASE_URL}/api/v1/app/welfare/list"
SIGN_URL = f"{BASE_URL}/api/v1/app/welfare/sign"
NEW_USER_URL = f"{BASE_URL}/api/v1/app/welfare/newUser"
NEW_USER_SEVEN_URL = f"{BASE_URL}/api/v1/app/welfare/newUserSeven"
TREASURE_DURATION_URL = f"{BASE_URL}/api/v1/app/welfare/treasureDuration"
TREASURE_OPEN_URL = f"{BASE_URL}/api/v1/app/welfare/treasureOpen"
WATCH_SHORT_COIN_URL = f"{BASE_URL}/api/v1/app/welfare/watchShortCoin"
WATCH_AD_URL = f"{BASE_URL}/api/v1/app/welfare/watchAd"
REPAIR_SIGN_EAT_URL = f"{BASE_URL}/api/v1/app/welfare/repairSignEat"
SIGN_EAT_URL = f"{BASE_URL}/api/v1/app/welfare/signEat"
SCREEN_MOVIE_URL = f"{BASE_URL}/api/v1/app/screen/screenMovie"
ADD_SHARE_URL = f"{BASE_URL}/api/v1/app/welfare/addShare"
HOT_SHORT_URL = f"{BASE_URL}/api/v1/app/welfare/hotShort"
PRAISE_URL = f"{BASE_URL}/api/v1/app/play/praiseMovie"
COLLECT_URL = f"{BASE_URL}/api/v1/app/play/collectMovie"
HISTORY_URL = f"{BASE_URL}/api/v1/app/play/historyMovie"
TOTAL_PRAISE_URL = f"{BASE_URL}/api/v1/app/play/totalPraise"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "niuniucookie.json")

USER_AGENT = "okhttp/4.12.0"

RSA_N = 107522656505270929442724722581058512110651253175247427825892093018524390746333815199881223296682736299955920009272663468531492738508249602683965927534752779675815517024639833341539225989849406517400535816502936660472399031815864420101596018171147542106874509169801562754992389695564473176300531216996943688261
RSA_E = 65537
RSA_KEY_BYTES = 128


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


def safe_data(resp: Dict[str, Any]) -> Any:
    """Safely extract 'data' from an API response."""
    return resp.get("data")


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🐂 牛牛免费短剧动态 code 版                   ║")
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

    last_exc: Exception | None = None
    for attempt in range(1, REQUEST_RETRY_TIMES + 1):
        try:
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
        except Exception as exc:
            last_exc = exc
            print(f"⚠️ [网络] {server} 第 {attempt}/{REQUEST_RETRY_TIMES} 次请求失败: {exc}")
            if attempt < REQUEST_RETRY_TIMES:
                sleep(REQUEST_RETRY_DELAY)

    raise last_exc


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


def build_sign(timestamp: str, params: str) -> str:
    raw = f"SaltLSFBTimestamp{timestamp}Params{params}ClientappDeviceId{DEVICE_ID}"
    encoded = base64.b64encode(raw.encode("utf-8"))
    return hashlib.md5(encoded).hexdigest().upper()


def rsa_encrypt(plain: str) -> str:
    data = plain.encode("utf-8")
    max_data_len = RSA_KEY_BYTES - 11
    if len(data) > max_data_len:
        raise ValueError("RSA plaintext too long")

    while True:
        padding = os.urandom(RSA_KEY_BYTES - len(data) - 3)
        if b"\x00" not in padding:
            break

    em = b"\x00\x02" + padding + b"\x00" + data
    encrypted = pow(int.from_bytes(em, "big"), RSA_E, RSA_N)
    return base64.b64encode(encrypted.to_bytes(RSA_KEY_BYTES, "big")).decode("utf-8")


def key_body(payload: Dict[str, Any]) -> str:
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return json.dumps({"key": rsa_encrypt(plain)}, ensure_ascii=False, separators=(",", ":"))


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


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "client": "app",
        "deviceid": DEVICE_ID,
        "devicetype": "Android",
    }
    if token:
        headers["token"] = token
    return headers


def parse_json(response: requests.Response) -> Dict[str, Any]:
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def extract_token(data: Any) -> str | None:
    if isinstance(data, str):
        return data.strip() or None

    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
        data.get("jwt"),
    ]

    inner = data.get("data")
    if isinstance(inner, str):
        candidates.append(inner)
    elif isinstance(inner, dict):
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
            "GET",
            LOGIN_URL,
            headers={
                "User-Agent": USER_AGENT,
                "client": "app",
                "deviceId": DEVICE_ID,
                "deviceType": "Android",
                "Accept": "*/*",
            },
            params={
                "code": code,
                "recommend": RECOMMEND,
            },
            proxies=proxies,
            server=server,
        )

        data = parse_json(response)
        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token),
        params=params,
        proxies=proxies,
        server=server,
    )
    return parse_json(response)


def api_post(
    server: str,
    url: str,
    token: str,
    proxies: Dict[str, str] | None,
    *,
    data: str | None = None,
    json_data: Dict[str, Any] | None = None,
    headers: Dict[str, str] | None = None,
    sign_params: str | None = None,
    params: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    request_headers = common_headers(token)
    if headers:
        request_headers.update(headers)

    if sign_params is not None:
        timestamp = str(int(time.time()))
        request_headers["timestamp"] = timestamp
        request_headers["sign"] = build_sign(timestamp, sign_params)

    response = request_with_proxy(
        "POST",
        url,
        headers=request_headers,
        data=data,
        json=json_data,
        params=params,
        proxies=proxies,
        server=server,
    )
    return parse_json(response)


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
    """优先使用缓存 token（用户信息接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            account_resp = api_get(server, USER_INFO_URL, cache_token, proxies)
            if account_resp.get("code") == 200:
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


# ====================== 业务任务 ======================
def find_welfare(data: Any, flag: str) -> Dict[str, Any] | None:
    for item in data or []:
        if item.get("flag") == flag:
            return item
    return None


def claim_no_body_task(
    server: str,
    token: str,
    proxies: Dict[str, str] | None,
    url: str,
    label: str,
) -> str:
    resp = api_post(
        server,
        url,
        token,
        proxies,
        data="{}",
        headers={"Content-Type": "application/json; charset=UTF-8"},
        sign_params="",
    )
    if resp.get("code") == 200:
        reward = resp.get("data")
        msg = f"{label}成功"
        if reward not in (None, ""):
            msg += f"：+{reward}"
        print(f"✅ [任务] {msg}")
        return msg

    msg = resp.get("msg") or resp.get("message") or f"{label}未领取"
    print(f"⚠️ [任务] {label}: {msg}")
    return f"{label}：{msg}"


def claim_repair_sign_eat(server: str, token: str, proxies: Dict[str, str] | None, welfare_list: Any) -> List[str]:
    messages: List[str] = []
    item = find_welfare(welfare_list, "WATCH_EAT_COIN")
    if not item:
        return messages

    for task in item.get("taskList") or []:
        config_id = task.get("configId")
        if not config_id or not task.get("isDone") or task.get("isReceive"):
            continue

        resp = api_post(
            server,
            REPAIR_SIGN_EAT_URL,
            token,
            proxies,
            data=f"configId={config_id}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            sign_params=f"configId{config_id}",
        )
        if resp.get("code") == 200:
            msg = f"吃饭补签 configId={config_id} 成功"
            messages.append(msg)
            print(f"✅ [任务] {msg}")
        else:
            msg = resp.get("msg") or resp.get("message") or f"configId={config_id} 领取失败"
            messages.append(f"configId={config_id}：{msg}")
            print(f"⚠️ [任务] 吃饭补签 {msg}")

    return messages


def claim_watch_ad(server: str, token: str, proxies: Dict[str, str] | None, welfare_list: Any) -> Tuple[int, str]:
    item = find_welfare(welfare_list, "WATCH_AD_COIN")
    ad_id = item.get("taskNum") if item else None

    total_coins = 0
    success_count = 0
    last_msg = ""

    for index in range(1, AD_MAX + 1):
        if ad_id:
            body = key_body({"adId": ad_id})
            sign_params = f"adId{ad_id}"
        else:
            body = "{}"
            sign_params = None

        resp = api_post(
            server,
            WATCH_AD_URL,
            token,
            proxies,
            data=body,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            sign_params=sign_params,
        )

        if resp.get("code") == 200:
            reward = resp.get("data")
            if isinstance(reward, (int, float)) and reward > 0:
                total_coins += int(reward)
                success_count += 1
                last_msg = f"第 {index} 次看广告 +{int(reward)} 金币"
                print(f"✅ [看广告] {last_msg}")
            else:
                last_msg = f"第 {index} 次看广告已处理"
                print(f"✅ [看广告] {last_msg}")
        else:
            last_msg = resp.get("msg") or resp.get("message") or f"第 {index} 次失败"
            print(f"⚠️ [看广告] {last_msg}")
            break

        wait_time = random.randint(2, 4)
        print(f"⏳ [看广告] 等待 {wait_time}s")
        sleep(wait_time)

    if success_count:
        return total_coins, f"看广告 {success_count} 次，累计 +{total_coins} 金币"
    return 0, last_msg or "看广告未执行"


def get_welfare_item_progress(server: str, token: str, proxies: Dict[str, str] | None, flag: str) -> Tuple[Any, Any, Dict[str, Any] | None]:
    resp = api_post(
        server,
        WELFARE_LIST_URL,
        token,
        proxies,
        data="{}",
        headers={"Content-Type": "application/json; charset=UTF-8"},
    )
    item = find_welfare(resp.get("data") or [], flag)
    if not item:
        return None, None, None
    return item.get("extraProgress"), item.get("extraReached"), item


def fetch_screen_movie_ids(server: str, token: str, proxies: Dict[str, str] | None) -> List[int]:
    resp = api_post(
        server,
        SCREEN_MOVIE_URL,
        token,
        proxies,
        json_data={"condition": {"typeId": "S1"}, "pageNum": 1, "pageSize": 40},
        headers={"Content-Type": "application/json; charset=UTF-8"},
    )
    records = (resp.get("data") or {}).get("records") or []
    ids = [int(rec["id"]) for rec in records if rec.get("id")]
    return ids or [DEFAULT_MOVIE_ID]


def claim_watch_ad_full(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[int, str]:
    progress, reached, item = get_welfare_item_progress(server, token, proxies, "WATCH_AD_COIN")
    ad_id = item.get("taskNum") if item else None
    if progress is not None and reached is not None:
        progress = int(progress or 0)
        reached = int(reached or 17)
        if progress >= reached:
            msg = f"看视频领金币进度 {progress}/{reached}，已完成"
            print(f"✅ [看广告] {msg}")
            return 0, msg

    total_coins = 0
    success_count = 0
    consecutive_fail = 0
    last_msg = ""
    for index in range(1, AD_MAX + 1):
        body = key_body({"adId": ad_id}) if ad_id else "{}"
        sign_params = f"adId{ad_id}" if ad_id else None
        resp = api_post(
            server,
            WATCH_AD_URL,
            token,
            proxies,
            data=body,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            sign_params=sign_params,
        )
        if resp.get("code") == 200:
            reward = resp.get("data")
            if isinstance(reward, (int, float)) and reward > 0:
                total_coins += int(reward)
                success_count += 1
                consecutive_fail = 0
                last_msg = f"第 {index} 次看广告 +{int(reward)} 金币"
                print(f"✅ [看广告] {last_msg}")
            else:
                last_msg = f"第 {index} 次看广告已处理"
                print(f"✅ [看广告] {last_msg}")
        else:
            last_msg = resp.get("msg") or resp.get("message") or f"第 {index} 次失败"
            consecutive_fail += 1
            print(f"⚠️ [看广告] {last_msg}")
            if "频繁" not in last_msg and "稍后" not in last_msg:
                break

        wait_time = random.randint(3, 5)
        print(f"⏳ [看广告] 等待 {wait_time}s")
        sleep(wait_time)
        if index % 2 == 0:
            progress, reached, _ = get_welfare_item_progress(server, token, proxies, "WATCH_AD_COIN")
            if progress is not None and reached is not None and int(progress or 0) >= int(reached or 17):
                print(f"✅ [看广告] 进度已达 {progress}/{reached}")
                break
        if consecutive_fail >= 8:
            print("⚠️ [看广告] 连续失败过多，暂停本轮")
            break

    progress, reached, _ = get_welfare_item_progress(server, token, proxies, "WATCH_AD_COIN")
    if progress is not None and reached is not None and int(progress or 0) >= int(reached or 17):
        last_msg = f"看视频领金币进度 {progress}/{reached}，已完成"
    elif success_count:
        last_msg = f"看广告 {success_count} 次，累计 +{total_coins} 金币，进度 {progress}/{reached}"
    return total_coins, last_msg


def claim_sign_eat_now(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    timestamp = str(int(time.time()))
    sign_value = build_sign(timestamp, "")
    last_msg = ""
    for attempt in range(1, 4):
        resp = api_post(
            server,
            SIGN_EAT_URL,
            token,
            proxies,
            params={"timestamp": timestamp, "sign": sign_value},
            data="{}",
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )
        if resp.get("code") == 200:
            msg = "吃饭看剧补贴领取成功"
            print(f"✅ [吃饭补贴] {msg}")
            return msg
        last_msg = resp.get("msg") or resp.get("message") or f"第 {attempt} 次领取失败"
        print(f"⚠️ [吃饭补贴] {last_msg}")
        if "频繁" not in last_msg and "稍后" not in last_msg:
            break
        sleep(3)
    return f"吃饭补贴：{last_msg}"


def claim_daily_praise_collect(server: str, token: str, proxies: Dict[str, str] | None) -> List[str]:
    messages: List[str] = []
    movie_ids = fetch_screen_movie_ids(server, token, proxies)
    specs = [
        ("THUMBS_UP_EPISODE", "点赞剧情", do_praise),
        ("COLLECT_EPISODE", "收藏新剧", do_collect),
    ]
    for flag, name, action in specs:
        progress, reached, _ = get_welfare_item_progress(server, token, proxies, flag)
        if progress is not None and reached is not None and int(progress or 0) >= int(reached or 2):
            msg = f"{name}进度 {progress}/{reached}，已完成"
            messages.append(msg)
            print(f"✅ [{name}] {msg}")
            continue
        for movie_id in movie_ids[:12]:
            messages.append(action(server, token, proxies, movie_id, DEFAULT_TYPE_ID))
            sleep(random.randint(2, 4))
            progress, reached, _ = get_welfare_item_progress(server, token, proxies, flag)
            print(f"📺 [{name}] 当前进度: {progress} / {reached}")
            if progress is not None and reached is not None and int(progress or 0) >= int(reached or 2):
                break
        if progress is not None and reached is not None and int(progress or 0) >= int(reached or 2):
            messages.append(f"{name}进度 {progress}/{reached}，已完成")
    return messages

def pick_movie_id(server: str, token: str, proxies: Dict[str, str] | None, welfare_list: Any) -> int:
    hot = find_welfare(welfare_list, "HOT_SHORT")
    if hot:
        short_list = hot.get("shortList") or []
        if short_list and short_list[0].get("id"):
            return int(short_list[0]["id"])
    return DEFAULT_MOVIE_ID


def do_add_share(server: str, token: str, proxies: Dict[str, str] | None, movie_id: int, type_id: str = DEFAULT_TYPE_ID) -> str:
    resp = api_post(
        server,
        ADD_SHARE_URL,
        token,
        proxies,
        data=urlencode({"typeId": type_id, "movieId": movie_id}),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        sign_params=f"typeId{type_id}movieId{movie_id}",
    )
    if resp.get("code") == 200:
        msg = f"分享短剧 {movie_id} 成功"
        print(f"✅ [分享] {msg}")
        return msg

    msg = resp.get("msg") or resp.get("message") or f"分享 {movie_id} 失败"
    print(f"⚠️ [分享] {msg}")
    return msg


def do_praise(server: str, token: str, proxies: Dict[str, str] | None, movie_id: int, type_id: str = DEFAULT_TYPE_ID) -> str:
    body = key_body({
        "action": 1,
        "episodeIndex": 0,
        "movieId": movie_id,
        "source": 0,
        "typeId": type_id,
    })
    resp = api_post(
        server,
        PRAISE_URL,
        token,
        proxies,
        data=body,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        sign_params=f"action1episodeIndex0movieId{movie_id}source0typeId{type_id}",
    )
    if resp.get("code") == 200:
        msg = f"点赞短剧 {movie_id} 成功"
        print(f"✅ [点赞] {msg}")
        return msg

    msg = resp.get("msg") or resp.get("message") or f"点赞 {movie_id} 失败"
    print(f"⚠️ [点赞] {msg}")
    return msg


def do_collect(server: str, token: str, proxies: Dict[str, str] | None, movie_id: int, type_id: str = DEFAULT_TYPE_ID) -> str:
    body = key_body({
        "action": 1,
        "id": movie_id,
        "source": 0,
        "typeId": type_id,
    })
    resp = api_post(
        server,
        COLLECT_URL,
        token,
        proxies,
        data=body,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        sign_params=f"action1id{movie_id}source0typeId{type_id}",
    )
    if resp.get("code") == 200:
        msg = f"收藏短剧 {movie_id} 成功"
        print(f"✅ [收藏] {msg}")
        return msg

    msg = resp.get("msg") or resp.get("message") or f"收藏 {movie_id} 失败"
    print(f"⚠️ [收藏] {msg}")
    return msg


def _old_get_watch_short_progress(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[int, int, Dict[str, Any]]:
    resp = api_post(
        server,
        WATCH_SHORT_COIN_URL,
        token,
        proxies,
        data="{}",
        headers={"Content-Type": "application/json; charset=UTF-8"},
    )
    data = resp.get("data") or {}
    try:
        progress = int(data.get("extraProgress") or 0)
    except (TypeError, ValueError):
        progress = 0
    try:
        reached = int(data.get("extraReached") or 30)
    except (TypeError, ValueError):
        reached = 30
    return progress, reached, resp


def _old_do_watch_short(server: str, token: str, proxies: Dict[str, str] | None, movie_id: int, type_id: str = DEFAULT_TYPE_ID) -> Tuple[int, str]:
    progress, reached, _ = get_watch_short_progress(server, token, proxies)
    if progress >= reached:
        msg = f"看短剧进度 {progress}/{reached}，已完成"
        print(f"📺 [看短剧] {msg}")
        return 0, msg

    success_count = 0
    consecutive_fail = 0
    last_msg = ""
    movie_ids = [movie_id]
    if movie_id != DEFAULT_MOVIE_ID:
        movie_ids.append(DEFAULT_MOVIE_ID)

    for work_movie_id in movie_ids:
        if progress >= reached:
            break
        print(f"🎬 [看短剧] 使用上报 ID {work_movie_id}，当前进度 {progress}/{reached}")

        for index in range(1, WATCH_SHORT_MAX + 1):
            episode = str((index - 1) % 40 + 1)
            body = key_body({
                "episode": episode,
                "id": work_movie_id,
                "playerId": "",
                "typeId": type_id,
                "watchDuration": "60",
            })
            resp = api_post(
                server,
                HISTORY_URL,
                token,
                proxies,
                data=body,
                headers={"Content-Type": "application/json; charset=UTF-8"},
                sign_params=None,
            )

            if resp.get("code") == 200:
                success_count += 1
                consecutive_fail = 0
                last_msg = f"第 {index} 次播放上报成功"
                print(f"✅ [看短剧] {last_msg}")
            else:
                msg = resp.get("msg") or resp.get("message") or f"第 {index} 次失败"
                consecutive_fail += 1
                last_msg = msg
                print(f"⚠️ [看短剧] {msg}")
                if "频繁" not in msg and "稍后" not in msg:
                    break

            wait_time = random.randint(3, 5)
            print(f"⏳ [看短剧] 等待 {wait_time}s")
            sleep(wait_time)

            if index % 3 == 0:
                progress, reached, _ = get_watch_short_progress(server, token, proxies)
                print(f"📺 [看短剧] 当前进度: {progress} / {reached}")
                if progress >= reached:
                    break
            if consecutive_fail >= 6:
                print("⚠️ [看短剧] 连续失败过多，暂停")
                break

        progress, reached, _ = get_watch_short_progress(server, token, proxies)
        if progress >= reached:
            break

    progress, reached, _ = get_watch_short_progress(server, token, proxies)
    if progress >= reached:
        msg = f"看短剧进度 {progress}/{reached}，已完成"
    else:
        msg = f"看短剧进度 {progress}/{reached}，上报成功 {success_count} 次"
    print(f"📺 [看短剧] {msg}")
    return success_count, msg


def get_watch_short_progress(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[int, int, Dict[str, Any]]:
    resp = api_post(
        server,
        WATCH_SHORT_COIN_URL,
        token,
        proxies,
        data="{}",
        headers={"Content-Type": "application/json; charset=UTF-8"},
    )
    data = resp.get("data") or {}
    try:
        progress = int(data.get("extraProgress") or 0)
    except (TypeError, ValueError):
        progress = 0
    try:
        reached = int(data.get("extraReached") or 30)
    except (TypeError, ValueError):
        reached = 30
    return progress, reached, resp


def do_watch_short(server: str, token: str, proxies: Dict[str, str] | None, movie_id: int, type_id: str = DEFAULT_TYPE_ID) -> Tuple[int, str]:
    progress, reached, _ = get_watch_short_progress(server, token, proxies)
    if progress >= reached:
        msg = f"看短剧进度 {progress}/{reached}，已完成"
        print(f"📺 [看短剧] {msg}")
        return 0, msg

    success_count = 0
    consecutive_fail = 0
    last_msg = ""
    movie_ids = [movie_id]
    if movie_id != DEFAULT_MOVIE_ID:
        movie_ids.append(DEFAULT_MOVIE_ID)

    for work_movie_id in movie_ids:
        if progress >= reached:
            break
        print(f"🎬 [看短剧] 使用上报 ID {work_movie_id}，当前进度 {progress}/{reached}")
        for index in range(1, WATCH_SHORT_MAX + 1):
            body = key_body({
                "episode": str((index - 1) % 40 + 1),
                "id": work_movie_id,
                "playerId": "",
                "typeId": type_id,
                "watchDuration": "60",
            })
            resp = api_post(
                server,
                HISTORY_URL,
                token,
                proxies,
                data=body,
                headers={"Content-Type": "application/json; charset=UTF-8"},
                sign_params=None,
            )
            if resp.get("code") == 200:
                success_count += 1
                consecutive_fail = 0
                last_msg = f"第 {index} 次播放上报成功"
                print(f"✅ [看短剧] {last_msg}")
            else:
                last_msg = resp.get("msg") or resp.get("message") or f"第 {index} 次失败"
                consecutive_fail += 1
                print(f"⚠️ [看短剧] {last_msg}")
                if "频繁" not in last_msg and "稍后" not in last_msg:
                    break

            wait_time = random.randint(3, 5)
            print(f"⏳ [看短剧] 等待 {wait_time}s")
            sleep(wait_time)

            if index % 3 == 0 or (resp.get("code") != 200 and index % 2 == 0):
                progress, reached, _ = get_watch_short_progress(server, token, proxies)
                print(f"📺 [看短剧] 当前进度: {progress} / {reached}")
                if progress >= reached:
                    break

            if consecutive_fail >= 6:
                print("⚠️ [看短剧] 连续失败过多，暂停本轮")
                break

        progress, reached, _ = get_watch_short_progress(server, token, proxies)
        if progress >= reached:
            break

    progress, reached, _ = get_watch_short_progress(server, token, proxies)
    if progress >= reached:
        msg = f"看短剧进度 {progress}/{reached}，已完成"
    else:
        msg = f"看短剧进度 {progress}/{reached}，上报成功 {success_count} 次"
    print(f"📺 [看短剧] {msg}")
    return success_count, msg

def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "taskMsg": "-",
        "balance": "-",
        "gold": "-",
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
    task_messages: List[str] = []

    try:
        user_info = api_get(server, USER_INFO_URL, token, proxies)
        if user_info.get("code") == 200:
            data = safe_data(user_info) or {}
            result["balance"] = str(data.get("balance", "-"))
            result["gold"] = str(data.get("goldBalance", "-"))
            print(f"💰 [余额] 余额: {result['balance']} 元，金币: {result['gold']}")

        welfare_list_resp = api_post(
            server,
            WELFARE_LIST_URL,
            token,
            proxies,
            data="{}",
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )
        welfare_list = welfare_list_resp.get("data") or []
        if not welfare_list:
            print(f"⚠️ [福利] 任务列表为空: {json_preview(welfare_list_resp, 300)}")

        task_messages.append(claim_no_body_task(server, token, proxies, NEW_USER_URL, "新用户红包"))
        task_messages.append(claim_no_body_task(server, token, proxies, NEW_USER_SEVEN_URL, "新用户7天"))

        treasure_resp = api_post(
            server,
            TREASURE_DURATION_URL,
            token,
            proxies,
            data="{}",
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )
        if treasure_resp.get("code") == 200:
            duration = safe_data(treasure_resp)
            print(f"🎁 [宝箱] 剩余时长: {duration}")
            if isinstance(duration, (int, float)) and duration > 0:
                task_messages.append(claim_no_body_task(server, token, proxies, TREASURE_OPEN_URL, "开启宝箱"))

        sign_resp = api_post(
            server,
            SIGN_URL,
            token,
            proxies,
            data="{}",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            sign_params="",
        )
        if sign_resp.get("code") == 200:
            reward = safe_data(sign_resp)
            result["signMsg"] = f"每日签到成功：+{reward}金币"
            print(f"✅ [签到] {result['signMsg']}")
        elif sign_resp.get("code") in (99, 2002):
            result["signMsg"] = "今日已签到"
            print(f"⚠️ [签到] {result['signMsg']}")
        else:
            result["signMsg"] = sign_resp.get("msg") or sign_resp.get("message") or "签到失败"
            print(f"⚠️ [签到] {result['signMsg']}")

        ad_coins, ad_msg = claim_watch_ad_full(server, token, proxies)
        task_messages.append(ad_msg)

        eat_messages: List[str] = []
        eat_messages.append(claim_sign_eat_now(server, token, proxies))
        eat_messages.extend(claim_repair_sign_eat(server, token, proxies, welfare_list))
        task_messages.extend(eat_messages)

        movie_id = pick_movie_id(server, token, proxies, welfare_list)
        print(f"🎬 [短剧] 使用短剧 ID: {movie_id}")

        hot_resp = api_post(
            server,
            HOT_SHORT_URL,
            token,
            proxies,
            data="{}",
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )
        short_list = []
        if hot_resp.get("code") == 200:
            hot_data = safe_data(hot_resp) or {}
            short_list = hot_data.get("shortList") or []
            for short in short_list[:2]:
                task_messages.append(do_add_share(server, token, proxies, int(short["id"]), DEFAULT_TYPE_ID))
                sleep(random.randint(4, 6))

        if short_list:
            print("⏳ [分享] 等待 4s 后继续分享当前短剧")
            sleep(4)
        task_messages.append(do_add_share(server, token, proxies, movie_id, DEFAULT_TYPE_ID))
        task_messages.extend(claim_daily_praise_collect(server, token, proxies))

        watch_count, watch_msg = do_watch_short(server, token, proxies, movie_id, DEFAULT_TYPE_ID)
        task_messages.append(watch_msg)

        result["taskMsg"] = "；".join([msg for msg in task_messages if msg])
        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🐂 牛牛免费短剧任务结果

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
📝 签到：{res["signMsg"]}
🎰 任务：{res["taskMsg"]}
💰 余额：{res["balance"]} 元
🪙 金币：{res["gold"]}
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
                "signMsg": "-",
                "taskMsg": "-",
                "balance": "-",
                "gold": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 牛牛免费短剧任务执行完成                    ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🐂 牛牛免费短剧任务完成", build_notify(results))


if __name__ == "__main__":
    main()
