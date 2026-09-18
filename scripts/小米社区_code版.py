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
小米社区微信小程序动态 code 版（appid wx240a4a764023c444）

功能：
  1. 本地 code 服务获取微信 code（附带 /wx/getuserinfo 加密资料，登录时写 userInfo cookie）
  2. account.xiaomi.com 微信登录链（源码接口，迁移自原脚本）：
     v2/code 换 wxSToken → v3/tokenLogin 换 passToken → serviceLogin 拿 STS → STS 换 wx_vip_ph
     passToken/userId/cUserId 按 code 服务地址缓存本地 JSON，复用期内跳过 tokenLogin
  3. 查询签到状态（/mtop/planet/wechat/checkin/mypagedata）
  4. 每日签到（/mtop/planet/wechat/member/addCommunityGrowUpPointByActionV2）
  5. 查询成长值（/mtop/planet/wechat/growup/points/detail）
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

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
import urllib.parse
import uuid as uuid_module
from datetime import datetime
from typing import Any, Dict, List, Tuple

import requests


APP_NAME = "小米社区"
APPID = "wx240a4a764023c444"

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

ACCOUNT_BASE = "https://account.xiaomi.com"
BASE_URL = "https://api.vip.miui.com"

WX_CODE_URL = "http://{server}/login"
WX_USERINFO_URL = "http://{server}/wx/getuserinfo"
WX_CODE_V2_URL = f"{ACCOUNT_BASE}/pass/sns/wxapp/v2/code"
TOKEN_LOGIN_URL = f"{ACCOUNT_BASE}/pass/sns/wxapp/v3/tokenLogin"
SERVICE_LOGIN_URL = f"{ACCOUNT_BASE}/pass/serviceLogin"

MYPAGE_URL = f"{BASE_URL}/mtop/planet/wechat/checkin/mypagedata"
SIGN_URL = f"{BASE_URL}/mtop/planet/wechat/member/addCommunityGrowUpPointByActionV2"
POINTS_URL = f"{BASE_URL}/mtop/planet/wechat/growup/points/detail"

SID = "wx_vip"
PAGE_VERSION = "73"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xmsqcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.73(0x18004939) NetType/WIFI Language/zh_CN"
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
    print("║ 📱 小米社区动态 code 版                       ║")
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


