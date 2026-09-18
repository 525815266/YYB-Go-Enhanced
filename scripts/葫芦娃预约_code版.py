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
葫芦娃预约（惠群白标酒水平台）动态 code 版

覆盖 8 个同体系小程序：偲源惠购 / 贵旅优品 / 空港乐购 / 航旅黔购 / 遵航出山 / 贵盐黔品 / 乐旅商城 / 驿路黔寻

功能：
  1. 本地 code 服务按各小程序 appid 分别获取微信 code
  2. 登录换 X-access-token（含缓存与自动刷新）
  3. callback 服务获取 ak/sk，业务请求 HMAC-SHA256 签名（X-HMAC-* 头）
  4. 查询用户信息与频道活动
  5. 活动进行中自动预约（checkCustomerInQianggou → appoint）
  6. 已开奖则查询中签结果
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

⚠️ 登录接口为推断，未经真机验证，失败请抓包核对

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       本地 code 服务地址，默认 127.0.0.1:8088

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import base64
import hashlib
import hmac
import json
import os
import random
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "葫芦娃预约（惠群平台）"
APPID = [
    "wxded2e7e6d60ac09d",
    "wx61549642d715f361",
    "wx613ba8ea6a002aa8",
    "wx936aa5357931e226",
    "wx624149b74233c99a",
    "wx5508e31ffe9366b8",
    "wx821fb4d8604ed4d6",
    "wxee0ce83ab4b26f9c",
]

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

API_URL = "https://gw.huiqunchina.com"
AKSK_URL = "https://callback.huiqunchina.com"

QUERY_BY_ID_PATH = "/front-manager/api/customer/queryById/token"
CHANNEL_ACTIVITY_PATH = "/front-manager/api/customer/promotion/channelActivity"
APPOINT_PATH = "/front-manager/api/customer/promotion/appoint"
CHECK_QIANGGOU_PATH = "/front-manager/api/customer/promotion/checkCustomerInQianggou"
WINNING_CUSTOMERS_PATH = "/front-manager/api/customer/promotion/getWinningCustomers"
LOGIN_PATH = "/front-manager/api/customer/wxLogin"  # ⚠️ 推断端点

APPS = [
    {"name": "偲源惠购", "key": "XLHG", "channelId": "8", "appId": "wxded2e7e6d60ac09d"},
    {"name": "贵旅优品", "key": "GLYP", "channelId": "7", "appId": "wx61549642d715f361"},
    {"name": "空港乐购", "key": "KGLG", "channelId": "2", "appId": "wx613ba8ea6a002aa8"},
    {"name": "航旅黔购", "key": "HLQG", "channelId": "6", "appId": "wx936aa5357931e226"},
    {"name": "遵航出山", "key": "ZXCS", "channelId": "5", "appId": "wx624149b74233c99a"},
    {"name": "贵盐黔品", "key": "GYQP", "channelId": "3", "appId": "wx5508e31ffe9366b8"},
    {"name": "乐旅商城", "key": "LLSC", "channelId": "1", "appId": "wx821fb4d8604ed4d6"},
    {"name": "驿路黔寻", "key": "YLQX", "channelId": "9", "appId": "wxee0ce83ab4b26f9c"},
]

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "huluwacookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/107.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x6309080f)XWEB/8461"
)

# ak/sk 缓存（来自 callback 服务 /api/getInfo，按 appid 刷新）
AK_SK = {
    "ak": "00670fb03584fbf44dd6b136e534f495",
    "sk": "0d65f24dbe2bc1ede3c3ceeb96ef71bb",
}

GMT_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
GMT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


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
    print("║ 🍶 葫芦娃预约动态 code 版                   ║")
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


