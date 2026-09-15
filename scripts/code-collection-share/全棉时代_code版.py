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
全棉时代 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. nmp /api/wx/main/login 使用 code 换 token（请求头 code=设备GUID, tag=v3.0）
  3. 每日签到（/api/member/signIn/point，失败回退新版 signId 签到接口）
  4. 种棉花：sg01 登录 → 刷新日常 → 自动种树（选定成长目标奖品）→ 循环浇水
  5. 日常任务：逛甄选好棉品/浏览新用户专区/社区送福利/订阅提醒/棉花工厂/
     三餐福袋/庄园小课堂（含 sg01 参数签名 md5+盐）、收集/使用阳光
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       本地 code 服务地址，默认 127.0.0.1:8088
  QMSD_SIGN_ID      新版签到接口 signId 回退默认值，默认 QD26060001
  qmzmh_prize_id    种树成长目标奖品 id，默认 1046（加厚棉柔巾 6片/包*1包）

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import hashlib
import json
import os
import random
import re
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "全棉时代"
APPID = "wxdfcaa44b1aa891a7"

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

NMP_BASE_URL = "https://nmp.pureh2b.com"
SG01_BASE_URL = "https://sg01.purcotton.com"

LOGIN_URL = f"{NMP_BASE_URL}/api/wx/main/login"
NMP_SIGN_POINT_URL = f"{NMP_BASE_URL}/api/member/signIn/point"
NMP_COMPLETE_TASK_URL = f"{NMP_BASE_URL}/api/purcotton/completetask"
NMP_CATEGORY_URL = f"{NMP_BASE_URL}/api/new/navigation/category/query"
NMP_SIGN_INDEX_URL = f"{NMP_BASE_URL}/api/new/member/sign/index"
NMP_SIGN_IN_URL = f"{NMP_BASE_URL}/api/new/member/sign/signIn"

SG01_LOGIN_URL = f"{SG01_BASE_URL}/api/login"
SG01_INDEX_URL = f"{SG01_BASE_URL}/api/index"
SG01_PRIZE_HOME_URL = f"{SG01_BASE_URL}/api/prize/home"
SG01_GAIN_TREE_URL = f"{SG01_BASE_URL}/api/gain-tree"
SG01_WATERING_URL = f"{SG01_BASE_URL}/api/watering"
SG01_TASK_LIST_URL = f"{SG01_BASE_URL}/api/task/list"
SG01_TASK_COMPLETE_URL = f"{SG01_BASE_URL}/api/task/complete-task"
SG01_TASK_MANUAL_URL = f"{SG01_BASE_URL}/api/task/complete-manual-task"
SG01_TASK_RECEIVE_WATER_URL = f"{SG01_BASE_URL}/api/task/receive-task-water"
SG01_ANSWER_URL = f"{SG01_BASE_URL}/api/answer"
SG01_ANSWER_COMPLETE_URL = f"{SG01_BASE_URL}/api/answer/complete"
SG01_ANSWER_OPEN_BOX_URL = f"{SG01_BASE_URL}/api/answer/open-box"
SG01_TODAY_WATER_URL = f"{SG01_BASE_URL}/api/get-today-water"
SG01_STATISTICS_URL = f"{SG01_BASE_URL}/api/statistics/store"
SG01_GET_SUNSHINE_URL = f"{SG01_BASE_URL}/api/get-sunshine"
SG01_SUNSHINE_TASK_URL = f"{SG01_BASE_URL}/api/sunshine-task/complete-task"

PRIZE_ID_DEFAULT = "1046"  # 种树默认成长目标: 加厚棉柔巾 6片/包*1包
DEFAULT_SIGN_ID = os.getenv("QMSD_SIGN_ID", "QD26060001")

# sg01 的 H5 只对部分接口请求体做了参数签名(formatMd5)，不带签名时服务端返回
# {"code":400,"msg":"参数格式错误"}。算法: 追加 timestamp(毫秒) → 丢掉值为 None/""
# 的项 → 按 key 排序拼成 query 串 → md5(串+固定盐).upper()
SG01_SIGN_SALT = "z0hQTvC21f8SXlLbL9Hv"

