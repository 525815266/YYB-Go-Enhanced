#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
爱裹旧衣服回收动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /recy/api/user/identityIdByAuthCode 使用 code 换 token（md5 签名）
  3. 每日签到
  4. 每日抽奖参与
  5. 查询积分与环保金余额
  6. 环保金满 0.3 元且绑定收款账户自动提现
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  AG_CHANNEL_NO     渠道号，默认空

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import hashlib
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from yyb_account_guard import filter_accounts, update_from_result

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


APP_NAME = "爱裹旧衣服回收小程序"
APPID = "wx4ff260333d3c5ddd"

# YYB 服务列表，格式为“地址@账号ID或OpenID”，每行一个账号
_SERVER_ENV = os.getenv("YYB_SERVER", "").strip()
SERVERS = [item.strip() for item in _SERVER_ENV.replace(",", "\n").splitlines() if item.strip()]
SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)
YYB_API_KEY = os.getenv("YYB_API_KEY", "").strip()

CHANNEL_NO = os.getenv("AG_CHANNEL_NO", "")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://alipay.haliaeetus.cn"
CURRENT_TIME_URL = f"{BASE_URL}/fuli/currentTime"
LOGIN_URL = f"{BASE_URL}/recy/api/user/identityIdByAuthCode"

SIGN_INFO_URL = f"{BASE_URL}/fuli/api/fuli/signedInfo"
SIGN_URL = f"{BASE_URL}/fuli/api/fuli/signed"
SIGN_TYPE_URL = f"{BASE_URL}/fuli/api/fuli/signType"
DRAW_LIST_URL = f"{BASE_URL}/fuli/api/drawDay/list/start"
DRAW_NO_LIST_URL = f"{BASE_URL}/fuli/api/drawDay/list/no/start"
DRAW_JOIN_URL = f"{BASE_URL}/fuli/api/drawDay/user/join"
DRAW_REMIND_URL = f"{BASE_URL}/fuli/api/drawDay/user/remind"
DRAW_USER_RECORD_URL = f"{BASE_URL}/fuli/api/drawDay/user/listUserRecodByDrawId"
DRAW_REMIND_LIST_URL = f"{BASE_URL}/fuli/api/drawDay/user/listRemindByDrawId"
ACCOUNT_URL = f"{BASE_URL}/fuli/api/jf/account"
COUNT_ASSET_URL = f"{BASE_URL}/recy/api/auth/asset/countAsset"
GET_INFO_URL = f"{BASE_URL}/recy/api/auth/asset/getInfo"
WITHDRAW_URL = f"{BASE_URL}/recy/api/auth/asset/cashout"

CASHOUT_SALT = "%*$##@88990"
WITHDRAW_MIN = 0.3

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agcookie.json")

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
    print("║ ♻️ 爱裹旧衣服回收动态 code 版               ║")
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



