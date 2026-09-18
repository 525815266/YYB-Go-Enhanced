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
海信爱家（公众号会员中心）动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. /ecrp/oauth/init 使用 code 换 Authorization token（需先取 SESSION cookie）
  3. /ecrp/member/initMember 获取活动 token（TOKEN_ACTIVITY）
  4. 每日签到（cps.hisense.com 活动接口）
  5. 打地鼠游戏（查询剩余次数、提交分数 MD5 签名、可选兑换次数）
  6. 查询会员信息（积分/等级/成长值）
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN         PushPlus token，可选
  QYWX_TOKEN             企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API              品赞代理提取 API，可选
  PROXY_TYPE             http / socks5，默认 http
  HISENSE_GAME_SCORE     打地鼠分数区间，默认 16-20
  HISENSE_PARTY_EXCHANGE 是否兑换游戏次数，默认 false

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
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "海信爱家会员中心"
APPID = "wx3b97b20380656267"

SERVERS = [
    "127.0.0.1:8088",
]
if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
GAME_SCORE_RANGE = os.getenv("HISENSE_GAME_SCORE", "16-20")
PARTY_EXCHANGE = os.getenv("HISENSE_PARTY_EXCHANGE", "false").lower() == "true"

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

SWEIXIN_BASE = "https://sweixin.hisense.com"
SESSION_URL = f"{SWEIXIN_BASE}/ecrp/forward/init?state=center"
OAUTH_URL = f"{SWEIXIN_BASE}/ecrp/oauth/init"
INIT_MEMBER_URL = f"{SWEIXIN_BASE}/ecrp/member/initMember"
CPS_BASE = "https://cps.hisense.com"
SIGN_URL = f"{CPS_BASE}/customerAth/activity-manage/activityUser/participate"
GAME_INFO_URL = f"{CPS_BASE}/customerAth/activity-manage/activityUser/getActivityInfo?code=a55ca53d96bd43be81c0df7ced7ef2b0"
PARTY_EXCHANGE_URL = f"{CPS_BASE}/customerAth/activity-manage/activityUser/partyExchange"
SIGN_ACTIVITY_CODE = "74f51fd29cea445e9b95eb0dd14fba40"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hisencookie.json")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.31(0x18001e31) "
    "NetType/WIFI Language/zh_CN miniProgram"
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
    print("║ 📺 海信爱家会员中心动态 code 版             ║")
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


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/xxx/page-frame.html",
    }
    if token:
        headers["Cookie"] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
        data.get("authorization"),
        data.get("Authorization"),
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


