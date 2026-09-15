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
古井贡酒会员中心小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. 登录接口使用 code 换 Access-Token（推断接口，登录后查 /member/info 补齐 memberId）
  3. 每日登录奖励（/login/info）
  4. 每日签到（sign:search 查状态 + sign:join 执行签到）
  5. 幸运抽奖（lucky:search 查次数 + lucky:join 逐次抽奖）
  6. 天降红包（red_packet:join）
  7. 查询会员积分（/member/info getPoint）
  8. PushPlus 推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       覆盖本地 code 服务地址，可选

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]

⚠️ 登录接口为推断，未经真机验证，失败请抓包核对
   （源脚本为抓包 Access-Token + memberId 型，未含登录调用；
   参考同平台（爱玛 scrm）脚本亦为抓包型，登录端点 /login 为最可能实现；
   业务请求头 Access-Token 需带 "Bearer " 前缀与否以抓包为准）
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


APP_NAME = "古井贡酒会员中心小程序"
APPID = "wxba9855bdb1a45c8e"

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

BASE_URL = "https://scrm.gujing.com/gujing_scrm/wxclient"
LOGIN_URL = f"{BASE_URL}/login"  # ⚠️ 推断接口
LOGIN_INFO_URL = f"{BASE_URL}/login/info"
SIGN_SEARCH_URL = f"{BASE_URL}/mkt/activities/sign:search"
SIGN_JOIN_URL = f"{BASE_URL}/mkt/activities/sign:join"
LUCKY_SEARCH_URL = f"{BASE_URL}/mkt/activities/lucky:search"
LUCKY_JOIN_URL = f"{BASE_URL}/mkt/activities/lucky:join"
RED_PACKET_URL = f"{BASE_URL}/mkt/activities/red_packet:join"
MEMBER_INFO_URL = f"{BASE_URL}/member/info"

# 活动参数（照源脚本原值）
SIGN_ACTIVITY_ID = "110001000"
LUCKY_ACTIVITY_ID = "110000525"
RED_PACKET_ACTIVITY_ID = "110000555"
LUCKY_LATITUDE = 32.310428619384766
LUCKY_LONGITUDE = 118.34776306152344
RED_LATITUDE = 32.3110466003418
RED_LONGITUDE = 118.34707641601562

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gujingcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.47(0x18002f2c) "
    "NetType/4G Language/zh_CN miniProgram/wxba9855bdb1a45c8e"
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
    print("║ 🍶 古井贡酒会员中心小程序动态 code 版         ║")
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
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=utf-8",
        "Origin": "https://scrm.gujing.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{APPID}/183/page-frame.html",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
    }
    if token:
        headers["Access-Token"] = token
    return headers


