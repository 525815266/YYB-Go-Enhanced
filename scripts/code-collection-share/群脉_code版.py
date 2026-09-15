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
群脉 MAI / quncrm 平台签到动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. oauth.quncrm.com/<accountId>/v2/weapp/oauth 使用 code 换 accessToken（JWT）
  3. 发现签到活动（/modules/campaigncenter/signin/page）
  4. 读取签到状态（/modules/campaigncenter/signin/stats）
  5. 每日签到（/modules/campaigncenter/signin）
  6. Token 本地缓存与自动刷新
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

说明：
  群脉鉴权方式特别：accessToken 不放请求头，而是作为 query 参数
  （accountId + accessToken）随每个业务请求下发。业务失败信封为
  {name, message, status, code, errors}；业务码 400105 /
  errors.campaign.memberFilter.code == "memberBanned" 表示活动人群定向
  未包含该会员（运营配置，非脚本问题），按 ⚠️ 输出并跳过。
  源脚本用 wx_server 按 openid 取码，本 code 版改由本地 code 服务按 appId
  取码，账号变量相应变为 accountId[#备注]。

环境变量：
  PLUSPLUS_TOKEN   PushPlus token，可选
  QYWX_TOKEN       企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API        品赞代理提取 API，可选
  PROXY_TYPE       http / socks5，默认 http
  QUNCRM_ACCOUNTS  账号列表，格式 群脉租户accountId[#备注]，一行一个或 & 分隔
                   （accountId 为 24 位十六进制，取自解包 app-config.json 的 ext.maiAccountId）

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


APP_NAME = "群脉平台签到小程序"
APPID = "wxc5d513880ace81a4"

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

OAUTH_BASE = "https://oauth.quncrm.com"
CONSUMER_BASE = "https://consumer-api.quncrm.com"
OAUTH_URL_TEMPLATE = OAUTH_BASE + "/{account_id}/v2/weapp/oauth"
SIGN_PAGE_URL = f"{CONSUMER_BASE}/modules/campaigncenter/signin/page"
SIGN_STATS_URL = f"{CONSUMER_BASE}/modules/campaigncenter/signin/stats"
SIGN_TAKE_URL = f"{CONSUMER_BASE}/modules/campaigncenter/signin"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quncrmcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; M2012K11AC Build/SKQ1.220303.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/134.0.6998.136 Mobile Safari/537.36 MicroMessenger/8.0.48.2580(0x28003036) MiniProgramEnv/android"
)


def parse_accounts() -> List[Dict[str, Any]]:
    """解析 QUNCRM_ACCOUNTS：accountId[#备注]，一行一个或 & 分隔"""
    raw = os.getenv("QUNCRM_ACCOUNTS", "")
    accounts: List[Dict[str, Any]] = []
    for line in raw.replace("&", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [item.strip() for item in line.split("#")]
        account_id = parts[0] if len(parts) > 0 else ""
        remark = parts[1] if len(parts) > 1 else ""
        accounts.append({"accountId": account_id, "remark": remark})
    return accounts


ACCOUNTS = parse_accounts()


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
    print("║ 👥 群脉平台签到动态 code 版                  ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(ACCOUNTS):<34}║")
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


def common_headers(token: str | None = None, appid: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://servicewechat.com/{appid or APPID}/0/page-frame.html",
        "xweb_xhr": "1",
    }
    return headers


def api_get(
    server: str,
    url: str,
    token: str,
    proxies: Dict[str, str] | None,
    account: Dict[str, Any],
    with_auth: bool = True,
    query: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = dict(query or {})
    if with_auth:
        # 群脉把身份放 query：accountId + accessToken
        params["accountId"] = account.get("accountId", "")
        if token:
            params["accessToken"] = token

    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(appid=account.get("appid") or APPID),
        params=params,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {"status": response.status_code, "message": f"HTTP {response.status_code}: {response.text[:300]}"}


def api_post(
    server: str,
    url: str,
    token: str,
    proxies: Dict[str, str] | None,
    payload: Dict[str, Any],
    account: Dict[str, Any],
    with_auth: bool = True,
    query: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = dict(query or {})
    if with_auth:
        params["accountId"] = account.get("accountId", "")
        if token:
            params["accessToken"] = token

    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(appid=account.get("appid") or APPID),
        json=payload,
        params=params,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {"status": response.status_code, "message": f"HTTP {response.status_code}: {response.text[:300]}"}


def is_ok(resp: Dict[str, Any]) -> bool:
    """成功就是没有 status>=400 的错误信封"""
    try:
        return not (int(resp.get("status") or 0) >= 400)
    except (TypeError, ValueError):
        return True


def msg_of(resp: Dict[str, Any]) -> str:
    return resp.get("message") or resp.get("name") or json_preview(resp, 200)


def code_of(resp: Dict[str, Any]) -> int:
    try:
        return int(resp.get("code") or 0)
    except (TypeError, ValueError):
        return 0


def is_already_done(text: Any) -> bool:
    return bool(re.search(r"已签|已经签|签到过|重复|已完成|already", str(text or ""), re.I))


def is_member_banned(resp: Dict[str, Any]) -> bool:
    """400105 / memberBanned = 活动人群定向没包含这个会员"""
    errors = resp.get("errors") or {}
    campaign = errors.get("campaign") or {} if isinstance(errors, dict) else {}
    member_filter = campaign.get("memberFilter") or {} if isinstance(campaign, dict) else {}
    return code_of(resp) == 400105 or str(member_filter.get("code") or "") == "memberBanned"


def is_auth_error(resp: Dict[str, Any]) -> bool:
    try:
        if int(resp.get("status") or 0) == 401:
            return True
    except (TypeError, ValueError):
        pass
    return bool(re.search(r"token|登录|未授权|过期", msg_of(resp), re.I))


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


def login_by_code(
    server: str, code: str, account: Dict[str, Any], proxies: Dict[str, str] | None
) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        url = OAUTH_URL_TEMPLATE.format(account_id=account.get("accountId", ""))
        data = api_post(
            server,
            url,
            "",
            proxies,
            {
                "scope": "base",
                "code": code,
                "watermark": {"appid": account.get("appid") or APPID},
                "is_group": "false",
            },
            account,
            with_auth=False,
        )

        token = extract_token(data)
        if is_ok(data) and token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


# ====================== Token缓存管理 ======================
def account_key(account: Dict[str, Any]) -> str:
    return f"{account.get('accountId', '')}#{account.get('appid') or APPID}"


CURRENT_KEY = ""


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
    del server  # 缓存按账号（accountId#appid）为键
    cache = load_token_cache()
    data = cache.get(CURRENT_KEY, {})
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print("✅ [缓存] 使用缓存 token")
                return data["token"]
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, token: str, expire_time: str, member_id: Any = None) -> None:
    del server  # 缓存按账号（accountId#appid）为键
    cache = load_token_cache()
    cache[CURRENT_KEY] = {
        "token": token,
        "memberId": member_id,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def signin_page(server: str, token: str, account: Dict[str, Any], proxies: Dict[str, str] | None, need_log: bool = True):
    """读签到活动；会话失效返回 None"""
    resp = api_get(server, SIGN_PAGE_URL, token, proxies, account)
    if not is_ok(resp):
        if is_auth_error(resp):
            return None
        if need_log:
            print(f"⚠️ [签到] 读取签到活动失败: {msg_of(resp)}")
        return {"failed": True, "res": resp}
    if need_log:
        title = resp.get("title") or resp.get("name") or resp.get("id")
        print(f"📣 [签到] 活动: {title}（{resp.get('status') or '-'}）")
    return resp


def login_with_cache(
    server: str, account: Dict[str, Any], proxies: Dict[str, str] | None
) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（签到活动接口验证），失效自动 code 刷新"""
    global CURRENT_KEY
    CURRENT_KEY = account_key(account)

    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            page = signin_page(server, cache_token, account, proxies, need_log=False)
            if page is not None and not page.get("failed"):
                print("✅ [缓存] token 有效")
                return cache_token, page
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None

    token, raw_login = login_by_code(server, code, account, proxies)
    if not token:
        return None, raw_login

    # JWT 未显式下发过期时间，默认 7 天，实际以签到活动接口验证为准
    expire_time = datetime.fromtimestamp(time.time() + 7 * 24 * 3600).isoformat()
    member_id = (raw_login or {}).get("member", {}).get("id") if isinstance(raw_login, dict) else None
    set_cached_token(server, token, expire_time, member_id)
    member_text = f"（会员 {str(member_id)[:8]}…）" if member_id else "（无会员档案）"
    print(f"✅ [登录] {member_text}")
    return token, raw_login


def run_account(index: int, total: int, server: str, account: Dict[str, Any]) -> Dict[str, Any]:
    tag = account.get("remark") or account.get("accountId") or "-"
    result = {
        "server": server,
        "tag": tag,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "activityMsg": "-",
        "signMsg": "-",
        "error": "",
    }

    log_account_header(index, total, f"{server}（{tag}）")

    if not account.get("accountId"):
        result["error"] = "变量值要写成 accountId[#备注]（accountId 取自解包 ext.maiAccountId）"
        print(f"❌ [账号] {result['error']}")
        return result

    proxies, proxy_ip = get_valid_proxy(tag)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    token, raw_login = login_with_cache(server, account, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        page = raw_login if isinstance(raw_login, dict) and raw_login.get("title") else signin_page(server, token, account, proxies)
        if page is None:
            result["error"] = "会话无效，签到跳过"
            print(f"❌ [签到] {result['error']}")
            return result

        if page.get("failed"):
            resp = page.get("res") or {}
            if is_member_banned(resp):
                result["signMsg"] = f"⚠️ {msg_of(resp)}（memberBanned 活动人群定向，登录/会员正常，不做绕过）"
                print(f"⚠️ [签到] {result['signMsg']}")
                result["success"] = True
                return result
            result["error"] = f"读取签到活动失败: {msg_of(resp)}"
            print(f"❌ [签到] {result['error']}")
            return result

        result["activityMsg"] = f"{page.get('title') or page.get('name') or page.get('id')}（{page.get('status') or '-'}）"

        # 签到状态
        stats = api_get(server, SIGN_STATS_URL, token, proxies, account)
        if not is_ok(stats) and is_member_banned(stats):
            result["signMsg"] = f"⚠️ {msg_of(stats)}（memberBanned 活动人群定向，不做绕过）"
            print(f"⚠️ [签到] {result['signMsg']}")
            result["success"] = True
            return result
        if is_ok(stats):
            print(f"📋 [签到] 状态: {json_preview(stats, 140)}")

        # 签到
        sign_resp = api_post(server, SIGN_TAKE_URL, token, proxies, {}, account)
        if is_ok(sign_resp):
            gain = sign_resp.get("bonus") or sign_resp.get("point")
            result["signMsg"] = f"签到成功{f'，+{gain}' if gain else ''}"
            print(f"✅ [签到] {result['signMsg']}")
        elif is_already_done(msg_of(sign_resp)):
            result["signMsg"] = f"今日已签到（{msg_of(sign_resp)}）"
            print(f"✅ [签到] {result['signMsg']}")
        elif is_member_banned(sign_resp):
            result["signMsg"] = f"⚠️ {msg_of(sign_resp)}（memberBanned 活动人群定向，不做绕过）"
            print(f"⚠️ [签到] {result['signMsg']}")
        else:
            result["signMsg"] = f"签到失败: {msg_of(sign_resp)}"
            print(f"❌ [签到] {result['signMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""👥 群脉平台签到任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}（{res["tag"]}）
🌐 代理：{res["proxyStatus"]}（{res["proxyIp"]}）
📣 活动：{res["activityMsg"]}
📝 签到：{res["signMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    if not ACCOUNTS:
        print("❌ [主程序] 未配置 QUNCRM_ACCOUNTS（格式：accountId[#备注]）")
        return

    results: List[Dict[str, Any]] = []

    for index, account in enumerate(ACCOUNTS, 1):
        server = SERVERS[(index - 1) % len(SERVERS)]
        try:
            result = run_account(index, len(ACCOUNTS), server, account)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {account.get('accountId') or server} 执行异常: {exc}")
            results.append({
                "server": server,
                "tag": account.get("remark") or account.get("accountId") or "-",
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "activityMsg": "-",
                "signMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(ACCOUNTS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 群脉平台任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("👥 群脉平台签到任务完成", build_notify(results))


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
