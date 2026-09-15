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
七彩虹商城（多彩商城）动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. /api/User/OnLogin 使用 code 换 OpenId（顺带 Token），必要时配合抓包 body 完成
     /api/User/DecryptPhoneNumber 手机号授权首登换 Token/RefreshToken
  3. 每日签到（旧版 /User/Sign + 新版 /User/SignV2）
  4. 积分任务列表驱动：社区签到/会员信息完善/社区内容发布/社区内容评论
  5. 可选抽奖（大转盘），自动消耗剩余次数
  6. 查询积分、连续签到天数
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  COLORFUL_HOST     接口域名，默认 shopapitest.skycolorful.com
                    （可选 shop.skycolorful.com:45677 / interface.skycolorful.com）
  COLORFUL_RAFFLE   是否开启抽奖，'true' 开启，默认关闭
  COLORFUL_BODY     可选，抓包 /api/User/DecryptPhoneNumber 请求体 JSON（一行），
                    用于 OpenId 换不到 Token 时的手机号授权首登兜底

⚠️ 该应用仅 /api/User/DecryptPhoneNumber 签发业务 Token，需要微信手机号授权 code；
   仅凭 wx.login code 只能拿到 OpenId，无法自动首登时请配置 COLORFUL_BODY 抓包兜底。

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
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "七彩虹商城小程序"
APPID = "wx49018277e65fc3e1"