def get_code(server: str, appid: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url} appId={appid}")

    try:
        response = direct_session().get(
            url,
            params={"appId": appid},
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


def common_headers(token: str | None = None, appid: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Referer": f"https://servicewechat.com/{appid or APPID[0]}/1/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["X-access-token"] = token
    return headers


# ====================== HMAC 签名（移植自源脚本） ======================
def hmac_signature(method: str, pathname: str, ak: str, sk: str, date: str) -> str:
    text = method.upper() + "\n" + pathname + "\n\n" + ak + "\n" + date + "\n"
    return base64.b64encode(hmac.new(sk.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).digest()).decode()


def gmt_date_text() -> str:
    now = datetime.now(timezone.utc)
    return (
        f"{GMT_DAYS[now.weekday()]}, {now.day:02d} {GMT_MONTHS[now.month - 1]} {now.year} "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} GMT"
    )


def format_headers(method: str, pathname: str, params_str: str) -> Dict[str, str]:
    date = gmt_date_text()
    return {
        "X-HMAC-SIGNATURE": hmac_signature(method, pathname, AK_SK["ak"], AK_SK["sk"], date),
        "X-HMAC-ACCESS-KEY": AK_SK["ak"],
        "X-HMAC-ALGORITHM": "hmac-sha256",
        "X-HMAC-DIGEST": base64.b64encode(
            hmac.new(AK_SK["sk"].encode("utf-8"), params_str.encode("utf-8"), hashlib.sha256).digest()
        ).decode(),
        "X-HMAC-Date": date,
    }


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


def api_post(server: str, pathname: str, payload: Dict[str, Any], app: Dict[str, Any], token: str = "",
             proxies: Dict[str, str] | None = None, base_url: str = "") -> Dict[str, Any]:
    """HMAC 签名 POST（请求体字符串必须与 X-HMAC-DIGEST 计算时一致）"""
    params_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = common_headers(token, app.get("appId", ""))
    headers.update(format_headers("post", pathname, params_str))
    response = request_with_proxy(
        "POST",
        (base_url or API_URL) + pathname,
        headers=headers,
        data=params_str.encode("utf-8"),
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {"code": "-1", "message": f"JSON解析失败: {response.text[:300]}"}


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, appid: str = "") -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token, appid),
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": "-1",
            "message": f"JSON解析失败: {response.text[:300]}",
        }


def get_ak_sk(server: str, app: Dict[str, Any], proxies: Dict[str, str] | None) -> None:
    """从 callback 服务获取 ak/sk（HMAC 签名密钥）"""
    global AK_SK
    try:
        response = request_with_proxy(
            "POST",
            f"{AKSK_URL}/api/getInfo",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            json={"appId": app["appId"]},
            proxies=proxies,
            server=server,
        )
        data = response.json()
        if str(data.get("code")) == "10000":
            inner = safe_data(data)
            if inner.get("ak") and inner.get("sk"):
                AK_SK = {"ak": str(inner["ak"]), "sk": str(inner["sk"])}
                print(f"🔑 [签名] {app['name']} 获取 ak/sk 成功")
                return
        print(f"⚠️ [签名] {app['name']} 获取 ak/sk 异常: {data.get('message') or json_preview(data, 200)}，使用默认 ak/sk")
    except Exception as exc:
        print(f"⚠️ [签名] {app['name']} 获取 ak/sk 异常: {exc}，使用默认 ak/sk")


