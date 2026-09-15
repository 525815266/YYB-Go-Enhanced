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
有赞店铺通用签到动态 code 版（临水玉泉 / TOI 等有赞小程序共用）

功能：
  1. 四端口本地服务获取微信 code
  2. uic.youzan.com/passport/general/auth.json 使用 code 换 token（有赞 UIC 通用登录）
  3. 会话校验（wscaccount/api/authorize/data.json）
  4. 每日签到（wscump/checkin/checkinV2）
  5. Token 缓存与自动刷新
  6. PushPlus / 企业微信推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  youzan            门店配置，可选，每行一个（或 & 分隔）：
                    openid#appid#kdtId#checkinId[#备注]
                    - openid 保留兼容旧配置，code 版无需填写可留空
                    - kdtId 有赞店铺号（可留空，登录后用服务端回显的 kdtId 兜底）
                    - checkinId 签到活动 id（小程序「签到」页抓 checkinV2.json?checkinId= 得到）
                    未配置时使用内置默认门店（临水玉泉 / TOI）
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

import json
import os
import random
import re
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, urlencode

import requests


APP_NAME = "有赞店铺签到"
APPID = [
    "wx21293beab739d5c3",
    "wxbb5a91aacbab57f2",
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

UIC_BASE = "https://uic.youzan.com"
H5_BASE = "https://h5.youzan.com"
UIC_LOGIN_URL = f"{UIC_BASE}/passport/general/auth.json"
SESSION_PATH = "/wscaccount/api/authorize/data.json"
CHECKIN_PATH = "/wscump/checkin/checkinV2.json"
USER_VERSION = "2.216.4.101"

# 内置默认门店（与源脚本示例一致，可用环境变量 youzan 覆盖）
DEFAULT_STORES = [
    {"name": "临水玉泉", "appId": "wx21293beab739d5c3", "kdtId": "44353481", "checkinId": "15129"},
    {"name": "TOI", "appId": "wxbb5a91aacbab57f2", "kdtId": "97827637", "checkinId": "2163238"},
]

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yzcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF"
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


def load_stores() -> List[Dict[str, str]]:
    """解析环境变量 youzan（openid#appid#kdtId#checkinId[#备注]），未配置时用内置默认门店"""
    raw = os.getenv("youzan", "").strip()
    stores: List[Dict[str, str]] = []

    if raw:
        for line in re.split(r"[\n&]", raw):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("#")]
            if len(parts) >= 4:
                stores.append({
                    "openid": parts[0],
                    "appId": parts[1],
                    "kdtId": parts[2],
                    "checkinId": parts[3],
                    "name": parts[4] if len(parts) > 4 else "",
                })
            elif len(parts) == 3:
                stores.append({
                    "openid": "",
                    "appId": parts[0],
                    "kdtId": parts[1],
                    "checkinId": parts[2],
                    "name": "",
                })

    if not stores:
        stores = [dict(item) for item in DEFAULT_STORES]

    for item in stores:
        if not item.get("name"):
            item["name"] = item.get("appId", "")

    return stores


def store_label(store: Dict[str, str]) -> str:
    return store.get("name") or store.get("appId") or "未知门店"


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🛍️ 有赞店铺通用签到动态 code 版               ║")
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


def get_code(server: str, app_id: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url} (appId={app_id})")

    try:
        response = direct_session().get(
            url,
            params={"appId": app_id},
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


def common_headers(cred: Dict[str, Any] | None = None) -> Dict[str, str]:
    app_id = (cred or {}).get("appId") or APPID[0]
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://servicewechat.com/{app_id}/0/page-frame.html",
        "Extra-Data": json.dumps((cred or {}).get("extraData") or {}, ensure_ascii=False),
    }
    return headers


def build_extra_data(session_id: str, union_id: str) -> Dict[str, Any]:
    return {
        "is_weapp": 1,
        "sid": session_id,
        "version": USER_VERSION,
        "client": "weapp",
        "bizEnv": "wsc",
        "uuid": union_id,
        "ftime": int(time.time() * 1000),
    }


