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
腾讯电脑管家动态 code 版

功能：
  1. 本地 code 服务获取微信 authCode（源脚本走 QRConnect + wx_server 扫码，见下方限制说明）
  2. jprx.m.qq.com/data/3078/forward 使用 authCode 登录换取 loginKey/openid
  3. 构建 _gj_* Cookie
  4. sdi.m.qq.com/public/auth/create 创建 sdi 授权，取 sessionKey
  5. sdi.m.qq.com/private/lottery/doLottery 每日抽奖
  6. 登录资料（loginKey/openid 等）本地缓存复用
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

⚠️ 限制说明：源脚本通过微信开放平台 QRConnect 二维码 + wx_server 扫码换取 authCode，
   该链路无法直接 code 化。本版按最佳推断改为：本地 code 服务按 appId 取码后直接
   作为 authCode 传入登录接口（未经真机验证），若失败请抓包核对，或设置
   QQPCMGR_AUTHCODE 环境变量直接指定 authCode。缓存资料仅校验字段存在性，
   sdi auth/create 失败时会自动清缓存重新登录一次。

环境变量：
  PLUSPLUS_TOKEN     PushPlus token，可选
  QYWX_TOKEN         企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API          品赞代理提取 API，可选
  PROXY_TYPE         http / socks5，默认 http
  QQPCMGR_AUTHCODE   直接使用指定 authCode（设置后不走 code 服务）
  QQPCMGR_GUID       客户端 guid，默认 fff4328476c4ffc836d21e82918faa19
  QQPCMGR_SDIAID     sdi aid，默认 2025121115391911962
  QQPCMGR_VERSION    客户端版本，默认 18.2.30604.301
  QQPCMGR_COMPUTER   电脑名，默认 smallfawn
  QQPCMGR_LID        抽奖活动 id，默认 Lottery2

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


APP_NAME = "腾讯电脑管家"
APPID = "wx5cd60c5d4817a188"

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

QRCONNECT_URL = os.getenv(
    "QQPCMGR_QRCONNECT_URL",
    "https://open.weixin.qq.com/connect/qrconnect?appid=wx5cd60c5d4817a188&scope=snsapi_login"
    "&redirect_uri=https%3A%2F%2Fsecurity.guanjia.qq.com%2Flogin&state=233&login_type=jssdk&self_redirect=true",
)
LOGIN_BY_CODE_URL = "https://jprx.m.qq.com/data/3078/forward"
AUTH_CREATE_URL = "https://sdi.m.qq.com/public/auth/create"
DO_LOTTERY_URL = "https://sdi.m.qq.com/private/lottery/doLottery"

AUTH_CODE = os.getenv("QQPCMGR_AUTHCODE", "")
CLIENT_GUID = os.getenv("QQPCMGR_GUID", "fff4328476c4ffc836d21e82918faa19")
SDIAID = os.getenv("QQPCMGR_SDIAID", "2025121115391911962")
LOTTERY_ID = os.getenv("QQPCMGR_LID", "Lottery2")
VERSION = os.getenv("QQPCMGR_VERSION", "18.2.30604.301")
COMPUTER_NAME = os.getenv("QQPCMGR_COMPUTER", "smallfawn")

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qqpcmgrcookie.json")