def extract_token(data: Any) -> str | None:
    """从登录响应中提取 Access-Token（推断登录接口的 token 字段名按该平台常见命名兼容）"""
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("accessToken"),
        data.get("access_token"),
        data.get("AccessToken"),
        data.get("token"),
        data.get("Authorization"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("AccessToken"),
            inner.get("token"),
            inner.get("Authorization"),
        ])

        content = inner.get("content")
        if isinstance(content, dict):
            candidates.extend([
                content.get("accessToken"),
                content.get("access_token"),
                content.get("token"),
            ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def extract_member_id(data: Any) -> str | None:
    """从会员信息响应中提取 memberId（content.vipMemberPointDTO.memberId，照源脚本 getCookie 逻辑）"""
    if not isinstance(data, dict):
        return None

    content = data.get("content")
    if isinstance(content, dict):
        member = content.get("vipMemberPointDTO")
        if isinstance(member, dict) and member.get("memberId"):
            return str(member["memberId"])

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 Access-Token（推断接口）")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json={"code": code, "belongToId": 1},
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


def set_cached_token(server: str, token: str, expire_time: str, member_id: str | None = None) -> None:
    cache = load_token_cache()
    entry = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    if member_id:
        entry["memberId"] = member_id
    cache[server] = entry
    save_token_cache(cache)


def get_cached_member_id(server: str) -> str | None:
    """读取缓存中的 memberId（会员账号身份标识，登录接口不返回时需 /member/info 补齐）"""
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("memberId"):
        return str(data["memberId"])
    return None


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（会员信息接口验证），失效自动 code 刷新；返回 (token, memberId, 会员信息原始响应)"""
    member_id = get_cached_member_id(server)
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            info_resp = api_get(server, MEMBER_INFO_URL, cache_token, proxies)
            if info_resp.get("code") == 200:
                print("✅ [缓存] token 有效")
                if not member_id:
                    member_id = extract_member_id(info_resp)
                    if member_id:
                        set_cached_token(server, cache_token, _cached_expire_time(server), member_id)
                return cache_token, member_id, info_resp
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None, None

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, None, raw_login

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

    # 登录后查询会员信息，补齐 memberId 并缓存
    if not member_id:
        try:
            info_resp = api_get(server, MEMBER_INFO_URL, token, proxies)
            if info_resp.get("code") == 200:
                member_id = extract_member_id(info_resp)
        except Exception as exc:
            print(f"⚠️ [登录] 查询会员信息异常: {exc}")
            info_resp = None
    set_cached_token(server, token, expire_time, member_id)
    return token, member_id, None


def _cached_expire_time(server: str) -> str:
    cache = load_token_cache()
    data = cache.get(server) or {}
    return data.get("expireTime") or datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()


def daily_login_reward(server: str, token: str, member_id: str, proxies: Dict[str, str] | None) -> str:
    """每日登录奖励 /login/info（源脚本用它判断 token 是否过期）"""
    resp = api_post(server, LOGIN_INFO_URL, token, proxies, {"belongToId": 1, "memberId": member_id})
    if resp.get("code") == 200:
        message = f"每日登录奖励: {resp.get('chnDesc') or '成功'}"
        print(f"✅ [登录奖励] {message}")
        return message
    message = f"token 已过期或登录奖励失败: {resp.get('chnDesc') or resp.get('msg') or json_preview(resp, 200)}"
    print(f"❌ [登录奖励] {message}")
    return message


def daily_sign(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """每日签到：sign:search 查状态，未签到则 sign:join（取主源与辅助源逻辑并集）"""
    search_resp = api_post(server, SIGN_SEARCH_URL, token, proxies, {"activityId": SIGN_ACTIVITY_ID, "preview": False})
    if search_resp.get("code") != 200:
        message = f"签到状态查询失败: {search_resp.get('chnDesc') or search_resp.get('msg') or json_preview(search_resp, 200)}"
        print(f"⚠️ [签到] {message}")
        return message

    if (search_resp.get("content") or {}).get("signed") == 1:
        print("✅ [签到] 今日已签到，无需重复签到")
        return "今日已签到"

    join_resp = api_post(server, SIGN_JOIN_URL, token, proxies, {"activityId": SIGN_ACTIVITY_ID, "preview": False})
    if join_resp.get("code") == 200:
        point = (join_resp.get("content") or {}).get("point")
        message = f"签到成功！获取积分 {point}" if point else "签到成功"
        print(f"✅ [签到] {message}")
        return message

    message = f"签到失败: {join_resp.get('chnDesc') or join_resp.get('msg') or json_preview(join_resp, 200)}"
    print(f"❌ [签到] {message}")
    return message


def lucky_draw(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """幸运抽奖：lucky:search 查免费次数，lucky:join 逐次抽奖"""
    body = {
        "verificationCode": "",
        "activityId": LUCKY_ACTIVITY_ID,
        "preview": False,
        "latitude": LUCKY_LATITUDE,
        "longitude": LUCKY_LONGITUDE,
    }
    search_resp = api_post(server, LUCKY_SEARCH_URL, token, proxies, body)
    if search_resp.get("code") != 200:
        message = f"抽奖信息查询失败: {search_resp.get('chnDesc') or search_resp.get('msg') or json_preview(search_resp, 200)}"
        print(f"⚠️ [抽奖] {message}")
        return message

    times = int((search_resp.get("content") or {}).get("availableJoinTimesDTO", {}).get("freeAvailableJoinTimes", 0) or 0)
    if times <= 0:
        print("⚠️ [抽奖] 今日抽奖次数已用完")
        return "今日抽奖次数已用完"

    prizes: List[str] = []
    for draw_index in range(1, times + 1):
        print(f"⏳ [抽奖] 第 {draw_index} 次抽奖前等待 5s")
        sleep(5)
        draw_resp = api_post(server, LUCKY_JOIN_URL, token, proxies, body)
        if draw_resp.get("code") != 200:
            message = f"第{draw_index}次抽奖失败: {draw_resp.get('chnDesc') or draw_resp.get('msg') or json_preview(draw_resp, 200)}"
            prizes.append(message)
            print(f"❌ [抽奖] {message}")
            continue

        content = draw_resp.get("content") or []
        name = "未知奖品"
        if isinstance(content, list) and content:
            name = str((content[0] or {}).get("actAwardName", "未知奖品"))
        prizes.append(name)
        if "积分" in name or "谢谢惠顾" in name:
            print(f"🎰 [抽奖] 第 {draw_index} 次: {name}")
        else:
            print(f"🎉 [抽奖] 第 {draw_index} 次获得: {name}")

    result = "、".join(prizes)
    print(f"🎰 [抽奖] 共抽 {times} 次: {result}")
    return result


def red_packet_join(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """天降红包：red_packet:join"""
    body = {
        "verificationCode": "",
        "activityId": RED_PACKET_ACTIVITY_ID,
        "redpackCnt": 1000,
        "preview": False,
        "latitude": RED_LATITUDE,
        "longitude": RED_LONGITUDE,
    }
    resp = api_post(server, RED_PACKET_URL, token, proxies, body)
    if resp.get("code") != 200:
        message = f"天降红包失败: {resp.get('chnDesc') or resp.get('msg') or json_preview(resp, 200)}"
        print(f"⚠️ [红包] {message}")
        return message

    awards = resp.get("content") or []
    prizes: List[str] = []
    for act_award in awards if isinstance(awards, list) else []:
        if not isinstance(act_award, dict):
            continue
        name = str(act_award.get("actAwardName", "未知奖品"))
        prizes.append(name)
        if "积分" in name or "谢谢惠顾" in name:
            print(f"🧧 [红包] {name}")
        else:
            print(f"🎉 [红包] 获得: {name}")

    result = "、".join(prizes) if prizes else "无"
    print(f"🧧 [红包] 天降红包结果: {result}")
    return result


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "loginRewardMsg": "-",
        "signMsg": "-",
        "lotteryMsg": "-",
        "redPacketMsg": "-",
        "points": "-",
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

    token, member_id, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        if not member_id:
            result["error"] = "未获取到 memberId，登录可能失败或需抓包核对"
            return result

        result["loginRewardMsg"] = daily_login_reward(server, token, member_id, proxies)

        print("⏳ [间隔] 等待 5s 后开始签到")
        sleep(5)

        result["signMsg"] = daily_sign(server, token, proxies)

        print("⏳ [间隔] 等待 5s 后开始抽奖")
        sleep(5)

        result["lotteryMsg"] = lucky_draw(server, token, proxies)

        print("🧧 [红包] 开始天降红包")
        result["redPacketMsg"] = red_packet_join(server, token, proxies)

        print("⏳ [间隔] 等待 5s 后查询会员信息")
        sleep(5)

        info_resp = api_get(server, MEMBER_INFO_URL, token, proxies)
        if info_resp.get("code") != 200:
            message = f"获取会员信息失败: {info_resp.get('chnDesc') or info_resp.get('msg') or json_preview(info_resp, 200)}"
            print(f"⚠️ [积分] {message}")
            result["points"] = "-"
        else:
            content = info_resp.get("content") or {}
            point = (content.get("vipMemberPointDTO") or {}).get("getPoint", "-")
            result["points"] = str(point)
            print(f"⭐ [积分] 拥有积分: {point}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍶 古井贡酒会员中心任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🎁 登录奖励：{res["loginRewardMsg"]}
📝 签到：{res["signMsg"]}
🎰 抽奖：{res["lotteryMsg"]}
🧧 红包：{res["redPacketMsg"]}
⭐ 积分：{res["points"]}
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
                "loginRewardMsg": "-",
                "signMsg": "-",
                "lotteryMsg": "-",
                "redPacketMsg": "-",
                "points": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 古井贡酒任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🍶 古井贡酒任务完成", build_notify(results))


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
