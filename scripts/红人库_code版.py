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
红人库签到动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. xapi.weimob.com /fe/mapi/user/loginX 使用 code 换 token（X-WX-Token）与 wid
  3. 读取签到状态（onecrm signMainInfo，data.hasSign）
  4. 每日签到（onecrm sign，重复签 errcode 60070013000332 视为已签）
  5. 查询积分（point myPoint getSimpleAccountInfo，availablePoint）
  6. Token 本地缓存与自动刷新
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

说明：
  vid/bosId/cid/merchantId/productInstanceId/productVersionId 等是这家小程序
  绑定的固定商户配置（原脚本硬编码，非个人凭证）；wid 由 code 登录动态返回。
  业务请求需带微盟网关路由头（x-wmsdk-vid/x-biz-id/x-component-is/cloud-bosid/
  weimob-bosId/weimob-cid），否则网关无法定位 vid 对应的 OneCRM 签到实例。
  每个账号对应一个本地 code 服务端口（SERVERS 一项即一个账号）。

环境变量：
  PLUSPLUS_TOKEN  PushPlus token，可选
  QYWX_TOKEN      企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API       品赞代理提取 API，可选
  PROXY_TYPE      http / socks5，默认 http

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
from urllib.parse import quote

import requests


APP_NAME = "红人库签到小程序"
APPID = "wx44356198837c0121"

SERVERS = ["127.0.0.1:8088"]

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

BASE_URL = "https://xapi.weimob.com"
LOGIN_URL = f"{BASE_URL}/fe/mapi/user/loginX"
SIGN_STATUS_URL = f"{BASE_URL}/api3/onecrm/mactivity/sign/misc/sign/activity/c/signMainInfo"
SIGN_TAKE_URL = f"{BASE_URL}/api3/onecrm/mactivity/sign/misc/sign/activity/core/c/sign"
POINT_URL = f"{BASE_URL}/api3/onecrm/point/myPoint/getSimpleAccountInfo"

# —— 这家小程序绑定的固定商户配置（原脚本硬编码常量，非个人凭证）——
WEIMOB = {
    "bosId": "4020386662105",
    "cid": "114397105",
    "vid": 6015049204105,
    "vidType": 2,
    "productId": 146,
    "productInstanceId": 1021082105,
    "productVersionId": "10003",
    "merchantId": 2000027614105,
    "tcode": "weimob",
}

SIGN_BASIC_INFO = {
    "vid": WEIMOB["vid"],
    "vidType": WEIMOB["vidType"],
    "bosId": int(WEIMOB["bosId"]),
    "productId": WEIMOB["productId"],
    "productInstanceId": WEIMOB["productInstanceId"],
    "productVersionId": WEIMOB["productVersionId"],
    "merchantId": WEIMOB["merchantId"],
    "tcode": WEIMOB["tcode"],
    "cid": int(WEIMOB["cid"]),
}

# 积分接口用的 basicInfo（productId 1 / usercenter 实例）
POINT_BASIC_INFO = {
    "vid": WEIMOB["vid"],
    "vidType": WEIMOB["vidType"],
    "bosId": int(WEIMOB["bosId"]),
    "productId": 1,
    "productInstanceId": 1021218105,
    "productVersionId": "36000",
    "merchantId": WEIMOB["merchantId"],
    "tcode": WEIMOB["tcode"],
    "cid": int(WEIMOB["cid"]),
}

SIGN_EXTEND_INFO = {
    "wxTemplateId": 7912,
    "analysis": [],
    "bosTemplateId": 1000001980,
    "childTemplateIds": [
        {"customId": 90004, "version": "crm@0.1.62"},
        {"customId": 90002, "version": "ec@67.1"},
        {"customId": 90006, "version": "hudong@0.0.227"},
        {"customId": 90008, "version": "cms@0.0.503"},
    ],
    "quickdeliver": {"enable": "false"},
    "youshu": {"enable": "false"},
    "source": 1,
    "channelsource": 5,
    "refer": "onecrm-signgift",
    "mpScene": 1302,
}

# 业务请求需带这组微盟网关路由头，否则网关无法定位 vid 对应的 OneCRM 签到实例
MERCHANT_HEADERS = {
    "x-wmsdk-vid": str(WEIMOB["vid"]),
    "x-biz-id": str(WEIMOB["productId"]),
    "x-component-is": "onecrm/signgift",
    "cloud-bosid": WEIMOB["bosId"],
    "weimob-bosId": WEIMOB["bosId"],
    "weimob-cid": WEIMOB["cid"],
}

# 登录只需商户 bos/cid 头
LOGIN_HEADERS = {
    "weimob-bosId": WEIMOB["bosId"],
    "weimob-cid": WEIMOB["cid"],
}

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rrkcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 "
    "Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF"
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
    print("║ 🌟 红人库签到动态 code 版                    ║")
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