USER_AGENT = os.getenv(
    "QQPCMGR_UA",
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36 "
    "Chrome/143.0.13.0 Tencent QQPCMgr/18.2.30604.301",
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
    print("║ 🖥️ 腾讯电脑管家动态 code 版                  ║")
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


def pick(source: Any, keys: List[str]) -> Any:
    if not isinstance(source, dict):
        return ""
    for key in keys:
        value = source.get(key)
        if value is not None and value != "":
            return value
    return ""


def find_login_payload(source: Any) -> Dict[str, Any] | None:
    if not isinstance(source, dict):
        return None
    login_key = pick(source, ["loginKey", "LoginKey", "loginkey", "login_key"])
    third = source.get("thirdPartyAccInfo")
    openid = (
        pick(source, ["openid", "openId", "OpenId", "OpenID"])
        or pick(third, ["bindAccount", "openid", "openId", "OpenId", "OpenID"])
    )
    if login_key and openid:
        return source

    for value in source.values():
        if isinstance(value, dict):
            found = find_login_payload(value)
            if found:
                return found
    return None


def cookie_value(value: Any) -> str:
    return str(value if value is not None else "").replace(";", "").replace("\r", "").replace("\n", "")


def build_cookie(profile: Dict[str, Any]) -> str:
    from urllib.parse import quote as _quote

    commonid = profile.get("commonid") or profile.get("openid")
    encoded_nickname = _quote(str(profile.get("nickname") or ""))
    pairs = {
        "_gj_acc_type": 2,
        "_gj_commonid": commonid,
        "_gj_version": VERSION,
        "_gj_computername": COMPUTER_NAME,
        "_gj_client_guid": profile.get("guid"),
        "_gj_server_guid": profile.get("serverGuid") or "",
        "_gj_vip": profile.get("vip") or 0,
        "_gj_nickname": profile.get("nickname") or "",
        "_gj_accountid": profile.get("account"),
        "_gj_loginkey": profile.get("loginKey"),
        "_gj_openid": profile.get("openid"),
        "_gj_sex": profile.get("sex") or 0,
        "_gj_headimgurl": profile.get("headimgurl") or "",
        "_gj_expired": 0,
        "_gj_support": "[0,1,2,3,4,5,6,8,10,11,12,13,14,15,18]",
        "_gj_encoded_nickname": encoded_nickname,
        "_gj_level": profile.get("level") or 0,
    }
    return "; ".join(f"{key}={cookie_value(value)}" for key, value in pairs.items())


def common_headers(token: str | None = None, referer: str = "https://webcdn.m.qq.com/") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "sec-ch-ua": '"Chromium";v="109"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": referer,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["sessionkey"] = token
    return headers


def normalize_profile(payload: Dict[str, Any], guid: str) -> Dict[str, Any]:
    third = payload.get("thirdPartyAccInfo") or {}
    account = pick(payload, ["account", "accountId", "Account", "userId", "UserId", "uin"])
    return {
        "loginKey": pick(payload, ["loginKey", "LoginKey", "loginkey", "login_key"]),
        "openid": pick(payload, ["openid", "openId", "OpenId", "OpenID"]) or pick(third, ["bindAccount", "openid", "openId", "OpenId", "OpenID"]),
        "nickname": pick(payload, ["nickname", "nickName", "NickName"]) or pick(third, ["nickname", "nickName", "NickName"]) or "JOY",
        "account": account,
        "userId": pick(payload, ["userId", "UserId"]) or account,
        "headimgurl": pick(payload, ["headimgurl", "headImgUrl", "HeadImgUrl", "headImg"]) or pick(third, ["headUrl", "headimgurl", "headImgUrl", "HeadImgUrl"]),
        "guid": pick(payload, ["guid", "clientGuid", "ClientGuid"]) or guid,
        "imei": pick(payload, ["imei", "Imei"]) or account,
        "commonid": pick(payload, ["commonid", "commonId", "CommonId"]) or pick(third, ["commonid", "commonId", "CommonId", "unionId"]),
        "serverGuid": pick(payload, ["serverGuid", "server_guid", "ServerGuid"]),
        "sex": pick(payload, ["sex", "Sex"]) or 0,
        "vip": pick(payload, ["vip", "Vip"]) or 0,
        "level": pick(payload, ["level", "Level"]) or 0,
    }


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    """使用 authCode 登录换取登录资料（loginKey/openid）"""
    try:
        print("🔐 [登录] 使用 authCode 换登录资料")
        response = request_with_proxy(
            "POST",
            LOGIN_BY_CODE_URL,
            headers=common_headers(referer="https://webcdn.m.qq.com/"),
            json={"req": {"platType": 3, "loginAccType": 32, "authCode": code, "clientGuid": CLIENT_GUID}},
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        payload = find_login_payload(data)
        if payload:
            profile = normalize_profile(payload, CLIENT_GUID)
            if profile.get("loginKey") and profile.get("openid"):
                print(f"✅ [登录] 登录资料获取成功: account={profile.get('account')} openid={mask(profile.get('openid'))}")
                return profile, data

        print(f"❌ [登录] 登录响应未找到 loginKey/openid: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_post(server: str, url: str, proxies: Dict[str, str] | None, payload: Dict[str, Any], session_key: str = "", cookie: str = "", referer: str = "https://sdi.3g.qq.com/", extra_headers: Dict[str, str] | None = None) -> Tuple[int, Dict[str, Any]]:
    headers = common_headers(session_key, referer=referer)
    headers["Origin"] = "https://sdi.3g.qq.com"
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)

    response = request_with_proxy(
        "POST",
        url,
        headers=headers,
        json=payload,
        proxies=proxies,
        server=server,
    )

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text[:300]}
    return response.status_code, data


def extract_session_key(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    inner = data.get("data") or {}
    user = inner.get("userInfo") or {} if isinstance(inner, dict) else {}
    for item in (
        user.get("sessionKey") if isinstance(user, dict) else None,
        inner.get("sessionKey") if isinstance(inner, dict) else None,
        data.get("sessionKey"),
    ):
        if item:
            return str(item)
    return None


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


def get_cached_token(server: str) -> Dict[str, Any] | None:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("loginKey") and data.get("openid"):
        print(f"✅ [缓存] 使用 {server} 缓存登录资料")
        return data
    return None


def set_cached_token(server: str, profile: Dict[str, Any], expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {
        "loginKey": profile.get("loginKey"),
        "openid": profile.get("openid"),
        "nickname": profile.get("nickname"),
        "account": profile.get("account"),
        "userId": profile.get("userId"),
        "guid": profile.get("guid"),
        "imei": profile.get("imei"),
        "commonid": profile.get("commonid"),
        "serverGuid": profile.get("serverGuid"),
        "headimgurl": profile.get("headimgurl"),
        "sex": profile.get("sex"),
        "vip": profile.get("vip"),
        "level": profile.get("level"),
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def clear_cached_token(server: str) -> None:
    cache = load_token_cache()
    if server in cache:
        del cache[server]
        save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    """优先使用缓存登录资料（本应用无只读校验接口，auth/create 失败时由业务层清缓存重登）"""
    cached = get_cached_token(server)
    if cached:
        return cached, None

    if AUTH_CODE:
        code = AUTH_CODE
        print("🔐 [授权] 使用环境变量 QQPCMGR_AUTHCODE 指定的 authCode")
    else:
        code = get_code(server)
        if not code:
            return None, None

    profile, raw_login = login_by_code(server, code, proxies)
    if not profile:
        return None, raw_login

    expire_time = datetime.fromtimestamp(time.time() + 7 * 24 * 3600).isoformat()
    set_cached_token(server, profile, expire_time)
    return profile, raw_login


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "accountMsg": "-",
        "authMsg": "-",
        "lotteryMsg": "-",
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
        guid = os.getenv(f"qqpcmgr_guid_{index}") or CLIENT_GUID
        profile, raw_login = login_with_cache(server, proxies)
        if not profile:
            result["error"] = f"登录失败: {json_preview(raw_login)}"
            return result

        result["token"] = mask(profile.get("loginKey"))

        def do_auth(p: Dict[str, Any]) -> Tuple[str, int, Dict[str, Any]]:
            cookie = build_cookie(p)
            print(f"🍪 [资料] account={p.get('account')} openid={mask(p.get('openid'))} nickname={p.get('nickname')}")
            status, data = api_post(
                server,
                AUTH_CREATE_URL,
                proxies,
                {
                    "loginKey": p.get("loginKey"),
                    "openid": p.get("openid"),
                    "nickname": p.get("nickname"),
                    "account": int(to_float(p.get("account"))) or p.get("account"),
                    "userId": int(to_float(p.get("userId"))) or p.get("userId"),
                    "headimgurl": p.get("headimgurl"),
                    "guid": p.get("guid") or guid,
                    "imei": int(to_float(p.get("imei"))) or p.get("imei"),
                    "loginType": "wx",
                    "platformId": "pcmgr16",
                    "loginAccType": 2,
                },
                cookie=cookie,
                extra_headers={"sdiaid": SDIAID},
            )
            print(f"🔗 [授权] auth/create HTTP {status} 返回: {json_preview(data, 300)}")
            return cookie, status, data

        cookie, status, auth_data = do_auth(profile)
        session_key = extract_session_key(auth_data)

        if not session_key and get_cached_token(server):
            # 缓存资料失效：清缓存重新登录一次再试
            print("⚠️ [授权] 缓存资料未取得 sessionKey，清缓存重新登录")
            clear_cached_token(server)
            profile, raw_login = login_with_cache(server, proxies)
            if profile:
                cookie, status, auth_data = do_auth(profile)
                session_key = extract_session_key(auth_data)

        if not session_key:
            result["error"] = f"auth/create 未返回 sessionKey: {json_preview(auth_data)}"
            return result

        result["authMsg"] = f"auth/create HTTP {status}，sessionKey 获取成功"

        lottery_status, lottery_data = api_post(
            server,
            DO_LOTTERY_URL,
            proxies,
            {"lid": LOTTERY_ID},
            session_key=session_key,
            cookie=cookie,
            extra_headers={"sdiaid": SDIAID},
        )
        print(f"🎰 [抽奖] doLottery({LOTTERY_ID}) HTTP {lottery_status} 返回: {json_preview(lottery_data, 300)}")
        result["lotteryMsg"] = json_preview(lottery_data, 200)

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🖥️ 腾讯电脑管家任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🔑 凭证：{res["token"]}
🔗 授权：{res["authMsg"]}
🎰 抽奖：{res["lotteryMsg"]}
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
                "accountMsg": "-",
                "authMsg": "-",
                "lotteryMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 腾讯电脑管家任务执行完成                  ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🖥️ 腾讯电脑管家任务完成", build_notify(results))


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
