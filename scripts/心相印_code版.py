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
芯享会（心相印）code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /user-member/auto-login 使用 code 换 access_token（未注册时回退 /user-member/user-auth 授权注册）
  3. 查询签到状态（连续签到天数 / 好奇豆）
  4. 每日签到得好奇豆
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

接口与签名逻辑来自小程序反编译源码（utils/util.js 签名盐、utils/http.js httpReq），
登录接口为源码接口（auto-login / user-auth），code 来源改为本地 code 服务。

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       本地 code 服务地址，可选（默认 127.0.0.1:8088）
  xxh_version       miniProgram.version，默认空串
  xxh_nickname      首次授权注册提交的昵称，默认 微信用户

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import hashlib
import json
import os
import random
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "芯享会（心相印）"
# 真实小程序 appid（2026-09-15 抓包坐实 servicewechat.com/wxfc766f1e9a63b01f/340）
APPID = os.getenv("XXH_APPID", "wxfc766f1e9a63b01f")

SERVERS = ["10.30.9.183:8088"]

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

BASE_URL = "https://jbl-xxh-api.91dh.com.cn"
SALT = "mGiz2csojwbADX9DETPK38jbFpw28YOj"          # utils/util.js 签名盐
SUCCESS_CODE = 10000                                # 1e4
SESSION_INVALID_CODE = 20003                        # 登录会话失效 -> 需重新登录
NEED_AUTH_CODES = (20008, 20010)                    # 需授权 / 需手机号 (账号前置条件)

AUTO_LOGIN_URL = "/user-member/auto-login"
USER_AUTH_URL = "/user-member/user-auth"
SIGN_IN_LIST_URL = "/user-member/sign-in-list"
SIGN_IN_URL = "/user-member/sign-in"

# miniProgram.version, 发布版通常为空串; 服务端按传入值重算签名, 一般无需修改
VERSION = os.getenv("xxh_version", "")
# 首次登录(注册)时提交的昵称; 仅在账号未授权时使用, 不会覆盖已注册账号

# ========== HAR 抓包坐实的备用后端（mshopapi.hengan.cn） ==========
# 该后端与上面的 91dh 后端是两套系统；token 互不通用。
# 抓包实测：Authorization: Bearer <UUID> 且签到成功（+5积分）。
HENGAN_BASE = os.getenv("HENGAN_BASE", "https://mshopapi.hengan.cn/mall/app").rstrip("/")
HENGAN_APPID = os.getenv("HENGAN_APPID", "wxfc766f1e9a63b01f")
HENGAN_TOKEN = os.getenv("HENGAN_TOKEN", "")   # 抓包得到的 Bearer token（可选）
HENGAN_USERINFO = "/userinfo?login=true"
HENGAN_CHECK_TOKEN = "/anon/api/auth/checkAppToken"
HENGAN_SIGN = "/sign/user"
HENGAN_SIGN_INTEGRAL = "/api/sign/v2/integral"
HENGAN_SIGN_CALENDAR = "/api/sign/v2/currentMonthSignInfo"
HENGAN_SIGN_CONFIG = "/api/sign/v2/currentActiveSignConfig"
HENGAN_SIGN_REWARDS = "/api/sign/v2/mySignRewardList"
HENGAN_APP_VERSION = "1.2.11"
# 真实登录接口（逆向 __APP__.wxapkg 坐实）：POST /auth/app/anon/oauth/wxappLogin
HENGAN_OAUTH_BASE = os.getenv("HENGAN_OAUTH_BASE", "https://mshopapi.hengan.cn").rstrip("/")
HENGAN_OAUTH_LOGIN = "/auth/app/anon/oauth/wxappLogin"

NICKNAME = os.getenv("xxh_nickname", "微信用户")
CACHE_DIR = os.environ.get("CODE_CACHE_DIR", os.path.join(os.path.expanduser("~"), "Documents", "写代码"))

os.makedirs(CACHE_DIR, exist_ok=True)

