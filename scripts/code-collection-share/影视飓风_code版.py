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
影视飓风小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /wscshop/weapp/authorize.json 使用 code 换 accessToken（有赞 wsc 店铺契约）
  3. /wscump/checkin/show_checkin_page_v2.json 获取签到活动 checkinId（兼作 token 校验）
  4. /wscump/checkin/checkinV2.json 每日签到（自动识别已签到/达上限）
  5. /wscump/integral/user_points.json 查询当前积分
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

说明：登录接口取自源脚本（有赞 weapp authorize，clientId/clientSecret 方式失败自动回退 clientBiz），
      access_token 走查询串（app_id/kdt_id/access_token），Extra-Data 与 Cookie 细节照源脚本保留。

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

import json
import os
import random
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "影视飓风"
APPID = "wx92782ef90ebc836d"

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

BASE_URL = "https://h5.youzan.com"
AUTH_URL = f"{BASE_URL}/wscshop/weapp/authorize.json"
CHECKIN_PAGE_URL = f"{BASE_URL}/wscump/checkin/show_checkin_page_v2.json"
CHECKIN_URL = f"{BASE_URL}/wscump/checkin/checkinV2.json"
POINTS_URL = f"{BASE_URL}/wscump/integral/user_points.json"

CLIENT_ID = "4d65249d377b2c3ed8"
CLIENT_SECRET = "1cdc05151d64f3a4a6ebd0e9de64422a"
GRANT_TYPE = "yz_union"
CLIENT_BIZ = "weapp_wsc"
KDT_ID = "149536603"
USER_VERSION = "2.226.7.101"
PAGE_VERSION = "17"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ysjfcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) MicroMessenger/3.9.12 MiniProgramEnv/Windows WindowsWechat/WMPF"
)

SESSION_ID = ""
COOKIE_HEADER = ""


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


def mask_phone(phone: Any) -> str:
    phone = str(phone or "")
    if len(phone) == 11 and phone.isdigit():
        return f"{phone[:3]}****{phone[7:]}"
    return phone


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🎬 影视飓风小程序动态 code 版                ║")
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