def get_userinfo_blob(server: str) -> Dict[str, Any] | None:
    """从 code 服务取微信 getUserInfo 加密资料（登录前写 userInfo cookie，否则 tokenLogin 302）"""
    url = f"http://{server}/wx/getuserinfo"
    try:
        response = direct_session().post(
            url,
            json={"appid": APPID},
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        data = response.json()
        inner = safe_data(data)
        if data.get("status") is False or not inner or not inner.get("encryptedData"):
            print(f"⚠️ [授权] getUserInfo 资料不可用: {json_preview(data, 200)}")
            return None

        try:
            profile = json.loads(inner.get("data") or "{}")
        except Exception:
            profile = {}
        # rawData 用微信原样返回串（保持 signature 有效）；cloud_id → cloudID
        return {
            "cloudID": inner.get("cloud_id"),
            "encryptedData": inner.get("encryptedData"),
            "iv": inner.get("iv"),
            "signature": inner.get("signature"),
            "userInfo": profile,
            "rawData": inner.get("data"),
            "errMsg": "getUserInfo:ok",
        }
    except Exception as exc:
        print(f"⚠️ [授权] getUserInfo 请求异常: {exc}")
        return None


def common_headers(session: "XiaomiSession", token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
        "Origin": "https://servicewechat.com",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    cookie = session.cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("passToken"),
        data.get("token"),
        data.get("wxSToken"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("passToken"),
            inner.get("token"),
            inner.get("wxSToken"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def parse_xiaomi_json(text: Any) -> Dict[str, Any]:
    """小米部分 JSON 接口以 &&&START&&& 开头"""
    if text is None:
        return {}
    if isinstance(text, dict):
        return text
    raw = str(text)
    if raw.startswith("&&&START&&&"):
        raw = raw[len("&&&START&&&"):]
    try:
        return json.loads(raw)
    except Exception:
        return {"__raw": raw[:300]}


class XiaomiSession:
    """轻量 cookie 容器：按源脚本逻辑手工管理 set-cookie（deviceId/wxSToken/userInfo/passToken/wx_vip_ph）"""

    def __init__(self) -> None:
        self.cookies: Dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self.cookies[name] = str(value)

    def get(self, name: str) -> str | None:
        return self.cookies.get(name)

    def absorb(self, response: requests.Response) -> None:
        try:
            for line in response.headers.get("Set-Cookie", "").split(","):
                seg = line.split(";")[0].strip()
                if "=" in seg:
                    name, _, value = seg.partition("=")
                    if name.strip():
                        self.cookies[name.strip()] = value.strip()
        except Exception:
            pass

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


def login_by_code(server: str, code: str, session: XiaomiSession, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """小米微信登录链：v2/code → tokenLogin → serviceLogin → STS，返回 wx_vip_ph 会话票据"""
    try:
        print("🔐 [登录] 使用 code 换 token")

        userinfo_blob = get_userinfo_blob(server)
        session.set("deviceId", f"wp_{uuid_module.uuid4()}")

        # 1) 微信 code 换 wxSToken
        resp1 = request_with_proxy(
            "POST",
            WX_CODE_V2_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
                "Origin": "https://servicewechat.com",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "code": code,
                "appid": APPID,
                "sid": SID,
                "userInfo": "true",
                "_locale": "zh_CN",
            },
            proxies=proxies,
            server=server,
        )
        session.absorb(resp1)
        body1 = parse_xiaomi_json(resp1.text)
        if body1.get("code") != 0:
            desc = body1.get("description") or body1.get("desc") or body1.get("message") or json_preview(body1)
            print(f"❌ [登录] 微信换取小米登录票据失败(code={body1.get('code')}, {desc})")
            return None, body1

        wx_stoken = (body1.get("data") or {}).get("wxSToken")
        if not wx_stoken:
            print(f"❌ [登录] 小米登录响应缺少 wxSToken: {json_preview(body1)}")
            return None, body1
        session.set("wxSToken", wx_stoken)
        # 关键：把微信 userInfo 写进 cookie，tokenLogin 才会返回 JSON 会话（缺它会 302）
        if userinfo_blob:
            session.set("userInfo", urllib.parse.quote(json.dumps(userinfo_blob, ensure_ascii=False)))

        # 2) 建立小米账号会话（返回 passToken；老账号可能 302，退回本地缓存）
        cached = load_token_cache().get(server) or {}
        resp2 = request_with_proxy(
            "POST",
            TOKEN_LOGIN_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
                "Origin": "https://servicewechat.com",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "sid": SID,
                "appid": APPID,
                "callback": "",
                "authType": "1",
                "wxSToken": wx_stoken,
                "_locale": "zh_CN",
            },
            proxies=proxies,
            server=server,
            allow_redirects=False,
        )
        session.absorb(resp2)

        session_data: Dict[str, Any] = {}
        if 300 <= resp2.status_code < 400:
            if not cached.get("passToken") or not cached.get("userId"):
                print("❌ [登录] 小米账号已有登录态但本地缺少首登票据，请在真机小程序登录一次以生成缓存后再跑")
                return None, {"code": -1, "msg": "本地缺少首登票据"}
            session_data = cached
            print("✅ [登录] 已加载本地缓存会话票据")
        else:
            body2 = parse_xiaomi_json(resp2.text)
            if not body2.get("passToken"):
                if body2.get("code") == 20003:
                    print(f"❌ [登录] 小米账号不存在(20003 用户不存在)，该微信号尚未注册小米账号/社区")
                elif body2.get("code") == 24023:
                    print(f"❌ [登录] 小米账号未设置密码(24023)，微信绑定的小米账号未完成注册激活")
                else:
                    print(f"❌ [登录] 小米会话建立失败: {json_preview(body2)}")
                return None, body2
            session_data = {
                "passToken": body2.get("passToken"),
                "userId": body2.get("userId"),
                "cUserId": body2.get("cUserId"),
            }
            set_cached_token(server, str(body2.get("passToken")), datetime.now().isoformat(), {
                "userId": body2.get("userId"),
                "cUserId": body2.get("cUserId"),
            })

        for key in ("passToken", "userId", "cUserId"):
            if session_data.get(key):
                session.set(key, session_data[key])

        # 3) serviceLogin 拿 STS 跳转地址
        resp3 = request_with_proxy(
            "GET",
            SERVICE_LOGIN_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
                "Cookie": session.cookie_header(),
            },
            params={"sid": SID, "_json": "true", "_locale": "zh_CN"},
            proxies=proxies,
            server=server,
        )
        session.absorb(resp3)
        body3 = parse_xiaomi_json(resp3.text)
        sts_url = body3.get("location")
        if body3.get("code") != 0 or not sts_url:
            print(f"❌ [登录] serviceLogin 失败: {json_preview(body3)}")
            return None, body3

        # 4) STS 登录，拿 wx_vip_ph 社区会话票据
        resp4 = request_with_proxy(
            "GET",
            sts_url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
                "Cookie": session.cookie_header(),
            },
            proxies=proxies,
            server=server,
        )
        session.absorb(resp4)
        body4 = parse_xiaomi_json(resp4.text)
        if body4.get("S") != "OK":
            print(f"❌ [登录] STS 登录失败: {json_preview(body4)}")
            return None, body4

        ph = session.get("wx_vip_ph")
        if not ph:
            print(f"❌ [登录] STS 未返回 wx_vip_ph: {json_preview(body4)}")
            return None, body4

        print(f"✅ [登录] token 获取成功: {mask(ph)}")
        return ph, body4
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, session: XiaomiSession, ph: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
            "Origin": "https://servicewechat.com",
            "Cookie": session.cookie_header(),
        },
        params={"wx_vip_ph": ph},
        proxies=proxies,
        server=server,
    )
    return parse_xiaomi_json(response.text)