def get_session_cookie(server: str, proxies: Dict[str, str] | None) -> str:
    """获取 ecrp SESSION cookie（禁止重定向，从 Set-Cookie 提取）"""
    try:
        response = request_with_proxy(
            "GET",
            SESSION_URL,
            headers={"User-Agent": USER_AGENT},
            proxies=proxies,
            server=server,
            allow_redirects=False,
        )
        set_cookie = response.headers.get("Set-Cookie", "")
        matched = re.search(r"SESSION=([\w]+?;)", set_cookie)
        if matched:
            print("✅ [登录] SESSION 获取成功")
            return matched.group(0)
        print(f"⚠️ [登录] 未提取到 SESSION: {json_preview(dict(response.headers), 200)}")
    except Exception as exc:
        print(f"⚠️ [登录] SESSION 获取异常: {exc}")
    return ""


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        session_cookie = get_session_cookie(server, proxies)
        if not session_cookie:
            print("❌ [登录] 缺少 SESSION cookie")
            return None, None

        print("🔐 [登录] 使用 code 换 Authorization")
        response = request_with_proxy(
            "GET",
            f"{OAUTH_URL}?code={quote(code)}&state=center",
            headers={"User-Agent": USER_AGENT, "Cookie": session_cookie},
            proxies=proxies,
            server=server,
            allow_redirects=False,
        )
        authorization = response.headers.get("Authorization", "")
        if authorization:
            token = f"{session_cookie} Authorization={authorization};"
            print(f"✅ [登录] token 获取成功: {mask(authorization)}")
            return token, {"authorization": authorization, "session": session_cookie}

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        print(f"❌ [登录] 未识别 Authorization 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def get_activity_token(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """用 sweixin 凭证调 initMember 换 TOKEN_ACTIVITY（cps 域活动凭证）"""
    try:
        response = request_with_proxy(
            "GET",
            INIT_MEMBER_URL,
            headers=common_headers(token),
            proxies=proxies,
            server=server,
        )
        try:
            text = response.text
        except Exception:
            text = ""
        matched = re.search(r"TOKEN_ACTIVITY=[\w=]+", text)
        if matched:
            print("✅ [活动] TOKEN_ACTIVITY 获取成功")
            return matched.group(0)
        print(f"⚠️ [活动] 未提取到 TOKEN_ACTIVITY: {text[:200]}")
    except Exception as exc:
        print(f"⚠️ [活动] TOKEN_ACTIVITY 获取异常: {exc}")
    return ""


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


def set_cached_token(server: str, token: str, expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def verify_cps_token(server: str, cps_token: str, proxies: Dict[str, str] | None) -> bool:
    try:
        response = request_with_proxy(
            "GET",
            GAME_INFO_URL,
            headers=common_headers(cps_token),
            proxies=proxies,
            server=server,
        )
        data = response.json()
        return bool(data.get("isSuccess")) or data.get("resultCode") == "00000"
    except Exception:
        return False


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str]:
    """优先使用缓存 TOKEN_ACTIVITY（活动信息接口验证），失效自动 code 刷新"""
    cps_token = get_cached_token(server)
    if cps_token:
        print("🔍 [缓存] 验证 token")
        try:
            if verify_cps_token(server, cps_token, proxies):
                print("✅ [缓存] token 有效")
                return cps_token, ""
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, ""

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, ""

    cps_token = get_activity_token(server, token, proxies)
    if not cps_token:
        print("⚠️ [登录] 未获取到 TOKEN_ACTIVITY，签到接口将无法调用")

    set_cached_token(server, cps_token or token, datetime.fromtimestamp(time.time() + 12 * 3600).isoformat())
    return cps_token, token


def game_score() -> int:
    try:
        low, high = GAME_SCORE_RANGE.split("-")
        return random.randint(int(low), int(high)) * 20
    except Exception:
        return random.randint(16, 20) * 20


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "gameMsg": "-",
        "memberMsg": "-",
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

    cps_token, sweixin_token = login_with_cache(server, proxies)
    if not cps_token:
        result["error"] = "登录失败: 未获取到 TOKEN_ACTIVITY"
        return result

    result["token"] = mask(cps_token)

    try:
        game_scores = 0
        sign_resp = api_post(server, SIGN_URL, cps_token, proxies, {"code": SIGN_ACTIVITY_CODE})
        if sign_resp.get("isSuccess") and sign_resp.get("resultCode") == "00000":
            sign_scores = safe_data(sign_resp).get("obtainScore", "")
            result["signMsg"] = f"签到成功，获得 {sign_scores} 积分"
            print(f"✅ [签到] {result['signMsg']} 🎉")
        elif sign_resp.get("resultCode") == "A0202":
            result["signMsg"] = "重复签到"
            print(f"⚠️ [签到] {result['signMsg']} ❌")
        else:
            result["signMsg"] = f"{sign_resp.get('resultMsg') or json_preview(sign_resp, 200)} ❌"
            print(f"❌ [签到] {result['signMsg']}")

        # 打地鼠游戏
        game_resp = api_get(server, GAME_INFO_URL, cps_token, proxies)
        game_code = safe_data(game_resp).get("code", "")
        remaining = int(safe_data(game_resp).get("userRemainingCount", 0) or 0)
        print(f"🎮 [游戏] 剩余打地鼠次数 {remaining}")

        rounds = 0
        while remaining >= 1 and rounds < 10:
            rounds += 1
            wait_time = random.randint(3, 6)
            print(f"⏳ [游戏] 第 {rounds} 轮前等待 {wait_time}s")
            sleep(wait_time)

            game_resp = api_get(server, GAME_INFO_URL, cps_token, proxies)
            game_code = safe_data(game_resp).get("code", "") or game_code
            remaining = int(safe_data(game_resp).get("userRemainingCount", 0) or 0)
            if not remaining or not game_code:
                break

            print("🎮 [游戏] 开始[打地鼠]游戏...")
            sleep(40)

            score = game_score()
            print(f"🎮 [游戏] 游戏结束, 提交分数: {score} 分")
            submit_resp = api_post(server, SIGN_URL, cps_token, proxies, {
                "code": game_code,
                "gameScore": str(score),
                "gameSignature": md5(f"{game_code}{score}"),
            })
            if submit_resp.get("isSuccess") and submit_resp.get("resultCode") == "00000" and safe_data(submit_resp).get("obtainScore"):
                game_scores += int(safe_data(submit_resp).get("obtainScore") or 0)
                remaining -= 1
                print(f"✅ [游戏] 提交成功，获得 {safe_data(submit_resp).get('obtainScore')} 积分")
            else:
                print(f"⚠️ [游戏] 提交失败: {json_preview(submit_resp, 200)}")
                break

        result["gameMsg"] = f"打地鼠共获得 {game_scores} 积分" if game_scores else "无游戏收益"

        if PARTY_EXCHANGE:
            for _ in range(2):
                exchange_resp = api_post(server, PARTY_EXCHANGE_URL, cps_token, proxies, {"code": game_code})
                if exchange_resp.get("isSuccess") and exchange_resp.get("resultCode") == "00000":
                    print("✅ [兑换] 游戏机会兑换成功")
                else:
                    print(f"⚠️ [兑换] 兑换失败: {exchange_resp.get('resultMsg') or json_preview(exchange_resp, 200)}")

        # 会员信息（sweixin 凭证）
        if sweixin_token:
            member_resp = api_get(server, INIT_MEMBER_URL, sweixin_token, proxies)
            detail = safe_data(member_resp).get("memberDetail") or {}
            if detail:
                score = detail.get("score", "-")
                grade_name = detail.get("gradeName", "-")
                grouth_value = detail.get("grouthValue", "-")
                next_value = detail.get("nextGrouthValue", 0)
                result["memberMsg"] = f"积分 {score}，等级 {grade_name}，成长值 {grouth_value}/{to_float(grouth_value) + to_float(next_value):.0f}"
                print(f"👤 [会员] {result['memberMsg']}")
            else:
                print(f"⚠️ [会员] 信息获取失败: {json_preview(member_resp, 200)}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""📺 海信爱家会员中心任务结果

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
🎮 游戏：{res["gameMsg"]}
👤 会员：{res["memberMsg"]}
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
                "gameMsg": "-",
                "memberMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 海信爱家任务执行完成                    ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("📺 海信爱家任务完成", build_notify(results))


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