SERVERS = [
    "127.0.0.1:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

COLORFUL_HOST = os.getenv("COLORFUL_HOST", "shopapitest.skycolorful.com")
RAFFLE_ENABLED = os.getenv("COLORFUL_RAFFLE", "false") == "true"
COLORFUL_BODY = os.getenv("COLORFUL_BODY", "")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = f"https://{COLORFUL_HOST}"
LOGIN_ON_URL = f"{BASE_URL}/api/User/OnLogin"
LOGIN_PHONE_URL = f"{BASE_URL}/api/User/DecryptPhoneNumber"
REFRESH_LOGIN_URL = f"{BASE_URL}/api/User/RefreshLoginTime"
USER_INFO_URL = f"{BASE_URL}/api/User/GetUserInfo"
POINT_CONFIG_URL = f"{BASE_URL}/api/Sys/GetPointConfig"
SIGN_URL = f"{BASE_URL}/api/User/Sign"
SIGN_V2_URL = f"{BASE_URL}/api/User/SignV2"
SIGN_DAYS_URL = f"{BASE_URL}/api/User/SignDays"
EDIT_INFO_URL = f"{BASE_URL}/api/User/EditInfo"
DO_EDIT_INFO_URL = f"{BASE_URL}/api/User/DoEditInfo"
USER_POINT_URL = f"{BASE_URL}/api/User/GetUserPoint"
BBS_POSTING_URL = f"{BASE_URL}/api/Bbs/Posting"
BBS_REPLY_URL = f"{BASE_URL}/api/Bbs/PostReply"
BBS_POSTING_LIST_URL = (
    f"{BASE_URL}/api/Bbs/GetPostingList?page=1&size=20"
    "&moduleId=09539c50-6de2-4a0c-adc8-535e488a419e&phone="
)
ACTIVITY_LIST_URL = f"{BASE_URL}/api/Activity/GetPageList?Page=1&Limit=20"
LUCKY_DRAW_INFO_URL = f"{BASE_URL}/api/LuckyDraw/GetLuckyDraw?Key="
LUCKY_DRAW_DO_URL = f"{BASE_URL}/api/LuckyDraw/Do"

HITOKOTO_URL = "https://v1.hitokoto.cn/?c=a&c=b&c=c&c=d&c=e&c=f&c=i&c=j&c=k&c=l&min_length=5"
LOCAL_CONTENTS = [
    "膜拜大大大神！！",
    "果断Mark！！！",
    "看帖看完了至少要顶一下！",
    "前排占座，学习了！",
    "每天一顶，心情好好",
    "内容引起极度舒适，已收藏。",
    "潜水多年，看到这个帖子我决定浮出水面点个赞。",
    "楼主辛苦了，感谢！！",
]

SIGN_APP_ID = "815d8026-9a52-4445-a42c-a5443134232e"
SIGN_APP_SECRET = "2b5c01fb-7640-401a-8188-43a13190a626"
SIGN_APP_SECRET_B64 = "MmI1YzAxZmItNzY0MC00MDFhLTgxODgtNDNhMTMxOTBhNjI2"
MODULE_ID = "09539c50-6de2-4a0c-adc8-535e488a419e"

# token 轮换：响应头 access-token / x-access-token 会滚动更新
ROTATED_AUTH = {"token": "", "refreshToken": ""}

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dcsccookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781() "
    "NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat()XWEB/1"
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
    return resp.get("Data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🌈 七彩虹商城动态 code 版                    ║")
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


def make_sign_headers() -> Dict[str, str]:
    """七彩虹接口签名头：Sign = md5(JSON(AppId)+JSON(Ticks)+JSON(requestId)+JSON(AppSecret))"""
    ticks = int(time.time() * 1000)
    request_id = str(uuid.uuid4())
    raw = "".join([
        json.dumps({"AppId": SIGN_APP_ID}, separators=(",", ":")),
        json.dumps({"Ticks": ticks}, separators=(",", ":")),
        json.dumps({"requestId": request_id}, separators=(",", ":")),
        json.dumps({"AppSecret": SIGN_APP_SECRET}, separators=(",", ":")),
    ])
    sign = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return {
        "AppId": SIGN_APP_ID,
        "Ticks": str(ticks),
        "AppSecret": SIGN_APP_SECRET_B64,
        "requestId": request_id,
        "Sign": sign,
    }


def parse_auth(token: str | None) -> Tuple[str, str]:
    """token 缓存值为 JSON 串（token/refreshToken），兼容旧格式裸 token"""
    if not token:
        return "", ""
    try:
        obj = json.loads(token)
        if isinstance(obj, dict):
            return str(obj.get("token") or ""), str(obj.get("refreshToken") or "")
    except Exception:
        pass
    return token, ""


def common_headers(token: str | None = None) -> Dict[str, str]:
    if ROTATED_AUTH.get("token"):
        access, refresh = ROTATED_AUTH["token"], ROTATED_AUTH["refreshToken"]
    else:
        access, refresh = parse_auth(token)

    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "version": "2.0.0",
        "User-from": "xcx",
        "source": "Wx",
        "xweb_xhr": "1",
        "UcSource": "30",
        "Referer": f"https://servicewechat.com/{APPID}/61/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    headers.update(make_sign_headers())
    if access:
        headers["Authorization"] = f"Bearer {access}"
    if refresh:
        headers["X-Authorization"] = f"Bearer {refresh}"
    return headers


def extract_token(data: Any) -> Dict[str, str]:
    """从登录响应中提取 token/refreshToken（Data.Token / Data.RefreshToken）"""
    if not isinstance(data, dict):
        return {}

    inner = data.get("Data") if isinstance(data.get("Data"), dict) else data

    token = inner.get("Token") or inner.get("token")
    refresh = inner.get("RefreshToken") or inner.get("refreshToken")

    if token and str(token) != "null":
        return {"token": str(token), "refreshToken": str(refresh or "")}
    return {}


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """OnLogin 换 OpenId（顺带 Token）；仅凭 code 拿不到 Token 时用 COLORFUL_BODY 兜底"""
    try:
        print("🔐 [登录] 使用 code 换 OpenId（/api/User/OnLogin）")
        response = request_with_proxy(
            "POST",
            LOGIN_ON_URL,
            headers=common_headers(None),
            json={"Code": code},
            proxies=proxies,
            server=server,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        open_id = safe_data(data).get("OpenId") or ""
        auth = extract_token(data)
        if auth.get("token"):
            print(f"✅ [登录] token 获取成功: {mask(auth['token'])}")
            ROTATED_AUTH.clear()
            ROTATED_AUTH.update(auth)
            return json.dumps(auth, ensure_ascii=False), data

        if not open_id:
            print(f"❌ [登录] OnLogin 未返回 OpenId: {json_preview(data)}")
            return None, data

        print(f"✅ [登录] OpenId 获取成功: {mask(open_id)}，业务 Token 需手机号授权")

        if not COLORFUL_BODY:
            print("⚠️ [登录] 未配置 COLORFUL_BODY（抓包 DecryptPhoneNumber 请求体），无法自动首登")
            return None, data

        try:
            body = json.loads(COLORFUL_BODY)
        except Exception:
            print("❌ [登录] COLORFUL_BODY 不是合法 JSON")
            return None, data
        if not body.get("OpenId"):
            body["OpenId"] = open_id

        print("🔐 [登录] 使用抓包 body 进行手机号授权首登（/api/User/DecryptPhoneNumber）")
        response = request_with_proxy(
            "POST",
            LOGIN_PHONE_URL,
            headers=common_headers(None),
            json=body,
            proxies=proxies,
            server=server,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        auth = extract_token(data)
        if auth.get("token"):
            print(f"✅ [登录] token 获取成功: {mask(auth['token'])}")
            ROTATED_AUTH.clear()
            ROTATED_AUTH.update(auth)
            return json.dumps(auth, ensure_ascii=False), data

        print(f"❌ [登录] 未获取到 Token: {json_preview(data)}")
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
    rotate_auth(response)
    if response.status_code in (401, 400):
        return {"Code": 401, "Message": f"HTTP {response.status_code}"}
    try:
        return response.json()
    except Exception:
        return {
            "Code": -1,
            "Message": f"JSON解析失败: {response.text[:300]}",
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
    rotate_auth(response)
    if response.status_code in (401, 400):
        return {"Code": 401, "Message": f"HTTP {response.status_code}"}
    try:
        return response.json()
    except Exception:
        return {
            "Code": -1,
            "Message": f"JSON解析失败: {response.text[:300]}",
        }


def rotate_auth(response: requests.Response) -> None:
    """token 会随响应头轮换（access-token / x-access-token）"""
    try:
        access = response.headers.get("access-token")
        refresh = response.headers.get("x-access-token")
        if access:
            ROTATED_AUTH["token"] = access
        if refresh:
            ROTATED_AUTH["refreshToken"] = refresh
    except Exception:
        pass


def hitokoto() -> str:
    """取一言接口随机句子，失败用本地文案兜底"""
    try:
        response = direct_session().get(HITOKOTO_URL, timeout=10)
        data = response.json()
        if data.get("hitokoto"):
            return data["hitokoto"]
    except Exception:
        pass
    return random.choice(LOCAL_CONTENTS)


def random_birthday() -> str:
    start = datetime(1980, 1, 1).timestamp()
    end = datetime(2005, 12, 31).timestamp()
    return datetime.fromtimestamp(start + random.random() * (end - start)).strftime("%Y-%m-%d")


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
    """优先使用缓存 token（RefreshLoginTime 接口验证），失效自动 code 刷新"""
    ROTATED_AUTH.clear()

    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            refresh_resp = api_post(server, REFRESH_LOGIN_URL, cache_token, proxies, {"phone": ""})
            if refresh_resp.get("Code") == 0:
                print("✅ [缓存] token 有效")
                ROTATED_AUTH.clear()
                access, refresh = parse_auth(cache_token)
                ROTATED_AUTH.update({"token": access, "refreshToken": refresh})
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
        inner = raw_login.get("Data")
        if isinstance(inner, dict):
            expire_time = inner.get("expireTime") or inner.get("expire_time")
            expires_in = inner.get("expiresIn")
            if not expire_time and isinstance(expires_in, (int, float)) and expires_in > 0:
                expire_time = datetime.fromtimestamp(time.time() + expires_in).isoformat()
    if not expire_time:
        expire_time = datetime.fromtimestamp(time.time() + 7 * 24 * 3600).isoformat()
    elif not isinstance(expire_time, str):
        expire_time = datetime.fromtimestamp(expire_time / 1000).isoformat()
    set_cached_token(server, token, expire_time)
    return token, raw_login


def do_comment_tasks(server: str, token: str, proxies: Dict[str, str] | None, phone: str) -> List[str]:
    """社区内容评论：随机取帖随机评 3 次"""
    logs: List[str] = []
    posting_resp = api_get(server, BBS_POSTING_LIST_URL, token, proxies)
    post_list = safe_data(posting_resp).get("DataList")
    if not isinstance(post_list, list) or not post_list:
        logs.append("评论失败: 没有可评论的帖子")
        return logs

    for i in range(3):
        content = hitokoto()
        post = random.choice(post_list)
        payload = {
            "PostId": post.get("Id") if isinstance(post, dict) else "",
            "ReplyId": "",
            "ParentReplyId": "",
            "Phone": phone,
            "Content": content,
            "Pictures": [],
        }
        resp = api_post(server, BBS_REPLY_URL, token, proxies, payload)
        if resp.get("Code") == 0:
            logs.append(f"第{i + 1}次评论成功")
        else:
            logs.append(f"第{i + 1}次评论失败: {resp.get('Message') or json_preview(resp, 120)}")
        wait_time = random.randint(10, 15)
        print(f"⏳ [评论] 等待 {wait_time}s")
        sleep(wait_time)
    return logs


def run_lottery(server: str, token: str, proxies: Dict[str, str] | None) -> List[str]:
    """大转盘抽奖：消耗全部剩余次数"""
    logs: List[str] = []
    activity_resp = api_get(server, ACTIVITY_LIST_URL, token, proxies)
    activities = safe_data(activity_resp).get("DataList")
    if not isinstance(activities, list):
        logs.append("获取活动列表失败")
        return logs

    for activity in activities:
        if not isinstance(activity, dict):
            continue
        if activity.get("Type") == 1 and activity.get("Status") == 1:
            key = activity.get("ActivityKey")
            info = api_get(server, LUCKY_DRAW_INFO_URL + quote(str(key)), token, proxies)
            residue = int(to_float(safe_data(info).get("ResidueCount", 0)))
            if residue <= 0:
                continue
            print(f"🎰 [抽奖] 活动【{activity.get('Name')}】可抽奖 {residue} 次")
            for _ in range(residue):
                draw = api_post(server, LUCKY_DRAW_DO_URL, token, proxies, {"key": key})
                if draw.get("Code") == 0:
                    logs.append(f"{activity.get('Name')}: 抽奖成功")
                    print(f"✅ [抽奖] {json_preview(draw, 200)}")
                else:
                    logs.append(f"{activity.get('Name')}: {draw.get('Message') or '抽奖失败'}")
                    print(f"⚠️ [抽奖] {json_preview(draw, 200)}")
                sleep(2)
    if not logs:
        logs.append("暂无可参加的抽奖活动")
    return logs


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "今日已签到或无签到任务",
        "taskMsg": "-",
        "lotteryMsg": "-",
        "point": "-",
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
        user_resp = api_get(server, USER_INFO_URL, token, proxies)
        if user_resp.get("Code") == 401:
            result["error"] = "token 已过期，请重新获取"
            print(f"❌ [账号] {result['error']}")
            return result
        nick_name = safe_data(user_resp).get("NickName", "")
        mobile = safe_data(user_resp).get("Mobile", "")
        print(f"👤 [会员] {nick_name} 开始任务")

        task_logs: List[str] = []

        # 积分任务列表驱动
        config_resp = api_get(server, POINT_CONFIG_URL, token, proxies)
        if config_resp.get("Code") == 401:
            result["error"] = "token 已过期，请重新获取"
            print(f"❌ [账号] {result['error']}")
            return result
        task_list = safe_data(config_resp).get("DataList") or []
        for point_task in task_list:
            if not isinstance(point_task, dict):
                continue
            name = point_task.get("Name")
            link_type = point_task.get("LinkType")
            done_total = point_task.get("DayGetPointTotal")
            if (point_task.get("DayMaxPointTotal") == done_total and link_type == 1) or (
                point_task.get("PerPoint") == done_total and link_type == 2
            ):
                continue

            if name == "社区签到":
                sign_resp = api_post(server, SIGN_URL, token, proxies, {})
                if sign_resp.get("Code") == 0:
                    gain = safe_data(sign_resp).get("Point")
                    result["signMsg"] = f"社区签到成功(积分+{gain})"
                    task_logs.append(result["signMsg"])
                    print(f"✅ [签到] {result['signMsg']}")
                else:
                    msg = sign_resp.get("Message") or "签到失败"
                    result["signMsg"] = f"社区签到: {msg}"
                    task_logs.append(result["signMsg"])
                    print(f"⚠️ [签到] {result['signMsg']}")
                # 新版签到（aux 并集）
                sign_v2 = api_post(server, SIGN_V2_URL, token, proxies, {})
                if sign_v2.get("Code") == 0:
                    task_logs.append("新版签到成功")
                    print("✅ [签到] 新版签到成功")
                else:
                    print(f"⚠️ [签到] 新版签到: {sign_v2.get('Message') or '失败'}")
            elif name == "会员信息完善":
                api_get(server, EDIT_INFO_URL, token, proxies)
                edit_resp = api_post(server, DO_EDIT_INFO_URL, token, proxies, {
                    "Birthday": random_birthday(),
                    "Nickname": nick_name,
                    "Sex": 1,
                })
                if edit_resp.get("Code") == 0:
                    task_logs.append("会员信息完善成功")
                    print("✅ [完善] 会员信息完善成功")
                else:
                    msg = edit_resp.get("Message") or "失败"
                    task_logs.append(f"会员信息完善: {msg}")
                    print(f"⚠️ [完善] 会员信息完善: {msg}")
            elif name == "购物有礼":
                continue
            elif name == "社区内容发布":
                content = hitokoto()
                posting = api_post(server, BBS_POSTING_URL, token, proxies, {
                    "ModuleId": MODULE_ID,
                    "Phone": mobile,
                    "Title": "签到",
                    "Content": content,
                    "Pictures": [],
                    "Source": 30,
                })
                if posting.get("Code") == 0:
                    task_logs.append("社区内容发布成功")
                    print(f"✅ [发布] 社区内容发布成功：{content}")
                else:
                    msg = posting.get("Message") or "发布失败"
                    task_logs.append(f"社区内容发布: {msg}")
                    print(f"⚠️ [发布] 社区内容发布: {msg}")
                wait_time = random.randint(10, 15)
                print(f"⏳ [发布] 等待 {wait_time}s")
                sleep(wait_time)
            elif name == "社区内容评论":
                task_logs.extend(do_comment_tasks(server, token, proxies, mobile))
            else:
                print(f"⚠️ [任务] {name} 未实现")
        result["taskMsg"] = "；".join(task_logs) if task_logs else "今日任务均已完成"

        # 可选抽奖
        if RAFFLE_ENABLED:
            lottery_logs = run_lottery(server, token, proxies)
            result["lotteryMsg"] = "；".join(lottery_logs)
        else:
            result["lotteryMsg"] = "未开启抽奖（COLORFUL_RAFFLE=true 开启）"

        # 查询积分
        user_resp = api_get(server, USER_INFO_URL, token, proxies)
        point = safe_data(user_resp).get("Point")
        result["point"] = str(point) if point is not None else "-"
        print(f"💰 [积分] 拥有积分: {result['point']}")
        days_resp = api_get(server, SIGN_DAYS_URL, token, proxies)
        days_value = days_resp.get("Data")
        if isinstance(days_value, (int, str)) and days_value not in ("", None):
            print(f"📅 [签到] 已连续签到 {days_value} 天")
        point_num_resp = api_get(server, USER_POINT_URL, token, proxies)
        if point_num_resp.get("Code") == 0 and isinstance(safe_data(point_num_resp).get("Num"), (int, str)):
            print(f"💰 [积分] 当前积分: {safe_data(point_num_resp).get('Num')}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🌈 七彩虹商城任务结果

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
📋 任务：{res["taskMsg"]}
🎰 抽奖：{res["lotteryMsg"]}
💰 积分：{res["point"]}
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
                "taskMsg": "-",
                "lotteryMsg": "-",
                "point": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 七彩虹商城任务执行完成                    ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🌈 七彩虹商城任务完成", build_notify(results))


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