COOKIE_FILE = os.path.join(CACHE_DIR, "xxhcookie.json")
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-G9910 Build/TP1A.220624.014) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.49.2600(0x28003137) NetType/WIFI Language/zh_CN "
    "miniProgram/" + APPID
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
    print("║ 🧻 芯享会（心相印）code 版                     ║")
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
    return {
        "content-type": "application/json",
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{APPID}/0/page-frame.html",
    }


# ========== 业务辅助函数（照源脚本） ==========
def now_time() -> str:
    """本地(北京)时间 YYYY-MM-DD HH:MM:SS, 对应 util.getNowTime()。"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def sign_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """复刻 util.getRequestData: 键名排序后拼接 key+value, sign=sha1(SALT+拼接+SALT)。"""
    concat = ""
    for key in sorted(fields.keys()):
        value = fields[key]
        if isinstance(value, (dict, list)):
            concat += key + json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        else:
            concat += key + str(value)
    signed = dict(fields)
    signed["sign"] = hashlib.sha1((SALT + concat + SALT).encode("utf-8")).hexdigest()
    return signed


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

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def api_request(endpoint: str, method: str, token: str, proxies: Dict[str, str] | None, params: Any = None, server: str = "") -> Dict[str, Any]:
    """复刻 utils/http.js httpReq: 签名后 GET 走 query、POST 走 body。"""
    fields: Dict[str, Any] = {
        "timestamp": now_time(),
        "access_token": token or "",
        "version": VERSION,
    }
    if params is not None and params != "":
        fields["params"] = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    signed = sign_payload(fields)

    url = f"{BASE_URL}{endpoint}?access_token={token or ''}"
    headers = common_headers(token)
    if method.upper() == "GET":
        response = request_with_proxy("GET", url, params=signed, headers=headers, proxies=proxies, server=server)
    else:
        response = request_with_proxy(
            "POST",
            url,
            data=json.dumps(signed, separators=(",", ":"), ensure_ascii=False),
            headers=headers,
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


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    return api_request(url, "GET", token, proxies, None, server)


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    return api_request(url, "POST", token, proxies, payload, server)


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """wx.login 的 code -> Bearer token（真实接口，逆向 __APP__.wxapkg 坐实）。

    JS 源码：
        g.request("/auth/app/anon/oauth/wxappLogin",
                  {code: <wx.login code>, spread: 0, labelValue: null},
                  {headers: {appVersion, envVersion}, method: "post"})
        -> resp.data.data.token，写入 storage["login_status"]
    注意：该接口挂在域名根路径，不在 /mall/app 前缀下。
    """
    try:
        print("🔐 [登录] 使用 code 换 token (/auth/app/anon/oauth/wxappLogin)")
        url = HENGAN_OAUTH_BASE + HENGAN_OAUTH_LOGIN
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "xweb_xhr": "1",
            "appVersion": HENGAN_APP_VERSION,
            "envVersion": "release",
            "Referer": f"https://servicewechat.com/{APPID}/340/page-frame.html",
        }
        body = {"code": code, "spread": 0, "labelValue": None}
        response = request_with_proxy(
            "POST", url,
            headers=headers,
            data=json.dumps(body, separators=(",", ":"), ensure_ascii=False),
            proxies=proxies, server=server,
        )
        try:
            result = response.json()
        except Exception:
            result = {"raw": response.text[:500]}

        # 成功: {"code":200,"data":{"token":"<uuid>"}}；失败: {"msg":...,"code":500}
        token = extract_token(result)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, result

        print(f"❌ [登录] code 换 token 失败: {json_preview(result)}")
        return None, result
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


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（/anon/api/auth/checkAppToken 验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            if hengan_check_token(cache_token, proxies, server):
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

def get_user_nickname(raw_login: Dict[str, Any] | None) -> str:
    """从登录响应 member_info 中取昵称"""
    if isinstance(raw_login, dict):
        inner = raw_login.get("data")
        if isinstance(inner, dict):
            member = inner.get("member_info") or {}
            if isinstance(member, dict):
                return member.get("nickname") or member.get("nick_name") or ""
    return ""


# ====================== HAR 后端（mshopapi.hengan.cn） ======================
def hengan_headers(token: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": USER_AGENT,
        "appVersion": HENGAN_APP_VERSION,
        "envVersion": "release",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{HENGAN_APPID}/340/page-frame.html",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def hengan_get(path: str, token: str = "", proxies: Dict[str, str] | None = None,
               server: str = "", params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET", f"{HENGAN_BASE}{path}",
        headers=hengan_headers(token), params=params,
        proxies=proxies, server=server,
    )
    try:
        return response.json()
    except Exception:
        return {"status": -1, "msg": f"JSON解析失败: {response.text[:200]}"}


def hengan_post(path: str, token: str, body: Any, proxies: Dict[str, str] | None = None,
                server: str = "") -> Dict[str, Any]:
    response = request_with_proxy(
        "POST", f"{HENGAN_BASE}{path}",
        headers=hengan_headers(token), json=body,
        proxies=proxies, server=server,
    )
    try:
        return response.json()
    except Exception:
        return {"status": -1, "msg": f"JSON解析失败: {response.text[:200]}"}


def hengan_check_token(token: str, proxies: Dict[str, str] | None, server: str) -> bool:
    """校验 Bearer token 是否有效（HAR 坐实的 anon 校验端点）"""
    try:
        data = hengan_get(HENGAN_CHECK_TOKEN, token, proxies, server, {"token": token})
        return bool(data.get("data")) and data.get("success") is True
    except Exception:
        return False


def hengan_run(server: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    """走 HAR 坐实的 hengan 后端：读用户 → 签到 → 领积分 → 查日历"""
    out: Dict[str, Any] = {"userMsg": "-", "signMsg": "-", "pointsMsg": "-", "success": False}

    info = hengan_get(HENGAN_USERINFO, token, proxies, server)
    d = info.get("data") or {}
    if info.get("success") is not True:
        out["error"] = f"读取用户失败: {json_preview(info, 200)}"
        return out
    out["userMsg"] = f"{d.get('nickname') or '微信用户'} ({d.get('phone') or '-'})"
    print(f"👤 [用户] {out['userMsg']}")

    # 签到
    sign = hengan_post(HENGAN_SIGN, token, {"sign": 1, "integral": 1, "all": 1}, proxies, server)
    sd = sign.get("data") or {}
    if sign.get("success") is True:
        if sd.get("isDaySign") is True:
            out["signMsg"] = f"今日已签到（连续 {sd.get('sumSignDay', '?')} 天）"
        else:
            out["signMsg"] = f"签到成功（连续 {sd.get('sumSignDay', '?')} 天）"
        print(f"✅ [签到] {out['signMsg']}")
    else:
        out["signMsg"] = sign.get("msg") or json_preview(sign, 150)
        print(f"⚠️ [签到] {out['signMsg']}")

    # 领积分（HAR：签到获得5积分）
    pts = hengan_post(HENGAN_SIGN_INTEGRAL, token, {}, proxies, server)
    if pts.get("success") is True:
        gained = (pts.get("data") or {}).get("integral")
        out["pointsMsg"] = f"+{gained} 积分" if gained is not None else (pts.get("msg") or "已领取")
        print(f"🎁 [积分] {out['pointsMsg']}")
    else:
        out["pointsMsg"] = pts.get("msg") or "-"
        print(f"⚠️ [积分] {out['pointsMsg']}")

    # 日历（可选）
    cal = hengan_get(HENGAN_SIGN_CALENDAR, token, proxies, server)
    if cal.get("success") is True and isinstance(cal.get("data"), list):
        signed = sum(1 for x in cal["data"] if isinstance(x, dict) and x.get("signed"))
        print(f"📅 [日历] 本月已签 {signed} 天")

    out["success"] = True
    return out


def do_sign_in(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[str, bool]:
    """每日签到，返回 (描述, 是否成功)"""
    result = api_request(SIGN_IN_URL, "GET", token, proxies, {}, server)
    rcode = int(result.get("code", -1))
    if rcode == SUCCESS_CODE:
        msg = "签到成功"
        rdata = safe_data(result)
        score = rdata.get("score") or rdata.get("point")
        if score:
            msg += f", +{score} 好奇豆"
        print(f"🎉 [签到] {msg}")
        return msg, True

    msg = result.get("msg") or f"签到失败 code={rcode}"
    print(f"❌ [签到] {msg}")
    return msg, False


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

    # ① 抓包 token（可选，HENGAN_TOKEN 环境变量）
    if HENGAN_TOKEN:
        print("🔍 [heng] 校验抓包 Bearer token")
        if hengan_check_token(HENGAN_TOKEN, proxies, server):
            print("✅ [heng] 抓包 token 有效")
            hres = hengan_run(server, HENGAN_TOKEN, proxies)
            result["token"] = mask(HENGAN_TOKEN)
            result["userMsg"] = hres.get("userMsg", "-")
            result["signMsg"] = hres.get("signMsg", "-")
            result["pointsMsg"] = hres.get("pointsMsg", "-")
            result["success"] = bool(hres.get("success"))
            if not hres.get("success"):
                result["error"] = hres.get("error") or "heng 后端执行失败"
            return result
        print("⚠️ [heng] 抓包 token 无效或已过期，回退 code 登录")

    # ② code 登录 -> 真实 hengan 后端签到（全自动，无需抓包凭证）
    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)
    hres = hengan_run(server, token, proxies)
    result["userMsg"] = hres.get("userMsg", "-")
    result["signMsg"] = hres.get("signMsg", "-")
    result["pointsMsg"] = hres.get("pointsMsg", "-")
    result["success"] = bool(hres.get("success"))
    if not hres.get("success"):
        result["error"] = hres.get("error") or "签到失败"
    return result

    # ---- 以下为旧 91dh 后端流程（已停用，保留作参考） ----

    try:
        state = api_get(server, SIGN_IN_LIST_URL, token, proxies)

        # 会话失效 -> 强制重新登录后重试一次 (对应源码 20003 自动重登)
        if int(state.get("code", -1)) == SESSION_INVALID_CODE:
            print("⚠️ [会话] token 已失效, 重新登录...")
            code = get_code(server)
            if not code:
                result["error"] = "会话失效且重新获取 code 失败"
                return result
            token, raw_login = login_by_code(server, code, proxies)
            if not token:
                result["error"] = f"重新登录失败: {json_preview(raw_login)}"
                return result
            result["token"] = mask(token)
            expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
            set_cached_token(server, token, expire_time)
            state = api_get(server, SIGN_IN_LIST_URL, token, proxies)

        code = int(state.get("code", -1))
        if code in NEED_AUTH_CODES:
            if code == 20010:
                msg = "需先在小程序「我的→签到→手机号授权」绑定手机号后才能签到"
            else:
                msg = "需先在小程序内完成注册授权后才能签到"
            print(f"⚠️ [签到] {msg} (code={code})")
            result["signMsg"] = msg
            return result

        if code != SUCCESS_CODE:
            msg = state.get("msg") or f"查询签到状态失败 code={code}"
            print(f"❌ [签到] {msg}")
            result["signMsg"] = msg
            return result

        data = safe_data(state)
        status = data.get("status")
        xq_count = data.get("xqCount")
        result["pointsMsg"] = str(xq_count if xq_count is not None else "-")
        print(f"📋 [签到] 签到状态 status={status} 连续签到={xq_count}")

        # status==2 表示今日可签到 (welfare/index.wxml: 签到按钮 wx:if=status==2)
        if status != 2:
            msg = f"今日无需签到 (status={status}, 连续签到 {xq_count} 天)"
            print(f"✅ [签到] {msg}")
            result["signMsg"] = msg
            result["success"] = True
            return result

        sign_msg, ok = do_sign_in(server, token, proxies)
        result["signMsg"] = sign_msg
        result["success"] = ok
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🧻 芯享会（心相印）任务结果

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
💰 好奇豆：{res["pointsMsg"]}
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
    print("║ 🏁 芯享会任务执行完成                          ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🧻 芯享会任务完成", build_notify(results))


# YYB_SERVER 多账号适配：必须在 main() 前安装，避免首轮运行使用旧 code 服务。
from yyb_compat import install as _install_yyb
_install_yyb(globals())

if __name__ == "__main__":
    main()