def common_headers(token: str | None = None, extra: Dict[str, str] | None = None) -> Dict[str, str]:
    headers = {
        "Host": "xapi.weimob.com",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Referer": f"https://servicewechat.com/{APPID}/93/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["X-WX-Token"] = token
    if extra:
        headers.update(extra)
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

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        body = {
            "appid": APPID,
            "basicInfo": {"bosId": WEIMOB["bosId"], "cid": WEIMOB["cid"], "tcode": "weimob", "vid": str(WEIMOB["vid"])},
            "env": "production",
            "extendInfo": {"source": 1},
            "is_pre_fetch_open": True,
            "parentVid": 0,
            "pid": WEIMOB["bosId"],
            "storeId": "0",
            "code": code,
            "queryAuthConfig": True,
        }
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(None, LOGIN_HEADERS),
            json=body,
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        token = extract_token(data)
        if int(data.get("errcode") or -1) == 0 and token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_post(
    server: str,
    url: str,
    token: str,
    proxies: Dict[str, str] | None,
    payload: Dict[str, Any],
    extra_headers: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token, extra_headers),
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "errcode": -1,
            "errmsg": f"JSON解析失败: {response.text[:300]}",
        }


def is_ok(resp: Dict[str, Any]) -> bool:
    try:
        return int(resp.get("errcode") or -1) == 0
    except (TypeError, ValueError):
        return False


def msg_of(resp: Dict[str, Any]) -> str:
    return resp.get("errmsg") or resp.get("msg") or json_preview(resp, 200)


def is_already_done(text: Any) -> bool:
    return bool(re.search(r"已签|已经签|签到过|重复|已完成|60070013000332|already", str(text or ""), re.I))


def is_auth_error(resp: Dict[str, Any]) -> bool:
    return bool(re.search(r"登录|token|未授权|失效|过期|未登录|1041|401|403", msg_of(resp), re.I))


def need_member(text: Any) -> bool:
    return bool(re.search(r"注册|未激活|会员|绑定|授权", str(text or "")))


def is_activity_closed(text: Any) -> bool:
    # 活动时间窗口相关（已结束/未开始/下线/不存在/无在用实例）
    return bool(re.search(
        r"活动未开始|活动已结束|活动不存在|活动已下线|活动未开启|活动已过期|活动结束|在用的OneCRM实例|OneCRM实例为空|60070012100039",
        str(text or ""),
    ))


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


def get_cached_wid(server: str) -> str:
    cache = load_token_cache()
    return str((cache.get(server) or {}).get("wid") or "")


def set_cached_token(server: str, token: str, expire_time: str, wid: str = "") -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "wid": wid, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def clear_cached_token(server: str) -> None:
    cache = load_token_cache()
    if server in cache:
        del cache[server]
        save_token_cache(cache)


def biz_body(wid: str) -> Dict[str, Any]:
    return {
        "appid": APPID,
        "basicInfo": dict(SIGN_BASIC_INFO),
        "extendInfo": dict(SIGN_EXTEND_INFO),
        "queryParameter": "null",
        "i18n": {"language": "zh", "timezone": "8"},
        "pid": "",
        "storeId": "",
        "customInfo": {"source": 0, "wid": wid},
    }