# 任务ID → 每日最多完成次数（照源脚本 pdrw 配置）
TASK_LIMITS: Dict[int, int] = {6: 4, 13: 2, 15: 1, 4: 3, 16: 1, 10: 1, 14: 1, 1: 1}
TASK_NAMES: Dict[int, str] = {
    1: "签到",
    4: "三餐福袋",
    6: "逛甄选好棉品",
    10: "订阅奖励提醒",
    13: "浏览新用户专区",
    14: "庄园小课堂",
    15: "棉花工厂",
    16: "社区送福利",
}
ACTION_NAMES: Dict[str, str] = {
    "browse_venue": "逛甄选好棉品",
    "browse_new_user_zone": "浏览新用户专区",
    "browse_community": "社区送福利",
    "subscibe": "订阅奖励提醒",
}

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qmsdcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; ONEPLUS A6000 Build/RKQ1.201217.002; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile "
    "Safari/537.36 XWEB/1160065 MMWEBSDK/20231201 MMWEBID/2930 "
    "MicroMessenger/8.0.45.2521(0x28002D3D) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 miniProgram/wxdfcaa44b1aa891a7"
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
    data = resp.get("data")
    return data if isinstance(data, dict) else {}


def china_today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def device_guid(server: str) -> str:
    """设备 GUID：nmp/sg01 请求头 code 字段，按账号稳定生成（非个人凭证）。"""
    return str(uuid.uuid3(uuid.NAMESPACE_DNS, f"{APPID}:{server}"))


def sg01_sign(params: Dict[str, Any]) -> Dict[str, Any]:
    """把参数体补上 timestamp + sign，返回可直接 json= 提交的新 dict。"""
    payload = dict(params)
    payload["timestamp"] = int(time.time() * 1000)
    kept = {k: v for k, v in payload.items() if v is not None and v != ""}
    query = "&".join(f"{str(k)}={str(v)}" for k, v in sorted(kept.items()))
    payload["sign"] = hashlib.md5((query + SG01_SIGN_SALT).encode("utf-8")).hexdigest().upper()
    return payload


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🌱 全棉时代签到+种棉花动态 code 版            ║")
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


def common_headers(token: str | None = None, server: str = "") -> Dict[str, str]:
    """复刻 request.js：每个请求头带 code=设备GUID、tag=v3.0，登录后再带 token。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/1376/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "app-id": APPID,
        "tag": "v3.0",
        "code": device_guid(server) if server else str(uuid.uuid4()),
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
        candidates.extend([
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
            "GET",
            LOGIN_URL,
            headers=common_headers(None, server),
            params={"code": code},
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
        headers=common_headers(token, server),
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
        headers=common_headers(token, server),
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


def api_post_form(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = common_headers(token, server)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    response = request_with_proxy(
        "POST",
        url,
        headers=headers,
        data=payload,
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
    """优先使用缓存 token（新版签到详情接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            resp = api_get(server, f"{NMP_SIGN_INDEX_URL}?signId={quote(DEFAULT_SIGN_ID)}", cache_token, proxies)
            if isinstance(resp, dict) and resp.get("signMember") is not None:
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


def find_sign_id(obj: Any) -> str:
    """从首页组件 redirectInfo.info 等结构里递归解析 signId（形如 QD\\d+）。"""
    if isinstance(obj, str):
        match = re.search(r"(QD\d+)", obj) or re.search(r"[?&]id=([^&]+)", obj)
        return match.group(1) if match else ""
    if isinstance(obj, list):
        for item in obj:
            found = find_sign_id(item)
            if found:
                return found
        return ""
    if isinstance(obj, dict):
        redirect = obj.get("redirectInfo")
        if isinstance(redirect, dict) and redirect.get("info"):
            found = find_sign_id(redirect.get("info"))
            if found:
                return found
        for value in obj.values():
            found = find_sign_id(value)
            if found:
                return found
    return ""