def extract_token(data: Any) -> Dict[str, Any] | None:
    """从有赞 UIC 登录响应中提取 accessToken/sessionId/kdtId"""
    if not isinstance(data, dict):
        return None

    inner = data.get("data")
    if not isinstance(inner, dict):
        inner = {}

    access_token = inner.get("accessToken") or inner.get("access_token")
    session_id = inner.get("sessionId") or inner.get("session_id")

    if access_token and str(access_token) != "null":
        return {
            "accessToken": str(access_token),
            "sessionId": str(session_id or ""),
            "kdtId": str(inner.get("kdtId") or ""),
            "unionId": str(inner.get("unionId") or ""),
        }

    return None


def login_by_code(server: str, store: Dict[str, str], code: str, proxies: Dict[str, str] | None) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    label = store_label(store)
    app_id = store["appId"]
    kdt = store.get("kdtId") or "0"

    try:
        print(f"🔐 [登录] [{label}] 使用 code 换 token")
        body = {
            "appId": app_id,
            "code": code,
            "platformName": "weapp",
            "signature": "windows",
            "clientBiz": "weapp_wsc",
            "inWsc": True,
            "kdtId": kdt,
            "extraBizData": {
                "enterOptions": {
                    "extKdtId": int(kdt) if str(kdt).isdigit() else 0,
                    "path": "pages/home/dashboard/index",
                    "query": {},
                    "scene": 1007,
                    "referrerInfo": {},
                    "apiCategory": "default",
                },
                "guideBizDataMap": {"from_params": ""},
                "sceneData": {},
            },
        }
        response = request_with_proxy(
            "POST",
            f"{UIC_LOGIN_URL}?kdt_id={quote(kdt)}&app_id={quote(app_id)}",
            headers=common_headers(),
            json=body,
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        if data.get("code") != 0:
            print(f"❌ [登录] [{label}] 接口返回失败: {json_preview(data)}")
            return None, data

        cred = extract_token(data)
        if cred:
            if not cred.get("kdtId"):
                cred["kdtId"] = kdt
            cred["appId"] = app_id
            cred["extraData"] = build_extra_data(cred["sessionId"], cred["unionId"])
            print(f"✅ [登录] [{label}] token 获取成功（店铺 kdtId={cred['kdtId']}）: {mask(cred['accessToken'])}")
            return cred, data

        print(f"❌ [登录] [{label}] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] [{label}] 请求异常: {exc}")
        return None, None


def build_h5_url(cred: Dict[str, Any], api_path: str, query: Dict[str, Any] | None = None) -> str:
    app_id = cred.get("appId") or ""
    kdt = cred.get("kdtId") or ""
    params: Dict[str, Any] = {
        "app_id": app_id,
        "kdt_id": kdt,
        "access_token": cred.get("accessToken") or "",
        "appId": app_id,
        "kdtId": kdt,
    }
    if query:
        params.update(query)
    return f"{H5_BASE}{api_path}?{urlencode(params)}"


def api_get(server: str, url: str, cred: Dict[str, Any] | None, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(cred),
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


def api_post(server: str, url: str, cred: Dict[str, Any] | None, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(cred),
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


def get_cached_token(cache_key: str) -> Dict[str, Any] | None:
    cache = load_token_cache()
    data = cache.get(cache_key)
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {cache_key} token")
                return {
                    "accessToken": str(data.get("token", "")),
                    "sessionId": str(data.get("sessionId", "") or ""),
                    "kdtId": str(data.get("kdtId", "") or ""),
                    "appId": str(data.get("appId", "") or ""),
                    "extraData": data.get("extraData") or {},
                }
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(cache_key: str, cred: Dict[str, Any], expire_time: str) -> None:
    cache = load_token_cache()
    cache[cache_key] = {
        "token": cred.get("accessToken", ""),
        "sessionId": cred.get("sessionId", ""),
        "kdtId": cred.get("kdtId", ""),
        "appId": cred.get("appId", ""),
        "extraData": cred.get("extraData") or {},
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def remove_cached_token(cache_key: str) -> None:
    cache = load_token_cache()
    if cache_key in cache:
        del cache[cache_key]
        save_token_cache(cache)


def cache_key_of(server: str, store: Dict[str, str]) -> str:
    return f"{server}#{store.get('appId', '')}#{store.get('kdtId', '')}"


def check_session(server: str, cred: Dict[str, Any], proxies: Dict[str, str] | None) -> bool:
    resp = api_get(server, build_h5_url(cred, SESSION_PATH), cred, proxies)
    return resp.get("code") == 0


def login_with_cache(server: str, store: Dict[str, str], proxies: Dict[str, str] | None) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    """优先使用缓存 token（会话校验接口验证），失效自动 code 刷新"""
    label = store_label(store)
    cache_key = cache_key_of(server, store)

    cache_cred = get_cached_token(cache_key)
    if cache_cred:
        print(f"🔍 [缓存] [{label}] 验证 token")
        try:
            if check_session(server, cache_cred, proxies):
                print(f"✅ [缓存] [{label}] token 有效")
                return cache_cred, None
        except Exception as exc:
            print(f"⚠️ [缓存] [{label}] 验证异常: {exc}")
        print(f"⚠️ [缓存] [{label}] token 已失效，重新登录")

    code = get_code(server, store["appId"])
    if not code:
        return None, None

    cred, raw_login = login_by_code(server, store, code, proxies)
    if not cred:
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
    set_cached_token(cache_key, cred, expire_time)
    return cred, raw_login


def is_already_done(text: Any) -> bool:
    return bool(re.search(r"已签|已经签|签到过|重复|已完成|already", str(text or ""), re.I))


def is_not_member(resp: Dict[str, Any]) -> bool:
    msg = str(resp.get("msg") or resp.get("message") or json_preview(resp, 200))
    try:
        code = int(resp.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    return code == 1000000002 or bool(re.search(r"userId must be|未注册|请先注册|注册会员|开通会员", msg, re.I))


def do_sign(server: str, store: Dict[str, str], cred: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
    label = store_label(store)

    if not store.get("checkinId"):
        print(f"⚠️ [签到] [{label}] 未配置 checkinId，只登录不签到（在小程序签到页抓 checkinV2.json?checkinId= 补上）")
        return "未配置 checkinId，只登录不签到"

    resp = api_get(server, build_h5_url(cred, CHECKIN_PATH, {"checkinId": store["checkinId"]}), cred, proxies)
    if resp.get("code") == 0:
        data = safe_data(resp)
        gain = data.get("point")
        if gain is None:
            gain = data.get("points")
        if gain is None:
            gain = data.get("reward")
        gain_text = f": +{gain}" if gain not in (None, "") else ""
        print(f"✅ [签到] [{label}] 签到成功{gain_text}")
        return f"签到成功{gain_text}"

    msg = str(resp.get("msg") or resp.get("message") or json_preview(resp, 300))
    if is_already_done(msg):
        print(f"✅ [签到] [{label}] 今日已签到（{msg}）")
        return f"今日已签到（{msg}）"
    if is_not_member(resp):
        print(f"⚠️ [签到] [{label}] 该微信号还没在这家店注册会员（有赞签到要先注册），请在小程序里注册一次再跑")
        return "该微信号还没在这家店注册会员"

    print(f"❌ [签到] [{label}] 签到失败: {msg}")
    if re.search(r"登录|token|session|access", msg, re.I):
        print(f"⚠️ [缓存] [{label}] 登录态失效，清除本地缓存")
        remove_cached_token(cache_key_of(server, store))
    return f"签到失败: {msg}"


def run_account(index: int, total: int, server: str, stores: List[Dict[str, str]]) -> Dict[str, Any]:
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

    try:
        sign_lines: List[str] = []
        token_masked = "-"

        for store_index, store in enumerate(stores):
            label = store_label(store)

            cred, raw_login = login_with_cache(server, store, proxies)
            if not cred:
                sign_lines.append(f"{label}: 登录失败: {json_preview(raw_login)}")
                print(f"❌ [登录] [{label}] 登录失败")
                continue

            token_masked = mask(cred.get("accessToken") or cred.get("sessionId") or "")
            result["token"] = token_masked

            sign_lines.append(f"{label}: {do_sign(server, store, cred, proxies)}")

            if store_index < len(stores) - 1:
                wait_time = random.randint(1, 3)
                print(f"⏳ [间隔] 下一个门店前等待 {wait_time}s")
                sleep(wait_time)

        result["signMsg"] = "\n".join(sign_lines) if sign_lines else "无门店结果"
        result["success"] = len(sign_lines) > 0
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🛍️ 有赞店铺签到任务结果

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

    stores = load_stores()
    print(f"🏬 [门店] 共 {len(stores)} 家门店: {'、'.join(store_label(s) for s in stores)}")

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server, stores)
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
    print("║ 🏁 有赞店铺签到任务执行完成                   ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🛍️ 有赞店铺签到任务完成", build_notify(results))


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