def check_signed(server: str, token: str, wid: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    resp = api_post(server, SIGN_STATUS_URL, token, proxies, biz_body(wid), MERCHANT_HEADERS)
    if is_ok(resp):
        d = safe_data(resp)
        return {
            "ok": True,
            "signed": d.get("hasSign") is True or d.get("isSign") is True,
            "days": d.get("activityCumulativeSignDays") if d.get("activityCumulativeSignDays") is not None else (
                d.get("monthCumulativeSignDays") if d.get("monthCumulativeSignDays") is not None else d.get("signedDate")
            ),
        }
    return {"ok": False, "res": resp}


def query_points(server: str, token: str, wid: str, proxies: Dict[str, str] | None) -> str:
    """积分为附带信息，失败不影响签到结论"""
    try:
        body = {
            "appid": APPID,
            "basicInfo": dict(POINT_BASIC_INFO),
            "extendInfo": {**SIGN_EXTEND_INFO, "refer": "cms-usercenter"},
            "queryParameter": "null",
            "i18n": {"language": "zh", "timezone": "8"},
            "pid": "",
            "storeId": "",
            "targetBasicInfo": {"productInstanceId": WEIMOB["productInstanceId"]},
            "request": {},
        }
        resp = api_post(server, POINT_URL, token, proxies, body, MERCHANT_HEADERS)
        if is_ok(resp) and resp.get("data"):
            available = safe_data(resp).get("availablePoint")
            return f"可用 {available if available is not None else 0}"
    except Exception:
        pass
    return "-"


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None, str]:
    """优先使用缓存 token（签到状态接口验证），失效自动 code 刷新；返回 (token, raw_login, wid)"""
    cache_token = get_cached_token(server)
    if cache_token:
        cached_wid = get_cached_wid(server)
        print("🔍 [缓存] 验证 token")
        try:
            st = check_signed(server, cache_token, cached_wid, proxies)
            if st["ok"] or not is_auth_error(st.get("res", {})):
                if st["ok"]:
                    print("✅ [缓存] token 有效")
                    return cache_token, None, cached_wid
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None, ""

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, raw_login, ""

    wid = str((safe_data(raw_login) or {}).get("wid") or "")
    expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    set_cached_token(server, token, expire_time, wid)
    if wid:
        print(f"✅ [登录] wid {wid}")
    return token, raw_login, wid


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
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

    token, raw_login, wid = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    state = {"token": token, "wid": wid}

    def relogin() -> bool:
        try:
            new_token, new_raw, new_wid = login_with_cache(server, proxies)
            if not new_token:
                return False
            state["token"] = new_token
            state["wid"] = new_wid
            return True
        except Exception as exc:
            print(f"❌ [登录] 重新登录异常: {exc}")
            return False

    try:
        def sign_flow(retry: bool = True) -> None:
            st = check_signed(server, state["token"], state["wid"], proxies)
            if st["ok"] and st["signed"]:
                days = st["days"]
                result["signMsg"] = f"今日已签到{f'，累计 {days} 天' if days else ''}"
                print(f"✅ [签到] {result['signMsg']}")
                result["pointsMsg"] = query_points(server, state["token"], state["wid"], proxies)
                if result["pointsMsg"] != "-":
                    print(f"💰 [积分] {result['pointsMsg']}")
                return
            if not st["ok"]:
                resp = st.get("res") or {}
                if is_activity_closed(msg_of(resp)) or is_activity_closed(resp.get("errcode")):
                    result["signMsg"] = f"⚠️ 该商户当前无在用的 OneCRM 签到实例/活动已下线（{msg_of(resp)}），仅签到活动被商户下线或轮换"
                    print(f"⚠️ [签到] {result['signMsg']}")
                    result["pointsMsg"] = query_points(server, state["token"], state["wid"], proxies)
                    return
                if retry and is_auth_error(resp):
                    print("🔁 [签到] 会话失效，重新登录后重试")
                    if relogin():
                        sign_flow(False)
                        return
                    result["error"] = "重新登录失败"
                    print(f"❌ [签到] {result['error']}")
                    return
                if need_member(msg_of(resp)):
                    result["error"] = f"该微信号还没在红人库注册会员（{msg_of(resp)}），先在小程序里注册一次再跑"
                    print(f"⚠️ [账号] {result['error']}")
                    return
                # 查询状态失败但非鉴权/会员问题：继续直接尝试签到
                print(f"⚠️ [签到] 查询签到状态失败（{msg_of(resp)}），直接尝试签到")

            resp = api_post(server, SIGN_TAKE_URL, state["token"], proxies, biz_body(state["wid"]), MERCHANT_HEADERS)
            if is_ok(resp):
                d = safe_data(resp)
                fr = d.get("fixedReward") or {}
                er = d.get("extraReward") or {}
                rewards: List[str] = []
                if to_float(fr.get("points")) > 0:
                    rewards.append(f"{fr.get('points')}{d.get('pointName') or '积分'}")
                if to_float(fr.get("growth")) > 0:
                    rewards.append(f"{fr.get('growth')}{d.get('growthName') or '成长值'}")
                if to_float(fr.get("amount")) > 0:
                    rewards.append(f"{fr.get('amount')}元")
                if to_float(er.get("points")) > 0:
                    rewards.append(f"额外{er.get('points')}{d.get('pointName') or '积分'}")
                rewards_text = "、".join(rewards)
                result["signMsg"] = f"签到成功{f'：{rewards_text}' if rewards else ''}"
                print(f"✅ [签到] {result['signMsg']}")
                result["pointsMsg"] = query_points(server, state["token"], state["wid"], proxies)
                if result["pointsMsg"] != "-":
                    print(f"💰 [积分] {result['pointsMsg']}")
                return
            if is_already_done(msg_of(resp)) or is_already_done(resp.get("errcode")):
                result["signMsg"] = f"今日已签到（{msg_of(resp)}）"
                print(f"✅ [签到] {result['signMsg']}")
                result["pointsMsg"] = query_points(server, state["token"], state["wid"], proxies)
                return
            if is_activity_closed(msg_of(resp)):
                result["signMsg"] = f"⚠️ 签到活动当前未开启/已结束（{msg_of(resp)}），需从小程序重新抓取 productInstanceId/productVersionId 后更新配置"
                print(f"⚠️ [签到] {result['signMsg']}")
                return
            if retry and is_auth_error(resp):
                print("🔁 [签到] 会话失效，重新登录后重试")
                if relogin():
                    sign_flow(False)
                else:
                    result["error"] = "重新登录失败"
                    print(f"❌ [签到] {result['error']}")
                return
            if need_member(msg_of(resp)):
                result["error"] = f"该微信号还没在红人库注册会员（{msg_of(resp)}），先在小程序里注册一次再跑"
                print(f"⚠️ [账号] {result['error']}")
                return
            result["signMsg"] = f"签到失败: {msg_of(resp)}"
            print(f"❌ [签到] {result['signMsg']}")

        sign_flow()
        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🌟 红人库签到任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🌐 代理：{res["proxyStatus"]}（{res["proxyIp"]}）
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
    print("║ 🏁 红人库任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🌟 红人库签到任务完成", build_notify(results))


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