def member_sign_in(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    """每日签到: GET /api/member/signIn/point。响应体为裸数字 1=成功 0=今日已签到。"""
    response = request_with_proxy(
        "GET",
        NMP_SIGN_POINT_URL,
        headers=common_headers(token, server),
        proxies=proxies,
        server=server,
    )
    text = (response.text or "").strip()
    try:
        val = json.loads(text)
    except Exception:
        val = text
    if val in (1, "1"):
        return True, "签到成功"
    if val in (0, "0"):
        return True, "今日已签到"
    return False, f"签到失败(返回={text[:80]})"


def aux_fallback_sign(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    """回退：新版 signId 签到接口（category 查 signId → 详情 → signIn）。"""
    sign_id = DEFAULT_SIGN_ID
    cat = api_post(server, NMP_CATEGORY_URL, token, proxies,
                   {"pageNum": 1, "pageSize": 10, "venueType": "MAIN", "categoryId": "010002"})
    if isinstance(cat, dict) and cat.get("code") == 200:
        found = find_sign_id(safe_data(cat).get("componentList"))
        if found:
            sign_id = found
            print(f"🔍 [签到] 自动获取签到ID: {sign_id}")

    index = api_get(server, f"{NMP_SIGN_INDEX_URL}?signId={quote(sign_id)}", token, proxies)
    if isinstance(index, dict) and index.get("signMember"):
        dates = index["signMember"].get("signDateList") or []
        days = index["signMember"].get("signDays") or 0
        print(f"🔍 [签到] 签到详情: 累计{days}天")
        if china_today() in dates:
            return True, "今日已签到"

    sign = api_post(server, NMP_SIGN_IN_URL, token, proxies, {"signType": 1, "signInId": sign_id})
    if isinstance(sign, list):
        if sign and isinstance(sign[0], dict) and sign[0].get("rewardPoint") is not None:
            return True, f"签到成功，积分+{sign[0].get('rewardPoint')}"
        return True, "今日已签到"
    msg = ""
    if isinstance(sign, dict):
        msg = str(sign.get("message") or sign.get("msg") or "")
    if re.search(r"已签|签到过|重复|已完成", msg):
        return True, f"今日已签到（{msg}）"
    return False, f"签到失败: {msg or json_preview(sign, 200)}"


def sg01_login(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[str | None, int | None]:
    """sg01 侧登录，返回 (phone, user_id)。"""
    resp = api_post(server, SG01_LOGIN_URL, token, proxies, {"invite_source": "task", "channel": ""})
    if resp.get("code") == 200:
        data = safe_data(resp)
        return data.get("phone"), data.get("id")
    print(f"⚠️ [sg01] 登录未通过: {resp.get('msg') or json_preview(resp, 300)}")
    return None, None


def hqid(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[int | None, Any, Any]:
    """获取树木ID和阳光信息（tree 字段兼容 dict/list 两种形态）。"""
    resp = api_get(server, SG01_INDEX_URL, token, proxies)
    if resp.get("code") != 200:
        print(f"⚠️ [种棉花] 获取庄园信息失败: {resp.get('msg') or json_preview(resp, 300)}")
        return None, None, None

    payload = resp.get("data") or {}
    tree_data = payload.get("tree")
    user_data = payload.get("user")
    if not isinstance(user_data, dict):
        user_data = {}

    if isinstance(tree_data, list):
        tree_data = tree_data[0] if tree_data else {}
    if not isinstance(tree_data, dict):
        tree_data = {}
    tree_id = tree_data.get("id")

    sunshine = user_data.get("sunshine", 0)
    total_sunshine = user_data.get("total_sunshine", 0)
    return tree_id, sunshine, total_sunshine


def prize_home(server: str, token: str, proxies: Dict[str, str] | None) -> List[Dict[str, Any]]:
    """GET /api/prize/home: 可选的成长目标(奖品)列表。"""
    resp = api_get(server, SG01_PRIZE_HOME_URL, token, proxies)
    if resp.get("code") == 200:
        lst = safe_data(resp).get("list")
        return lst if isinstance(lst, list) else []
    print(f"⚠️ [种棉花] 获取目标奖品列表失败: {resp.get('msg') or json_preview(resp, 300)}")
    return []


def zhongshu(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    """种树: 选定成长目标后 POST /api/gain-tree {prize_id}（一次性动作，不消耗水滴）。"""
    prizes = prize_home(server, token, proxies)
    if not prizes:
        return False, "无可选成长目标(prize/home 为空), 未种树"

    want = str(os.getenv("qmzmh_prize_id", PRIZE_ID_DEFAULT)).strip()
    chosen = next((p for p in prizes if str(p.get("id")) == want), None)
    if chosen is None:
        chosen = prizes[0]
        print(f"⚠️ [种棉花] 目标 prize_id={want} 已不在列表中, 回退为首项")
    title = chosen.get("title") or chosen.get("name") or ""

    resp = api_post(server, SG01_GAIN_TREE_URL, token, proxies, {"prize_id": chosen.get("id")})
    if resp.get("code") == 200:
        return True, f"已种下(目标: {title})"
    msg = resp.get("msg") or f"code={resp.get('code')}"
    # 已有树时服务端会拒绝, 视为幂等成功
    if any(k in str(msg) for k in ("已", "存在", "重复")):
        return True, f"已有树({msg})"
    return False, f"种树失败: {msg}"


def jscz(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    """浇水（种棉花），只浇自己的树；尚未种树时自动种下后继续。"""
    tree_id, sunshine, total_sunshine = hqid(server, token, proxies)

    if tree_id is None:
        print("🌱 [种棉花] 尚未种树, 正在自动种下...")
        planted, plant_msg = zhongshu(server, token, proxies)
        print(f"🌱 [种棉花] 种树: {plant_msg}")
        if not planted:
            return False, f"种树未成功: {plant_msg}"
        tree_id, sunshine, total_sunshine = hqid(server, token, proxies)
        if tree_id is None:
            print("⚠️ [种棉花] 种树后仍未取到树木ID, 跳过浇水")
            return False, "种树后仍未取到树木ID"

    print(f"🌱 [种棉花] 树木ID: {tree_id} 当前阳光: {sunshine} 总阳光: {total_sunshine}")

    water_round = 0
    while True:
        resp = api_post(server, SG01_WATERING_URL, token, proxies, {"tree_user_id": tree_id, "water_cnt": 1})
        if resp.get("code") == 200:
            remaining_water = safe_data(resp).get("info", {}).get("sy_water", "未知")
            water_round += 1
            print(f"💧 [浇水] 第 {water_round} 次完成, 剩余水滴数: {remaining_water}")

            if to_float(remaining_water) < 30:
                print("⚠️ [浇水] 水滴不足，停止浇水。")
                break

            wait_time = random.randint(1, 3)
            print(f"⏳ [浇水] 暂停 {wait_time}s 后继续...")
            sleep(wait_time)
        elif resp.get("code") == 400:
            print(f"⚠️ [浇水] {resp.get('msg') or '未知错误'}")
            break
        else:
            print(f"⚠️ [浇水] 未知的响应code: {resp.get('code')} {json_preview(resp, 300)}")
            break

    return True, f"浇水完成（共 {water_round} 次）"


def tjlq_mpjl(server: str, token: str, tid: str, proxies: Dict[str, str] | None) -> None:
    """领取任务奖励水滴: POST /api/task/receive-task-water {tid}。"""
    resp = api_post(server, SG01_TASK_RECEIVE_WATER_URL, token, proxies, {"tid": tid})
    if resp.get("code") == 200:
        data = safe_data(resp)
        print(f"✅ [任务] 奖励领取成功。剩余水量: {data.get('sy_water', '未知')}, 获取水量: {data.get('get_water', '未知')}")
    else:
        print(f"⚠️ [任务] 奖励领取失败: {resp.get('msg') or json_preview(resp, 300)}")


def llhmp(server: str, token: str, phone: str | None, action: str, tid: str, proxies: Dict[str, str] | None) -> None:
    """nmp 浏览类任务: POST /api/purcotton/completetask（表单），成功后领奖励。"""
    action_description = ACTION_NAMES.get(action, "执行任务")
    resp = api_post_form(server, NMP_COMPLETE_TASK_URL, token, proxies,
                         {"action": action, "phone": phone or "", "from": "guoyuan"})

    if resp.get("code") == 200:
        print(f"✅ [任务] {action_description} 任务成功，暂停一段时间再继续...")
        wait_time = random.randint(15, 20)
        sleep(wait_time)
        tjlq_mpjl(server, token, tid, proxies)
    elif resp.get("code") == 400:
        print(f"⚠️ [任务] {action_description}: {resp.get('msg')}")
    else:
        print(f"⚠️ [任务] {action_description} 收到未预期的响应: {json_preview(resp, 300)}")


def complete_task(server: str, token: str, tid: str, proxies: Dict[str, str] | None) -> None:
    """棉花工厂: POST /api/task/complete-manual-task（带签名）。"""
    payload = sg01_sign({"tid": tid, "relate_id": 0})
    resp = api_post(server, SG01_TASK_MANUAL_URL, token, proxies, payload)
    if resp.get("code") == 200:
        print(f"✅ [任务] {TASK_NAMES.get(int(tid), tid)} 完成成功。")
        tjlq_mpjl(server, token, tid, proxies)
    else:
        print(f"⚠️ [任务] {TASK_NAMES.get(int(tid), tid)} 任务失败: {resp.get('msg')}")


def lq_fd(server: str, token: str, tid: int, proxies: Dict[str, str] | None) -> None:
    """三餐福袋和签到: POST /api/task/complete-task（带签名）。"""
    task_name = {4: "三餐福袋", 1: "签到"}.get(tid, "未知任务")
    payload = sg01_sign({"tid": tid})
    resp = api_post(server, SG01_TASK_COMPLETE_URL, token, proxies, payload)
    if resp.get("code") == 200:
        data = safe_data(resp)
        print(f"✅ [任务] {task_name} 奖励领取成功。剩余水量: {data.get('sy_water', '未知')}, 获取水量: {data.get('get_water', '未知')}")
    else:
        print(f"⚠️ [任务] {task_name}: {resp.get('msg')}")


def hdwt_box(server: str, token: str, tid: str, proxies: Dict[str, str] | None) -> None:
    """庄园小课堂: GET /api/answer 取题，POST /api/answer/complete 提交（带签名），开宝箱。"""
    resp = api_get(server, SG01_ANSWER_URL, token, proxies)
    exams = safe_data(resp).get("exams") or []
    if not exams:
        print(f"⚠️ [答题] 未获取到题目: {resp.get('msg') or json_preview(resp, 300)}")
        return

    for exam in exams:
        exam_id = exam.get("id")
        # H5 提交的是所选选项字母(answer=A/B/C/D)，服务端在响应里回正确答案
        options = [letter for letter in ("A", "B", "C", "D") if exam.get(letter.lower())]
        choice = options[0] if options else "A"
        payload = sg01_sign({"answer": choice, "exam_id": exam_id, "tid": int(tid)})
        submit_resp = api_post(server, SG01_ANSWER_COMPLETE_URL, token, proxies, payload)

        if submit_resp.get("code") != 200:
            print(f"⚠️ [答题] 提交答案失败: {submit_resp.get('msg')}")
            continue

        data_ans = submit_resp.get("data") or {}
        get_water = data_ans.get("get_water", 0)
        complete_num = data_ans.get("complete_num", 0)
        box_id = data_ans.get("box_id", 0)
        print(f"✅ [答题] 答{choice} 正确答案{data_ans.get('answer', '?')} 获取水量: {get_water}, 完成数量: {complete_num}, 宝箱ID: {box_id}")

        if to_float(box_id) > 0:
            print(f"📦 [答题] 检测到宝箱ID: {box_id}，尝试打开宝箱...")
            box_resp = api_post(server, SG01_ANSWER_OPEN_BOX_URL, token, proxies, {"box_id": box_id})
            if box_resp.get("code") == 200:
                box_data = safe_data(box_resp)
                print(f"📦 [答题] 宝箱打开成功。剩余水量: {box_data.get('sy_water', 0)}, 宝箱水量: {box_data.get('get_water', 0)}")
            else:
                print(f"⚠️ [答题] 宝箱打开失败: {box_resp.get('msg') or json_preview(box_resp, 300)}")

        wait_time = random.randint(3, 5)
        print(f"⏳ [答题] 暂停 {wait_time}s")
        sleep(wait_time)


def today_water(server: str, token: str, proxies: Dict[str, str] | None) -> None:
    """查询今日/明日可获取水量。"""
    resp = api_post(server, SG01_TODAY_WATER_URL, token, proxies, {})
    if resp.get("code") == 200:
        data = safe_data(resp)
        print(f"💧 [水量] 今日获取水量: {data.get('get_water', '未知')} 明日可获取水量: {data.get('tomorrow_get_water_num', '未知')}")
    else:
        print(f"⚠️ [水量] {resp.get('msg') or '未知错误'}")


def sj_yg(server: str, token: str, proxies: Dict[str, str] | None) -> None:
    """收集阳光: POST /api/get-sunshine，循环领取直到没有可领（code 400）。"""
    while True:
        resp = api_post(server, SG01_GET_SUNSHINE_URL, token, proxies, {"time": int(time.time() * 1000)})
        if resp.get("code") == 200:
            data = safe_data(resp)
            print(f"☀️ [阳光] 成功领取阳光: 剩余阳光: {data.get('sy_sunshine')}, 获得阳光: {data.get('get_sunshine')}")
            wait_time = random.randint(1, 3)
            print(f"⏳ [阳光] 暂停 {wait_time}s 后重新领取...")
            sleep(wait_time)
        elif resp.get("code") == 400:
            print("⚠️ [阳光] 没有可领取的阳光")
            break
        else:
            print(f"⚠️ [阳光] 阳光操作响应: {json_preview(resp, 300)}")
            break


def syyg(server: str, token: str, proxies: Dict[str, str] | None) -> None:
    """当阳光值大于 100 时，完成阳光任务。"""
    _, sunshine, _ = hqid(server, token, proxies)
    if to_float(sunshine) > 99:
        resp = api_post(server, SG01_SUNSHINE_TASK_URL, token, proxies, {"tid": 1})
        if resp.get("code") == 200:
            print("✅ [阳光] 成功完成阳光任务。")
        else:
            print(f"⚠️ [阳光] 完成阳光任务失败: {resp.get('msg') or json_preview(resp, 300)}")
    else:
        print(f"⚠️ [阳光] 阳光值未达到 {sunshine}/100，不执行任务。")


def cscscs(server: str, token: str, user_id: int | None, proxies: Dict[str, str] | None) -> None:
    """刷新/领取日常: POST /api/statistics/store {uid, type:301}。"""
    api_post(server, SG01_STATISTICS_URL, token, proxies, {"uid": user_id, "type": 301})


def task_list(server: str, token: str, proxies: Dict[str, str] | None) -> List[Dict[str, Any]]:
    """任务列表: GET /api/task/list，返回今天已完成的任务信息。"""
    resp = api_get(server, SG01_TASK_LIST_URL, token, proxies)
    if resp.get("code") != 200:
        print(f"⚠️ [任务] 获取任务列表失败: {resp.get('msg') or json_preview(resp, 300)}")
        return []

    today_date = datetime.now().strftime("%Y-%m-%d")
    task_user_info = safe_data(resp).get("task_user_info") or []
    today_tasks: List[Dict[str, Any]] = []
    print("------任务进度条-----------")
    for task in task_user_info:
        if not isinstance(task, dict):
            continue
        if task.get("complete_date") == today_date:
            task_id = task.get("task_id")
            task_name = TASK_NAMES.get(task_id, f"未知任务 {task_id}")
            print(f"任务ID: {task_id} {task_name}/{task.get('complete_num')}, 任务时间: {task.get('complete_date')}")
            today_tasks.append(task)
    print("---------------------------")
    return today_tasks


def pdrw(server: str, token: str, phone: str | None, proxies: Dict[str, str] | None) -> str:
    """根据任务完成情况执行任务（照源脚本 pdrw 分支逻辑）。"""
    today_tasks = task_list(server, token, proxies)
    existing_task_ids = [task.get("task_id") for task in today_tasks]
    done: List[str] = []

    for task_id, max_completes in TASK_LIMITS.items():
        task_info = next((task for task in today_tasks if task.get("task_id") == task_id), None)

        if task_info:
            if not (task_info.get("complete_num", 0) < max_completes):
                continue
        elif task_id in existing_task_ids:
            continue

        if task_id == 6:
            llhmp(server, token, phone, "browse_venue", "6", proxies)
            if task_info is None:
                today_water(server, token, proxies)
        elif task_id == 13:
            llhmp(server, token, phone, "browse_new_user_zone", "13", proxies)
            if task_info is not None:
                today_water(server, token, proxies)
            sj_yg(server, token, proxies)
            syyg(server, token, proxies)
        elif task_id == 15:
            complete_task(server, token, "15", proxies)
        elif task_id == 16:
            llhmp(server, token, phone, "browse_community", "16", proxies)
        elif task_id == 10:
            llhmp(server, token, phone, "subscibe", "10", proxies)
        elif task_id == 14:
            hdwt_box(server, token, "14", proxies)
        elif task_id == 4:
            lq_fd(server, token, 4, proxies)
        elif task_id == 1:
            lq_fd(server, token, 1, proxies)

        done.append(TASK_NAMES.get(task_id, str(task_id)))
        wait_time = random.randint(1, 5)
        print(f"⏳ [任务] 暂停 {wait_time}s")
        sleep(wait_time)

    return "、".join(done) if done else "今日任务均已完成"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "waterMsg": "-",
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
        # 未注册会员校验（登录响应 member.phone）
        member = None
        if raw_login and isinstance(raw_login, dict):
            inner = raw_login.get("data") if isinstance(raw_login.get("data"), dict) else raw_login
            candidate = inner.get("member") if isinstance(inner, dict) else None
            if isinstance(candidate, dict):
                member = candidate
            elif isinstance(raw_login.get("member"), dict):
                member = raw_login["member"]
        if member is not None and not member.get("phone"):
            msg = ("该账号尚未绑定手机号(需先在小程序「全棉时代」内完成手机号授权"
                   "注册为会员后, 才能签到/种棉花)")
            print(f"⚠️ [会员] {msg}")
            result["error"] = msg
            return result

        # 1. 每日签到（signIn/point，失败回退新版 signId 签到接口）
        ok, sign_msg = member_sign_in(server, token, proxies)
        if not ok:
            print(f"⚠️ [签到] {sign_msg}，尝试新版签到接口...")
            ok, sign_msg = aux_fallback_sign(server, token, proxies)
        result["signMsg"] = sign_msg
        print(("✅ [签到] " if ok else "❌ [签到] ") + sign_msg)

        # 2. 种棉花（sg01 需再登录一次拿到 phone/user_id；失败仅提示，不影响签到结果）
        try:
            sg_phone, sg_uid = sg01_login(server, token, proxies)
            if sg_phone and sg_uid:
                cscscs(server, token, sg_uid, proxies)      # 刷新/领取日常
                watered, water_msg = jscz(server, token, proxies)   # 浇水(种棉花)
                result["waterMsg"] = water_msg
                task_msg = pdrw(server, token, sg_phone, proxies)   # 日常任务判断
                result["taskMsg"] = task_msg
            else:
                print("⚠️ [种棉花] sg01 登录未通过, 跳过浇水(不影响签到)")
                result["waterMsg"] = "sg01 未登录, 已跳过"
                result["taskMsg"] = "sg01 未登录, 已跳过"
        except Exception as exc:
            print(f"⚠️ [种棉花] 流程异常(忽略, 不影响签到): {exc}")
            result["waterMsg"] = f"异常已忽略: {exc}"

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🌱 全棉时代任务结果

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
💧 种棉花：{res["waterMsg"]}
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
                "waterMsg": "-",
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
    print("║ 🏁 全棉时代任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🌱 全棉时代任务完成", build_notify(results))


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