def api_post_form(server: str, url: str, session: XiaomiSession, ph: str, proxies: Dict[str, str] | None, form: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
            "Origin": "https://servicewechat.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": session.cookie_header(),
        },
        params={"wx_vip_ph": ph},
        data=form,
        proxies=proxies,
        server=server,
    )
    return parse_xiaomi_json(response.text)


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


def set_cached_token(server: str, token: str, expire_time: str, extra: Dict[str, Any] | None = None) -> None:
    cache = load_token_cache()
    entry: Dict[str, Any] = {
        "token": token,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    if extra:
        entry.update(extra)
    cache[server] = entry
    save_token_cache(cache)


def login_with_cache(server: str, session: XiaomiSession, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """缓存里存的是 passToken/userId/cUserId 首登票据（供 tokenLogin 302 兜底），登录链每次走 code"""
    code = get_code(server)
    if not code:
        return None, None

    token, raw_login = login_by_code(server, code, session, proxies)
    if not token:
        return None, raw_login
    return token, raw_login


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

    session = XiaomiSession()
    ph, raw_login = login_with_cache(server, session, proxies)
    if not ph:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(ph)

    try:
        # 查询用户信息
        user_resp = api_get(server, f"{BASE_URL}/mtop/planet/lite/userinfo", session, ph, proxies)
        if user_resp.get("status") == 200:
            entity = user_resp.get("entity") or {}
            username = entity.get("username") or "-"
            user_id = entity.get("userId") or "-"
            result["userMsg"] = f"{username}({user_id})"
            print(f"👤 [用户] {result['userMsg']}")
        else:
            print(f"⚠️ [用户] 获取用户信息失败: {json_preview(user_resp, 200)}")

        wait_time = random.randint(1, 2)
        print(f"⏳ [签到] 查询签到状态前等待 {wait_time}s")
        sleep(wait_time)

        # 查询签到状态（entity.data[].title=="每日签到" 且 button=="已签到" 即已签）
        status = api_get(server, MYPAGE_URL, session, ph, proxies)
        if status.get("code") == 401:
            result["error"] = "SESSION_INVALID:小米登录态无效"
            print(f"❌ [签到] {result['error']}")
            return result

        buttons = (status.get("entity") or {}).get("data") or []
        already = False
        if isinstance(buttons, list):
            for item in buttons:
                if not isinstance(item, dict):
                    continue
                sub_buttons = item.get("buttons") or []
                first_button = sub_buttons[0].get("button") if isinstance(sub_buttons, list) and sub_buttons and isinstance(sub_buttons[0], dict) else None
                if item.get("title") == "每日签到" and first_button == "已签到":
                    already = True
                    break

        if already:
            result["signMsg"] = "今日已签到，无需重复执行"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            sign_resp = api_post_form(server, SIGN_URL, session, ph, proxies, {"action": "WECHAT_CHECKIN_TASK"})
            if sign_resp.get("message") == "success":
                entity = sign_resp.get("entity") or {}
                title = entity.get("title") or "获得成长值"
                score = entity.get("score")
                extra = f"，{title}" if entity.get("title") else f"，积分+{score}" if score is not None else ""
                result["signMsg"] = f"签到成功{extra}"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                msg = str(sign_resp.get("message") or sign_resp.get("description") or sign_resp.get("desc") or json_preview(sign_resp, 200))
                import re
                if re.search(r"已签|签到过|重复|已完成", msg):
                    result["signMsg"] = f"今日已签到（{msg}）"
                    print(f"✅ [签到] {result['signMsg']}")
                else:
                    result["signMsg"] = f"签到失败: {msg}"
                    print(f"❌ [签到] {result['signMsg']}")

        wait_time = random.randint(1, 2)
        print(f"⏳ [成长值] 查询成长值前等待 {wait_time}s")
        sleep(wait_time)

        # 查询成长值
        points_resp = api_get(server, POINTS_URL, session, ph, proxies)
        if points_resp.get("status") == 200:
            entity = points_resp.get("entity") or []
            total_points = 0
            if isinstance(entity, list):
                for item in entity:
                    if not isinstance(item, dict):
                        continue
                    jump_text = str(item.get("jumpText") or "")
                    digits = "".join(ch for ch in jump_text if ch.isdigit())
                    if digits:
                        total_points += int(digits)
            result["pointsMsg"] = f"{total_points} 成长值"
            print(f"🌱 [成长值] 当前成长值: {total_points}")
        else:
            print(f"⚠️ [成长值] 查询成长值失败: {json_preview(points_resp, 200)}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""📱 小米社区任务结果

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
🌱 成长值：{res["pointsMsg"]}
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
    print("║ 🏁 小米社区任务执行完成                       ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("📱 小米社区任务完成", build_notify(results))


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