def common_headers(session_id: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Extra-Data": json.dumps(
            {
                "sid": session_id or "",
                "version": USER_VERSION,
                "clientType": "weapp-miniprogram",
                "client": "weapp",
                "bizEnv": "wsc",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if COOKIE_HEADER:
        headers["Cookie"] = COOKIE_HEADER
    return headers


def base_params(token: str, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """有赞 wsc 契约：access_token 走查询串（照源脚本 getBaseParams）"""
    params: Dict[str, Any] = {
        "app_id": APPID,
        "kdt_id": KDT_ID,
        "access_token": token or "",
    }
    if extra:
        params.update(extra)
    return params


def capture_cookies(response: requests.Response) -> None:
    """照源脚本：把响应 Set-Cookie 的首段串起来，后续请求带回"""
    global COOKIE_HEADER
    try:
        cookies = response.raw.headers.getlist("Set-Cookie")  # type: ignore[attr-defined]
    except Exception:
        cookies = []
    if not cookies:
        single = response.headers.get("Set-Cookie") or response.headers.get("set-cookie")
        cookies = [single] if single else []
    parts = []
    for item in cookies:
        seg = str(item).split(";")[0].strip()
        if seg:
            parts.append(seg)
    if parts:
        COOKIE_HEADER = "; ".join(parts)


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


def extract_session_id(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("sessionId", "session_id"):
        if data.get(key):
            return str(data[key])
    inner = data.get("data")
    if isinstance(inner, dict):
        for key in ("sessionId", "session_id"):
            if inner.get(key):
                return str(inner[key])
    return ""


def extract_user_desc(data: Any) -> str:
    """从授权响应提取昵称+脱敏手机号（照源脚本登录成功日志）"""
    if not isinstance(data, dict):
        return "未知"
    src = data.get("data") if isinstance(data.get("data"), dict) else data
    nick = src.get("nick_name") or src.get("nickName") or ""
    mobile = mask_phone(src.get("mobile") or "")
    desc = f"{nick} {mobile}".strip()
    return desc or "未知"


def is_token_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(word in text for word in ("access_token", "token", "登录", "授权", "invalid session", "session"))


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        global SESSION_ID
        print("🔐 [登录] 使用 code 换 token")

        def _authorize(payload: Dict[str, Any]) -> Dict[str, Any]:
            response = request_with_proxy(
                "POST",
                AUTH_URL,
                headers=common_headers(),
                json={**payload, "code": code},
                proxies=proxies,
                server=server,
            )
            capture_cookies(response)
            try:
                return response.json()
            except Exception:
                return {"raw": response.text[:800]}

        # 源脚本：先试 clientId/clientSecret/grantType，失败回退 clientBiz
        data = _authorize({
            "appId": APPID,
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET,
            "grantType": GRANT_TYPE,
        })
        if not extract_token(data):
            print("🔁 [登录] 主授权方式未取得 token，改用 clientBiz 重试")
            data = _authorize({
                "appId": APPID,
                "clientBiz": CLIENT_BIZ,
            })

        token = extract_token(data)
        if not token:
            print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
            return None, data

        SESSION_ID = extract_session_id(data)
        print(f"✅ [登录] token 获取成功: {mask(token)}")
        return token, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(SESSION_ID),
        params=base_params(token, params),
        proxies=proxies,
        server=server,
    )
    capture_cookies(response)
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any], params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(SESSION_ID),
        json=payload,
        params=base_params(token, params),
        proxies=proxies,
        server=server,
    )
    capture_cookies(response)
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


def get_cached_extra(server: str) -> Dict[str, str]:
    cache = load_token_cache()
    data = cache.get(server)
    if isinstance(data, dict):
        return {
            "sessionId": str(data.get("sessionId") or ""),
            "cookie": str(data.get("cookie") or ""),
        }
    return {"sessionId": "", "cookie": ""}


def set_cached_token(server: str, token: str, expire_time: str, extra: Dict[str, Any] | None = None) -> None:
    cache = load_token_cache()
    entry: Dict[str, Any] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    if extra:
        entry.update(extra)
    cache[server] = entry
    save_token_cache(cache)


def clear_cached_token(server: str) -> None:
    try:
        cache = load_token_cache()
        if server in cache:
            del cache[server]
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            print(f"🗑️ [缓存] 已清除 {server} 失效 token")
    except Exception as exc:
        print(f"⚠️ [缓存] 清除失败: {exc}")


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（签到页接口验证），失效自动 code 刷新"""
    global SESSION_ID, COOKIE_HEADER
    cache_token = get_cached_token(server)
    if cache_token:
        extra = get_cached_extra(server)
        SESSION_ID = extra.get("sessionId", "")
        COOKIE_HEADER = extra.get("cookie", "")
        print("🔍 [缓存] 验证 token")
        try:
            page_resp = api_get(server, CHECKIN_PAGE_URL, cache_token, proxies)
            if page_resp.get("code") == 0:
                print("✅ [缓存] token 有效")
                return cache_token, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        SESSION_ID = ""
        COOKIE_HEADER = ""
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
    set_cached_token(server, token, expire_time, {"sessionId": SESSION_ID, "cookie": COOKIE_HEADER})
    return token, raw_login


# ====================== 签到业务（移植自源脚本 Task 类） ======================
def show_checkin_page(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    resp = api_get(server, CHECKIN_PAGE_URL, token, proxies)
    if resp.get("code") != 0:
        msg = resp.get("msg") or json_preview(resp, 300)
        if is_token_error(msg):
            clear_cached_token(server)
        print(f"⚠️ [签到] 获取签到活动失败: {msg}")
        return ""
    data = safe_data(resp)
    checkin_id = str(data.get("checkinId") or "")
    is_show = bool(data.get("isShow"))
    print(f"📋 [签到] 签到活动: checkinId={checkin_id or '未获取'} isShow={is_show}")
    return checkin_id


def do_checkin(server: str, token: str, proxies: Dict[str, str] | None, checkin_id: str) -> str:
    if not checkin_id:
        print("⚠️ [签到] 未获取到 checkinId，跳过签到")
        return "未获取到 checkinId，跳过签到"

    resp = api_get(server, CHECKIN_URL, token, proxies, params={"checkinId": checkin_id})
    if resp.get("code") == 0:
        data = safe_data(resp)
        awards_list: List[str] = []
        for item in data.get("list") or []:
            if not isinstance(item, dict):
                continue
            infos = item.get("infos")
            if isinstance(infos, dict) and infos.get("title"):
                awards_list.append(str(infos["title"]))
        awards = "、".join(awards_list)
        desc = data.get("desc") or ""
        msg = f"签到成功: {desc}" + (f" {awards}" if awards else "")
        print(f"✅ [签到] {msg}")
        return msg

    msg = resp.get("msg") or json_preview(resp, 300)
    if any(word in msg for word in ("已达最大参与次数", "已签到", "重复签到")):
        print("✅ [签到] 今日已签到")
        return "今日已签到"
    if is_token_error(msg):
        clear_cached_token(server)
    print(f"❌ [签到] 签到失败: {msg}")
    return f"签到失败: {msg}"


def get_points(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    resp = api_get(server, POINTS_URL, token, proxies)
    if resp.get("code") != 0:
        msg = resp.get("msg") or json_preview(resp, 300)
        print(f"⚠️ [积分] 查询积分失败: {msg}")
        return f"查询失败: {msg}"
    data = safe_data(resp)
    points = data.get("current_points")
    if points is None:
        points = data.get("real_points")
    if points is None:
        points = "未知"
    print(f"💰 [积分] 当前积分: {points}")
    return str(points)


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "userMsg": "-",
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

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)
    if raw_login:
        result["userMsg"] = extract_user_desc(raw_login)

    try:
        checkin_id = show_checkin_page(server, token, proxies)
        result["signMsg"] = do_checkin(server, token, proxies, checkin_id)
        result["pointsMsg"] = get_points(server, token, proxies)

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🎬 影视飓风动态 code 任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
👤 用户：{res["userMsg"]}
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
                "userMsg": "-",
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
    print("║ 🏁 影视飓风任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🎬 影视飓风任务完成", build_notify(results))


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