def request_retry(method: str, url: str, session: requests.Session | None = None, **kwargs) -> requests.Response:
    """发起请求；证书校验失败时自动关闭校验重试一次"""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    try:
        if session is None:
            return requests.request(method, url, **kwargs)
        return session.request(method, url, **kwargs)
    except requests.exceptions.SSLError as exc:
        print(f"⚠️ [SSL] 证书校验失败，关闭校验重试: {exc}")
        kwargs["verify"] = False
        if session is None:
            return requests.request(method, url, **kwargs)
        return session.request(method, url, **kwargs)


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
        response = request_retry("GET", PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
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
            try:
                return requests.request(method, url, proxies=proxies, **kwargs)
            except requests.exceptions.SSLError as exc:
                print(f"⚠️ [代理] {server} 证书校验失败，关闭校验重试: {exc}")
                return requests.request(method, url, proxies=proxies, verify=False, **kwargs)
        except Exception as exc:
            print(f"⚠️ [代理] {server} 代理请求失败: {exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            print("🔁 [兜底] 切换直连重试")

    session = direct_session()
    try:
        return session.request(method, url, **kwargs)
    except requests.exceptions.SSLError as exc:
        print(f"⚠️ [SSL] {server} 证书校验失败，关闭校验重试: {exc}")
        return session.request(method, url, verify=False, **kwargs)


def send_pushplus(title: str, content: str) -> None:
    if not PLUSPLUS_TOKEN:
        print("⚠️ [PushPlus] 未配置 PLUSPLUS_TOKEN，跳过推送")
        return

    try:
        request_retry(
            "POST",
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
        response = request_retry(
            "POST",
            url,
            session=direct_session(),
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
        "plateForm": "WX",
        "channelNo": CHANNEL_NO,
        "Referer": f"https://servicewechat.com/{APPID}/267/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["Authorization"] = token
    return headers


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


def sign_md5(raw: str) -> str:
    """小程序 sign 算法：将字符串按字符排序后取 md5"""
    return hashlib.md5("".join(sorted(raw)).encode("utf-8")).hexdigest()


def fetch_current_time() -> int:
    response = request_retry("GET", CURRENT_TIME_URL, session=direct_session(), timeout=15)
    return int(response.text.strip())


def build_signed_body(data: Dict[str, Any], salt: str = "") -> Dict[str, Any]:
    m = fetch_current_time()
    raw = str(m) + json.dumps(data, separators=(",", ":"), ensure_ascii=False) + salt
    return {"data": data, "m": m, "s": sign_md5(raw)}


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json=build_signed_body({"authCode": code}),
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
    """优先使用缓存 token（签到信息接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            sign_resp = api_get(server, SIGN_INFO_URL, cache_token, proxies)
            if sign_resp.get("status") == 200 and isinstance(sign_resp.get("data"), dict):
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
    set_cached_token(server, token, expire_time)
    return token, raw_login


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "lotteryMsg": "-",
        "remindMsg": "-",
        "balance": "-",
        "withdrawMsg": "-",
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
        # ====================== 每日签到 ======================
        sign_info = api_get(server, SIGN_INFO_URL, token, proxies)
        if sign_info.get("status") == 200:
            sign_data = safe_data(sign_info)
            if sign_data.get("isSign"):
                result["signMsg"] = f"今日已签到，连续 {sign_data.get('signDays') or 0} 天"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                sign_resp = api_get(server, SIGN_URL, token, proxies)
                if sign_resp.get("status") == 200:
                    result["signMsg"] = "每日签到成功"
                    print(f"✅ [签到] {result['signMsg']}")
                else:
                    result["signMsg"] = sign_resp.get("msg") or "签到失败"
                    print(f"⚠️ [签到] {result['signMsg']}")
        else:
            result["signMsg"] = sign_info.get("msg") or "签到状态查询失败"
            print(f"⚠️ [签到] {result['signMsg']}")

        # ====================== 每日抽奖（参与） ======================
        draw_list = api_post(server, DRAW_LIST_URL, token, proxies, {"page": {"limit": 1, "page": 1}})
        if draw_list.get("status") != 200:
            result["lotteryMsg"] = draw_list.get("msg") or "抽奖列表获取失败"
            print(f"⚠️ [抽奖] {result['lotteryMsg']}")
        else:
            draw_items = draw_list.get("data") or []
            if not draw_items:
                result["lotteryMsg"] = "暂无进行中的抽奖活动"
                print(f"⚠️ [抽奖] {result['lotteryMsg']}")
            else:
                draw = draw_items[0]
                draw_id = draw.get("id")
                draw_name = draw.get("goodName") or f"活动{draw_id}"
                part_in = draw.get("partInNum") or 0
                max_part = draw.get("maxPartInNum") or 0
                print(f"🎰 [抽奖] {draw_name} 参与人数 {part_in}/{max_part}")

                already = False
                record_resp = api_post(server, DRAW_USER_RECORD_URL, token, proxies, {"data": draw_id})
                if record_resp.get("status") == 200:
                    for record in record_resp.get("data") or []:
                        record_draw = record.get("drawDay") or {}
                        if record_draw.get("id") == draw_id:
                            already = True
                            break

                if already:
                    result["lotteryMsg"] = f"{draw_name} 已参与"
                    print(f"✅ [抽奖] {result['lotteryMsg']}")
                else:
                    join_resp = api_post(server, DRAW_JOIN_URL, token, proxies, build_signed_body(draw_id))
                    if join_resp.get("status") == 200:
                        result["lotteryMsg"] = f"{draw_name} 参与成功"
                        print(f"✅ [抽奖] {result['lotteryMsg']}")
                    else:
                        result["lotteryMsg"] = join_resp.get("msg") or f"{draw_name} 参与失败"
                        print(f"⚠️ [抽奖] {result['lotteryMsg']}")

        # ====================== 待开始活动预约 ======================
        no_list = api_post(server, DRAW_NO_LIST_URL, token, proxies, {"page": {"limit": 100, "page": 1}})
        if no_list.get("status") != 200:
            result["remindMsg"] = no_list.get("msg") or "待开始列表获取失败"
            print(f"⚠️ [预约] {result['remindMsg']}")
        else:
            no_items = no_list.get("data") or []
            if not no_items:
                result["remindMsg"] = "暂无待开始活动"
                print(f"⚠️ [预约] {result['remindMsg']}")
            else:
                no_ids = [item.get("id") for item in no_items if item.get("id")]
                reminded_ids = set()
                remind_list_resp = api_post(
                    server,
                    DRAW_REMIND_LIST_URL,
                    token,
                    proxies,
                    {"data": ",".join(str(draw_id) for draw_id in no_ids)},
                )
                if remind_list_resp.get("status") == 200:
                    for record in remind_list_resp.get("data") or []:
                        if record.get("drawDayId") is not None:
                            reminded_ids.add(record.get("drawDayId"))

                remind_messages = []
                for item in no_items:
                    remind_draw_id = item.get("id")
                    remind_name = item.get("goodName") or f"活动{remind_draw_id}"
                    if remind_draw_id in reminded_ids:
                        remind_messages.append(f"{remind_name} 已预约")
                        print(f"✅ [预约] {remind_name} 已预约")
                        continue
                    remind_resp = api_post(
                        server,
                        DRAW_REMIND_URL,
                        token,
                        proxies,
                        build_signed_body(remind_draw_id),
                    )
                    if remind_resp.get("status") == 200:
                        remind_messages.append(f"{remind_name} 预约成功")
                        print(f"✅ [预约] {remind_name} 预约成功")
                    else:
                        msg = remind_resp.get("msg") or "预约失败"
                        remind_messages.append(f"{remind_name} 预约失败:{msg}")
                        print(f"⚠️ [预约] {remind_name} 预约失败: {msg}")
                    sleep(random.randint(1, 2))
                result["remindMsg"] = "、".join(remind_messages)

        # ====================== 余额查询 ======================
        points_resp = api_post(server, ACCOUNT_URL, token, proxies, {})
        points = points_resp.get("data") if points_resp.get("status") == 200 else 0

        asset_resp = api_post(server, COUNT_ASSET_URL, token, proxies, build_signed_body({}))
        asset_data = safe_data(asset_resp)
        total_asset = to_float(asset_data.get("total"))
        frozen_asset = to_float(asset_data.get("frozenAsset"))
        available = round(total_asset - frozen_asset, 2)

        result["balance"] = f"积分 {points} / 环保金 {total_asset:.2f}（冻结 {frozen_asset:.2f}，可提 {available:.2f}）"
        print(f"💰 [余额] {result['balance']} 元")

        # ====================== 提现 ======================
        if available < WITHDRAW_MIN:
            result["withdrawMsg"] = f"环保金可提 {available:.2f} 元，未满 {WITHDRAW_MIN} 元，跳过提现"
            print(f"⚠️ [提现] {result['withdrawMsg']}")
            result["success"] = True
            return result

        info_resp = api_post(server, GET_INFO_URL, token, proxies, {})
        info_data = safe_data(info_resp)
        alipay_account = info_data.get("alipayAccount") or ""

        if not alipay_account:
            result["withdrawMsg"] = "未绑定收款账户，跳过提现"
            print(f"⚠️ [提现] {result['withdrawMsg']}")
            result["success"] = True
            return result

        withdraw_payload = build_signed_body(
            {"payType": "WX_PAY_V3_PRO", "amount": available},
            salt=CASHOUT_SALT,
        )
        withdraw_resp = api_post(server, WITHDRAW_URL, token, proxies, withdraw_payload)
        result["withdrawMsg"] = (
            withdraw_resp.get("msg")
            or withdraw_resp.get("message")
            or json_preview(withdraw_resp)
        )
        print(f"💸 [提现] {result['withdrawMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""♻️ 爱裹旧衣服回收任务结果

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
🎰 抽奖：{res["lotteryMsg"]}
📅 预约：{res["remindMsg"]}
💰 余额：{res["balance"]} 元
💸 提现：{res["withdrawMsg"]}
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
            update_from_result(server, result, app_id=APPID)
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
                "lotteryMsg": "-",
                "remindMsg": "-",
                "balance": "-",
                "withdrawMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 爱裹旧衣服回收任务执行完成                ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("♻️ 爱裹旧衣服回收任务完成", build_notify(results))


if __name__ == "__main__":
    main()