def login_by_code(server: str, code: str, app: Dict[str, Any], proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print(f"🔐 [登录] {app['name']} 使用 code 换 token")
        payload = {"code": code, "appId": app["appId"]}
        data = api_post(server, LOGIN_PATH, payload, app, token="", proxies=proxies)

        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


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


def login_with_cache(server: str, app: Dict[str, Any], proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（用户信息接口验证），失效自动 code 刷新"""
    cache_key = f"{server}:{app['appId']}"
    cache_token = get_cached_token(cache_key)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            user_resp = api_post(server, QUERY_BY_ID_PATH, {"appId": app["appId"]}, app, token=cache_token, proxies=proxies)
            if str(user_resp.get("code")) == "10000":
                print("✅ [缓存] token 有效")
                return cache_token, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server, app["appId"])
    if not code:
        return None, None

    token, raw_login = login_by_code(server, code, app, proxies)
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
    set_cached_token(cache_key, token, expire_time)
    return token, raw_login


def get_winning_customers(server: str, app: Dict[str, Any], token: str, activity_id: Any, activity_name: str,
                          proxies: Dict[str, str] | None) -> None:
    resp = api_post(server, WINNING_CUSTOMERS_PATH, {"activityId": activity_id}, app, token=token, proxies=proxies)
    if str(resp.get("code")) != "10000":
        print(f"❌ [中签] 查询中签结果失败：{resp.get('message')}")
        return
    icon = "⚠️" if resp.get("data") else "✅"
    print(f"{icon} [中签] [{activity_name}]中签结果：{resp.get('message')}")


def format_activity_time(end_time: Any) -> str:
    try:
        value = float(end_time)
    except (TypeError, ValueError):
        return str(end_time)
    if value > 100000000000:
        value = value / 1000
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M:00")


def reservation(server: str, app: Dict[str, Any], token: str, proxies: Dict[str, str] | None) -> str:
    try:
        user_resp = api_post(server, QUERY_BY_ID_PATH, {"appId": app["appId"]}, app, token=token, proxies=proxies)
        if str(user_resp.get("code")) != "10000":
            print(f"❌ [用户] {app['name']} 查询用户失败: {user_resp.get('message')}")
            return "用户查询失败"

        activity_resp = api_post(server, CHANNEL_ACTIVITY_PATH, {"id": app["channelId"]}, app, token=token, proxies=proxies)
        if str(activity_resp.get("code")) != "10000":
            print(f"❌ [活动] {app['name']} 查询活动失败: {activity_resp.get('message')}")
            return "活动查询失败"

        a_data = safe_data(activity_resp)
        end_time = a_data.get("endTime") or 0
        now_ms = time.time() * 1000
        if end_time and now_ms - to_float(end_time) > 1000 * 60 * 1000:
            print(f"----暂无新活动。最近活动为「{format_activity_time(end_time)}」【{a_data.get('name')}】----")
            return "活动已结束"

        user_info = safe_data(user_resp)
        print(f"👤 [用户] 当前用户[{user_info.get('phone')}]")

        appoint_counts = to_float(a_data.get("appointCounts"))
        draw_time = to_float(a_data.get("drawTime"))
        if appoint_counts > 1 and draw_time and draw_time < now_ms:
            print(
                f"📢 [开奖] [{a_data.get('name')}]结果已公布，中签人数[{int(appoint_counts)}]，"
                f"{'您可能已中签，尽快进小程序确认！' if a_data.get('isAppoint') else '您未中签'}"
            )
            get_winning_customers(server, app, token, a_data.get("id"), a_data.get("name"), proxies)
            return "结果已公布"

        print(f"📣 [活动] 活动名称[{a_data.get('name')}]")

        check_resp = api_post(
            server,
            CHECK_QIANGGOU_PATH,
            {"activityId": a_data.get("id"), "channelId": app["channelId"]},
            app,
            token=token,
            proxies=proxies,
        )
        if str(check_resp.get("code")) != "10000":
            print(f"❌ [预约] {app['name']} 检查抢购状态失败: {check_resp.get('message')}")
            return "状态检查失败"

        if not check_resp.get("data"):
            r = api_post(
                server,
                APPOINT_PATH,
                {"activityId": a_data.get("id"), "channelId": app["channelId"]},
                app,
                token=token,
                proxies=proxies,
            )
            message = str(r.get("message"))
            if "验证码" in message:
                print(f"⚠️ [预约] 预约结果[appoint][{message}]")
            else:
                print(f"✅ [预约] 预约结果[appoint][{message}]")
            return f"预约: {message}"

        print("✅ [预约] 预约结果[已经预约成功，无需重复预约]")
        return "已经预约成功，无需重复预约"
    except Exception as exc:
        print(f"❌ [预约] {app['name']} 运行异常[{exc}]")
        return f"运行异常[{exc}]"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "appsMsg": "-",
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

    lines: List[str] = []
    ok_apps = 0

    try:
        for app in APPS:
            print(f"\n▶️ [应用] {app['name']}预约开始")
            get_ak_sk(server, app, proxies)

            token, raw_login = login_with_cache(server, app, proxies)
            if not token:
                msg = f"登录失败: {json_preview(raw_login, 200)}" if raw_login else "登录失败(code 获取失败)"
                print(f"❌ [应用] {app['name']} {msg}")
                lines.append(f"{app['name']}: {msg}")
                continue

            if result["token"] == "-":
                result["token"] = mask(token)

            msg = reservation(server, app, token, proxies)
            if "失败" not in msg and "异常" not in msg:
                ok_apps += 1
            print(f"🏁 [应用] {app['name']}预约结束 → {msg}")
            lines.append(f"{app['name']}: {msg}")
            sleep(random.randint(1, 2))

        result["appsMsg"] = f"{ok_apps}/{len(APPS)} 应用正常"
        result["success"] = ok_apps > 0
        result["_lines"] = lines
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        result["_lines"] = lines
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍶 葫芦娃预约任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🍶 应用：{res["appsMsg"]}
"""
        for line in res.get("_lines", []):
            content += f"  · {line}\n"
        content += f"""{icon} 结果：{"成功" if res["success"] else "失败"}
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
                "appsMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 葫芦娃预约任务执行完成                ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🍶 葫芦娃预约任务完成", build_notify(results))


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
