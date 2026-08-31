#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token + 奖励会话 → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
商联道小程序动态 code 版

功能：
  1. 通过 YYB Go 按账号获取微信 code
  2. /api/auth/login 使用 code 换 token + 奖励会话（reward_session）
  3. 每日签到（漏签时自动看广告补签）
  4. 每日任务全套：
     - 观看激励视频广告（home_reward，循环至当日上限）
     - 健康知识文章：阅读心跳 → 答题 → 看广告领双倍奖励（最多 3 篇）
     - 广场发帖（需已实名认证）
     - 好友聊天（有会话时发送一条消息）
  5. 每日任务全勤奖领取
  6. 金豆余额查询
  7. 满足门槛自动申请提现
  8. PushPlus 推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER        YYB Go 服务地址，格式：地址@微信账号标识，多账号换行分隔
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import hashlib
import hmac
import json
import os
import random
import string
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "商联道小程序"
APPID = "wx31a4573b0bf1fcb3"

# 青龙环境变量：YYB_SERVER=YYB地址@账号ID或OpenID，多账号每行一个。
# 保留 CODE_SERVER 作为旧版单账号配置的兼容入口。
_YYB_SERVER_RAW = os.getenv("YYB_SERVER", "")
SERVERS = [line.strip() for line in _YYB_SERVER_RAW.splitlines() if line.strip()]
if not SERVERS and os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER", "").strip()]
if not SERVERS:
    print("❌ 未配置 YYB_SERVER（格式：地址@微信账号标识，多账号换行分隔）")
    raise SystemExit(1)

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://mini.shangliandao.cn"
LOGIN_URL = f"{BASE_URL}/api/auth/login"
REWARD_SESSION_REFRESH_URL = f"{BASE_URL}/api/auth/reward-session/refresh"

PROFILE_URL = f"{BASE_URL}/api/user/profile"
POINTS_URL = f"{BASE_URL}/api/points"

SIGN_DETAIL_URL = f"{BASE_URL}/api/rewards/sign-in-detail"
SIGN_IN_URL = f"{BASE_URL}/api/rewards/sign-in"
SIGN_MAKEUP_URL = f"{BASE_URL}/api/rewards/sign-in-makeup"

HOME_REWARD_CONFIG_URL = f"{BASE_URL}/api/rewards/home-reward-config"
HOME_REWARD_STATUS_URL = f"{BASE_URL}/api/rewards/home-reward-status"
AD_CHALLENGE_URL = f"{BASE_URL}/api/rewards/ad-challenge"
AD_COMPLETE_URL = f"{BASE_URL}/api/rewards/ad-complete"
AD_REWARD_RETRY_URL = f"{BASE_URL}/api/rewards/ad-reward-retry"

DAILY_TASK_STATUS_URL = f"{BASE_URL}/api/daily-task/status"
DAILY_TASK_FULL_REWARD_CLAIM_URL = f"{BASE_URL}/api/daily-task/full-reward/claim"

ARTICLE_LIST_PAGE_URL = f"{BASE_URL}/api/article/list-page"
ARTICLE_TODAY_STATS_URL = f"{BASE_URL}/api/article/today-stats"

SOCIAL_POSTS_URL = f"{BASE_URL}/api/social/posts"
SOCIAL_CONVERSATIONS_URL = f"{BASE_URL}/api/social/conversations"
SOCIAL_MESSAGES_URL = f"{BASE_URL}/api/social/messages"

GOLDEN_BEAN_WITHDRAW_STATUS_URL = f"{BASE_URL}/api/user/golden-bean/withdraw/status"
GOLDEN_BEAN_WITHDRAW_APPLY_URL = f"{BASE_URL}/api/user/golden-bean/withdraw/apply"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sldcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541923) XWEB/19823"
)

NONCE_CHARS = string.ascii_letters + string.digits + "_-"

POST_CONTENT_POOL = [
    "今天天气不错，来广场逛逛，顺便分享一个生活小妙招：衣服沾上油渍可以先用洗洁精干搓再洗，亲测有效！",
    "分享一个健康小知识：饭后不要马上坐下，靠墙站十分钟，对消化和体态都有帮助。",
    "最近在学收纳整理，桌面清爽了心情都变好了，推荐大家试试断舍离。",
    "早睡早起真的有用，坚持了一周感觉白天精神好多了，一起加油。",
    "买菜的时候发现应季的蔬菜水果又新鲜又便宜，大家平时喜欢买什么？",
]


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
    print("║ 🏮 商联道小程序动态 code 版                    ║")
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


def send_pushplus(title: str, content: str) -> None:
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


def parse_yyb_entry(raw: str) -> Tuple[str, str]:
    """解析 YYB_SERVER：地址@微信账号标识。"""
    raw = str(raw or "").strip()
    if "@" not in raw:
        # 兼容旧版 CODE_SERVER=地址（旧服务的 /login 接口不需要 ref）。
        server = raw.rstrip("/")
        if "://" in server:
            server = server.split("://", 1)[1]
        return server, ""
    server, ref = raw.split("@", 1)
    server = server.strip().rstrip("/")
    if "://" in server:
        server = server.split("://", 1)[1]
    ref = ref.strip()
    if not server or not ref:
        print(f"❌ YYB_SERVER 地址或账号标识为空：{raw}")
        return "", ""
    return server, ref


def get_code(entry: str) -> str | None:
    server, ref = parse_yyb_entry(entry)
    if not server:
        return None
    if not ref:
        # 仅兼容旧版本地 code 服务；YYB_SERVER 应始终使用 地址@账号标识。
        url = f"http://{server}/login"
        print(f"🔐 [授权] 请求旧版 code 服务: {url}")
        try:
            response = direct_session().get(url, params={"appId": APPID}, timeout=20)
            data = response.json()
            code = data.get("code")
            if data.get("err") == 0 and code:
                print("✅ [授权] code 获取成功")
                return str(code)
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
        except Exception as exc:
            print(f"❌ [授权] code 获取异常: {exc}")
        return None
    url = f"http://{server}/wxapp/getCode"
    print(f"🔐 [授权] 请求 YYB code 服务: {url}（账号 {ref}）")

    try:
        response = direct_session().post(
            url,
            json={"ref": ref, "app_id": APPID},
            timeout=20,
        )
        data = response.json()
        result = data.get("data") or {}
        if isinstance(result, dict):
            result = result.get("result") or result
        code = result.get("code") if isinstance(result, dict) else None
        if not code and isinstance(data.get("result"), dict):
            code = data["result"].get("code")
        if not code and isinstance(data.get("code"), str):
            code = data.get("code")
        if data.get("code") not in (None, 0, "0") and not code:
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None
        if not code:
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None
        print("✅ [授权] code 获取成功")
        return str(code)
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


# ====================== 请求签名 ======================
# 逆向自小程序 utils/requestSignature.js：
#   X-Mini-Request-Sign  = HMAC-SHA256(key=登录token,  msg=规范串)
#   X-Reward-Sign        = HMAC-SHA256(key=会话secret, msg=规范串+session_id)
#   规范串 = method \n path \n canonical_query \n body_sha256 \n ts \n nonce \n device_id [\n session_id]

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hmac_sha256_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def encode_component(value: str) -> str:
    return quote(str(value), safe="")


def canonical_query(params: Dict[str, Any] | None) -> str:
    """Replicates JS canonical query: sort 'k=v' pairs, percent-encoded."""
    pairs: List[str] = []
    for key in sorted((params or {}).keys()):
        raw = (params or {})[key]
        if raw is None:
            text = ""
        elif raw is True:
            text = "1"
        elif raw is False:
            text = "0"
        else:
            text = str(raw)
        pairs.append(f"{encode_component(key)}={encode_component(text)}")
    pairs.sort()
    return "&".join(pairs)


def make_nonce() -> str:
    return "".join(random.choice(NONCE_CHARS) for _ in range(24))


def make_device_id() -> str:
    """Replicates utils/deviceId.js: sx-<base36(ms)>-<hex random>, max 64 chars."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    num = int(time.time() * 1000)
    b36 = ""
    while num > 0:
        b36 = digits[num % 36] + b36
        num //= 36
    rand_hex = f"{random.getrandbits(64):016x}{random.getrandbits(64):016x}"
    return f"sx-{b36}-{rand_hex}"[:64]


def build_sign_headers(
    method: str,
    path: str,
    body_text: str,
    query: Dict[str, Any] | None,
    token: str,
    device_id: str,
    reward_session: Dict[str, Any] | None,
) -> Dict[str, str]:
    method_up = (method or "GET").upper()
    body_sha = sha256_hex("" if method_up in ("GET", "HEAD") else body_text)
    ts = str(int(time.time()))
    query_text = canonical_query(query)

    headers: Dict[str, str] = {}
    if token:
        nonce = make_nonce()
        message = "\n".join([method_up, path, query_text, body_sha, ts, nonce, device_id or ""])
        headers["X-Mini-Request-Ts"] = ts
        headers["X-Mini-Request-Nonce"] = nonce
        headers["X-Mini-Request-Body-Sha256"] = body_sha
        headers["X-Mini-Request-Sign"] = hmac_sha256_hex(token, message)

        session_id = str((reward_session or {}).get("session_id") or "").strip()
        secret = str((reward_session or {}).get("secret") or "").strip()
        if session_id and secret:
            r_ts = str(int(time.time()))
            r_nonce = make_nonce()
            reward_message = "\n".join(
                [method_up, path, query_text, body_sha, r_ts, r_nonce, device_id or "", session_id]
            )
            headers["X-Reward-Session-Id"] = session_id
            headers["X-Reward-Ts"] = r_ts
            headers["X-Reward-Nonce"] = r_nonce
            headers["X-Reward-Body-Sha256"] = body_sha
            headers["X-Reward-Sign"] = hmac_sha256_hex(secret, reward_message)

    return headers


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/52/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    inner = data.get("data")
    candidates: List[Any] = []
    if isinstance(inner, dict):
        candidates.append(inner.get("token"))
        user = inner.get("user")
        if isinstance(user, dict):
            candidates.append(user.get("token"))

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, device_id: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        payload = {"code": code, "device_id": device_id}
        body_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            data=body_text.encode("utf-8"),
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


def api_request(
    server: str,
    method: str,
    url: str,
    token: str,
    device_id: str,
    reward_session: Dict[str, Any] | None,
    proxies: Dict[str, str] | None,
    payload: Dict[str, Any] | None = None,
    query: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    method_up = method.upper()
    path = url.split(BASE_URL, 1)[-1].split("?", 1)[0]

    body_text = ""
    if method_up not in ("GET", "HEAD") and payload is not None:
        body_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    sign_query = query or {}
    headers = common_headers(token)
    if device_id:
        headers["X-Device-Id"] = device_id
    headers.update(
        build_sign_headers(method_up, path, body_text, sign_query, token, device_id, reward_session)
    )

    kwargs: Dict[str, Any] = {
        "headers": headers,
        "proxies": proxies,
        "server": server,
    }
    if method_up in ("GET", "HEAD"):
        if query:
            kwargs["params"] = query
    else:
        kwargs["data"] = body_text.encode("utf-8")

    response = request_with_proxy(method_up, url, **kwargs)
    try:
        return response.json()
    except Exception:
        return {
            "status": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def api_get(server: str, url: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None, query: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return api_request(server, "GET", url, token, device_id, reward_session, proxies, None, query)


def api_post(server: str, url: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    return api_request(server, "POST", url, token, device_id, reward_session, proxies, payload, None)


def api_put(server: str, url: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    return api_request(server, "PUT", url, token, device_id, reward_session, proxies, payload, None)


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
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} token")
                return data
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, entry: Dict[str, Any]) -> None:
    cache = load_token_cache()
    cache[server] = entry
    save_token_cache(cache)


def refresh_reward_session(server: str, token: str, device_id: str, proxies: Dict[str, str] | None) -> Dict[str, Any] | None:
    print("🔄 [会话] 刷新奖励会话")
    resp = api_post(server, REWARD_SESSION_REFRESH_URL, token, device_id, None, proxies, {"device_id": device_id})
    data = safe_data(resp)
    session = data.get("reward_session")
    if resp.get("status") == 200 and isinstance(session, dict) and session.get("session_id") and session.get("secret"):
        print("✅ [会话] 奖励会话刷新成功")
        return session
    print(f"⚠️ [会话] 奖励会话刷新失败: {json_preview(resp, 300)}")
    return None


def reward_session_valid(session: Dict[str, Any] | None) -> bool:
    if not session or not session.get("session_id") or not session.get("secret"):
        return False
    try:
        expires_at = float(session.get("expires_at") or 0)
        if expires_at and expires_at <= time.time() * 1000 + 30000:
            return False
    except Exception:
        return False
    return True


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None, str, Dict[str, Any] | None]:
    """优先使用缓存 token（用户信息接口验证），失效自动 code 刷新"""
    cached = get_cached_token(server)
    device_id = (cached or {}).get("deviceId") or make_device_id()

    if cached:
        token = cached["token"]
        reward_session = cached.get("rewardSession")
        print("🔍 [缓存] 验证 token")
        try:
            profile_resp = api_get(server, PROFILE_URL, token, device_id, reward_session, proxies)
            if profile_resp.get("status") == 200:
                print("✅ [缓存] token 有效")
                if not reward_session_valid(reward_session):
                    new_session = refresh_reward_session(server, token, device_id, proxies)
                    if new_session:
                        reward_session = new_session
                        cached["rewardSession"] = new_session
                        set_cached_token(server, cached)
                return token, None, device_id, reward_session
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None, device_id, None

    token, raw_login = login_by_code(server, code, device_id, proxies)
    if not token:
        return None, raw_login, device_id, None

    reward_session = None
    expire_time = None
    if raw_login and isinstance(raw_login, dict):
        inner = raw_login.get("data")
        if isinstance(inner, dict):
            session = inner.get("reward_session")
            if isinstance(session, dict):
                expires_in = to_float(session.get("expires_in")) or 7 * 24 * 3600
                reward_session = {
                    "session_id": session.get("session_id"),
                    "secret": session.get("secret"),
                    "expires_in": expires_in,
                    "expires_at": time.time() * 1000 + expires_in * 1000,
                }
            expire_time = inner.get("expireTime") or inner.get("expire_time")
            expires_in = inner.get("expiresIn")
            if not expire_time and isinstance(expires_in, (int, float)) and expires_in > 0:
                expire_time = datetime.fromtimestamp(time.time() + expires_in).isoformat()
    if not expire_time:
        expire_time = datetime.fromtimestamp(time.time() + 7 * 24 * 3600).isoformat()
    elif not isinstance(expire_time, str):
        expire_time = datetime.fromtimestamp(expire_time / 1000).isoformat()

    set_cached_token(server, {
        "token": token,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
        "deviceId": device_id,
        "rewardSession": reward_session,
    })
    return token, raw_login, device_id, reward_session


# ====================== 任务：签到 ======================
def task_sign_in(server: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None) -> str:
    detail = api_get(server, SIGN_DETAIL_URL, token, device_id, reward_session, proxies)
    if detail.get("status") != 200:
        return f"获取签到信息失败: {detail.get('msg') or json_preview(detail, 200)}"

    data = safe_data(detail)
    if data.get("signed_today"):
        streak = data.get("consecutive_days", 0)
        return f"今日已签到，连续 {streak} 天"

    sign_ticket = str(data.get("sign_ticket") or "").strip()
    if not sign_ticket:
        return "签到票据缺失"

    sleep(random.uniform(1, 2))
    resp = api_post(server, SIGN_IN_URL, token, device_id, reward_session, proxies, {"sign_ticket": sign_ticket})
    if resp.get("status") == 200:
        rdata = safe_data(resp)
        beans = to_float(rdata.get("golden_beans")) + to_float(rdata.get("first_sign_bonus"))
        streak = rdata.get("streak_day", 1)
        extra = "，含首次签到奖励" if rdata.get("is_first_sign") else ""
        return f"签到成功 +{beans:.0f} 金豆{extra}，连续 {streak} 天"

    return f"签到失败: {resp.get('msg') or json_preview(resp, 200)}"


# ====================== 任务：激励视频广告 ======================
def task_watch_ads(server: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None) -> str:
    status = api_get(server, HOME_REWARD_STATUS_URL, token, device_id, reward_session, proxies)
    if status.get("status") != 200:
        return f"获取广告状态失败: {status.get('msg') or json_preview(status, 200)}"

    data = safe_data(status)
    remaining = int(data.get("ad_remaining_today") or 0)
    min_watch = int(data.get("min_watch_seconds") or 15)

    if remaining <= 0:
        return f"今日广告已完成（{data.get('ad_count_today', 0)}/{data.get('ad_daily_limit', 20)}）"

    total_beans = 0.0
    watched = 0
    for round_index in range(remaining):
        ad_page_session = str(data.get("ad_page_session") or "").strip()
        ad_ticket = str(data.get("ad_ticket") or "").strip()
        if not ad_page_session or not ad_ticket:
            status = api_get(server, HOME_REWARD_STATUS_URL, token, device_id, reward_session, proxies)
            data = safe_data(status)
            ad_page_session = str(data.get("ad_page_session") or "").strip()
            ad_ticket = str(data.get("ad_ticket") or "").strip()
            if not ad_page_session or not ad_ticket:
                break

        challenge = api_post(
            server, AD_CHALLENGE_URL, token, device_id, reward_session, proxies,
            {"scene": "home_reward", "ad_page_session": ad_page_session, "ad_ticket": ad_ticket},
        )
        challenge_token = str(safe_data(challenge).get("challenge_token") or "").strip()
        if not challenge_token:
            print(f"⚠️ [广告] 第 {round_index + 1} 次挑战票据失败: {json_preview(challenge, 200)}")
            break

        watch_seconds = min_watch + random.randint(15, 22)
        print(f"⏳ [广告] 第 {round_index + 1}/{remaining} 次，模拟观看 {watch_seconds}s")
        sleep(watch_seconds)

        complete_payload = {
            "scene": "home_reward",
            "watch_seconds": watch_seconds,
            "challenge_token": challenge_token,
            "ad_page_session": ad_page_session,
        }
        complete = api_post(server, AD_COMPLETE_URL, token, device_id, reward_session, proxies, complete_payload)
        cdata = safe_data(complete)

        if complete.get("status") == 50305 and cdata.get("biz_no"):
            sleep(1.5)
            complete = api_post(server, AD_REWARD_RETRY_URL, token, device_id, reward_session, proxies, {"biz_no": cdata["biz_no"]})
            cdata = safe_data(complete)

        if complete.get("status") != 200:
            print(f"⚠️ [广告] 第 {round_index + 1} 次记录失败: {complete.get('msg') or json_preview(complete, 200)}")
            break

        amount = to_float(cdata.get("amount"))
        total_beans += amount
        watched += 1
        print(f"✅ [广告] 第 {round_index + 1} 次完成 +{amount:.0f} 金豆")

        sleep(random.uniform(3, 6))
        status = api_get(server, HOME_REWARD_STATUS_URL, token, device_id, reward_session, proxies)
        data = safe_data(status)
        if int(data.get("ad_remaining_today") or 0) <= 0:
            break

    return f"观看广告 {watched} 次，+{total_beans:.0f} 金豆"


# ====================== 任务：健康文章 ======================
def pick_quiz_answer(article: Dict[str, Any]) -> str:
    """按文章内容与选项文本重合度猜答案，猜不中就选 A（答错也有安慰奖）。"""
    quiz = article.get("quiz") or {}
    options = quiz.get("options") or []
    if not options:
        return "A"

    content = str(article.get("content") or "")
    best_id = str(options[0].get("id") or "A")
    best_score = -1
    for option in options:
        text = str(option.get("text") or "")
        score = sum(1 for ch in text if ch in content)
        if score > best_score:
            best_score = score
            best_id = str(option.get("id") or best_id)
    return best_id


def run_article(server: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None, article_id: int) -> Tuple[bool, float, str]:
    detail = api_get(server, f"{BASE_URL}/api/article/{article_id}", token, device_id, reward_session, proxies)
    if detail.get("status") != 200:
        return False, 0.0, f"文章详情失败: {detail.get('msg') or ''}"

    article = safe_data(detail)
    read_session_id = str(article.get("read_session_id") or "").strip()
    read_duration = int(article.get("read_duration") or 30)
    if not read_session_id:
        return False, 0.0, "阅读会话缺失"

    quiz_ticket = ""
    heartbeat_interval = 10
    for _ in range(read_duration // heartbeat_interval + 3):
        sleep(heartbeat_interval)
        heartbeat = api_post(server, f"{BASE_URL}/api/article/{article_id}/heartbeat", token, device_id, reward_session, proxies, {"read_session_id": read_session_id})
        today_status = safe_data(heartbeat).get("today_status") or {}
        if today_status.get("is_qualified"):
            quiz_ticket = str(today_status.get("quiz_ticket") or "").strip()
            if quiz_ticket:
                break

    if not quiz_ticket:
        return False, 0.0, "阅读时长未达标"

    answer = pick_quiz_answer(article)
    quiz_resp = api_post(
        server, f"{BASE_URL}/api/article/{article_id}/quiz", token, device_id, reward_session, proxies,
        {"answer": answer, "read_session_id": read_session_id, "quiz_ticket": quiz_ticket},
    )
    if quiz_resp.get("status") != 200:
        return False, 0.0, f"答题失败: {quiz_resp.get('msg') or ''}"

    quiz_data = safe_data(quiz_resp)
    beans = to_float(quiz_data.get("bean_amount"))
    correct = quiz_data.get("quiz_correct")
    print(f"{'✅' if correct else '⚠️'} [文章] {article_id} 答题{'正确' if correct else '错误(安慰奖)'} +{beans:.0f} 金豆")

    if quiz_data.get("double_enabled") and not quiz_data.get("bean_doubled"):
        double_beans = try_article_double(server, token, device_id, reward_session, proxies, article_id)
        if double_beans > 0:
            beans += double_beans

    return True, beans, ""


def try_article_double(server: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None, article_id: int) -> float:
    challenge = api_post(
        server, AD_CHALLENGE_URL, token, device_id, reward_session, proxies,
        {"scene": "article_reward_double", "session_id": str(article_id)},
    )
    challenge_token = str(safe_data(challenge).get("challenge_token") or "").strip()
    if not challenge_token:
        print(f"⚠️ [文章] {article_id} 双倍挑战票据失败: {json_preview(challenge, 200)}")
        return 0.0

    watch_seconds = 30 + random.randint(3, 10)
    print(f"⏳ [文章] {article_id} 模拟观看双倍广告 {watch_seconds}s")
    sleep(watch_seconds)

    complete = api_post(
        server, AD_COMPLETE_URL, token, device_id, reward_session, proxies,
        {
            "scene": "article_reward_double",
            "watch_seconds": watch_seconds,
            "challenge_token": challenge_token,
            "session_id": str(article_id),
        },
    )
    double_ticket = str(safe_data(complete).get("double_ticket") or "").strip()
    if not double_ticket:
        print(f"⚠️ [文章] {article_id} 双倍票据失败: {json_preview(complete, 200)}")
        return 0.0

    double_resp = api_post(
        server, f"{BASE_URL}/api/article/{article_id}/double-reward", token, device_id, reward_session, proxies,
        {"double_ticket": double_ticket},
    )
    if double_resp.get("status") == 200:
        doubled = to_float(safe_data(double_resp).get("doubled_amount"))
        print(f"✅ [文章] {article_id} 双倍奖励 +{doubled:.0f} 金豆")
        return doubled

    print(f"⚠️ [文章] {article_id} 双倍领取失败: {json_preview(double_resp, 200)}")
    return 0.0


def task_health_articles(server: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None, need_count: int) -> str:
    if need_count <= 0:
        return "已完成"

    list_resp = api_get(server, ARTICLE_LIST_PAGE_URL, token, device_id, reward_session, proxies, {"page": 1, "limit": 10})
    if list_resp.get("status") != 200:
        return f"文章列表失败: {list_resp.get('msg') or ''}"

    articles = safe_data(list_resp).get("list") or []
    candidates = [a for a in articles if isinstance(a, dict) and not a.get("read_today")]
    if not candidates:
        return "今日文章均已读完"

    done = 0
    total_beans = 0.0
    for article in candidates[:need_count]:
        ok, beans, err = run_article(server, token, device_id, reward_session, proxies, int(article.get("id") or 0))
        if ok:
            done += 1
            total_beans += beans
        else:
            print(f"⚠️ [文章] {article.get('id')} {err}")
        sleep(random.uniform(2, 4))

    return f"完成 {done}/{need_count} 篇，+{total_beans:.0f} 金豆"


# ====================== 任务：广场发帖 ======================
def task_publish_post(server: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None) -> str:
    content = random.choice(POST_CONTENT_POOL)
    payload = {"scene": "plaza", "content": content, "media_type": 0}
    resp = api_post(server, SOCIAL_POSTS_URL, token, device_id, reward_session, proxies, payload)
    if resp.get("status") == 200:
        reward = safe_data(resp).get("golden_bean_reward") or {}
        beans = to_float(reward.get("beans"))
        suffix = f" +{beans:.0f} 金豆" if beans > 0 else ""
        return f"发帖成功{suffix}"
    return f"发帖失败: {resp.get('msg') or json_preview(resp, 200)}"


# ====================== 任务：好友聊天 ======================
def task_friend_chat(server: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None) -> str:
    conversation_id = None
    for scene in ("treehouse", "plaza", "offline_store"):
        conv_resp = api_get(server, SOCIAL_CONVERSATIONS_URL, token, device_id, reward_session, proxies, {"scene": scene, "page": 1})
        if conv_resp.get("status") != 200:
            continue
        data = safe_data(conv_resp)
        items = data.get("list") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = data if isinstance(data, list) else []
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                conversation_id = item.get("id")
                break
        if conversation_id:
            break

    if not conversation_id:
        return "无会话，跳过（需先有好友/匹配）"

    payload = {
        "conversation_id": conversation_id,
        "client_msg_id": f"cm_{int(time.time() * 1000)}_{random.randint(1, 9999)}",
        "msg_type": "text",
        "content": "你好呀，日常打卡~",
    }
    resp = api_post(server, SOCIAL_MESSAGES_URL, token, device_id, reward_session, proxies, payload)
    if resp.get("status") == 200:
        return "聊天消息发送成功"
    return f"聊天失败: {resp.get('msg') or json_preview(resp, 200)}"


# ====================== 全勤奖 ======================
def task_full_reward(server: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None) -> Tuple[str, Dict[str, Any]]:
    status = api_get(server, DAILY_TASK_STATUS_URL, token, device_id, reward_session, proxies)
    if status.get("status") != 200:
        return f"任务状态失败: {status.get('msg') or ''}", {}

    data = safe_data(status)
    if data.get("full_reward_claimed"):
        return "全勤奖今日已领取", data
    if not data.get("can_claim_full_reward"):
        return f"任务 {data.get('completed_count', 0)}/{data.get('total_count', 0)}，全勤奖待达成", data

    payload = {
        "date": str(data.get("date") or ""),
        "full_reward_ticket": str(data.get("full_reward_ticket") or ""),
    }
    resp = api_post(server, DAILY_TASK_FULL_REWARD_CLAIM_URL, token, device_id, reward_session, proxies, payload)
    if resp.get("status") == 200:
        beans = to_float(safe_data(resp).get("reward_beans"))
        return f"全勤奖领取成功 +{beans:.0f} 金豆", safe_data(resp)
    return f"全勤奖领取失败: {resp.get('msg') or json_preview(resp, 200)}", data


# ====================== 余额与提现 ======================
def task_withdraw(server: str, token: str, device_id: str, reward_session: Dict[str, Any] | None, proxies: Dict[str, str] | None) -> Tuple[str, str]:
    status = api_get(server, GOLDEN_BEAN_WITHDRAW_STATUS_URL, token, device_id, reward_session, proxies)
    if status.get("status") != 200:
        return "-", f"提现状态失败: {status.get('msg') or ''}"

    data = safe_data(status)
    balance = str(data.get("golden_bean_balance") or "0")
    withdrawable = str(data.get("withdrawable_yuan") or "0.00")

    if data.get("withdrawn_today"):
        return balance, "今日已提现"
    if not data.get("can_withdraw"):
        return balance, f"可提现 {withdrawable} 元，未达门槛 {data.get('withdraw_min_yuan', '0.10')} 元"

    withdraw_ticket = str(data.get("withdraw_ticket") or "").strip()
    if not withdraw_ticket:
        return balance, "提现票据缺失"

    apply_resp = api_post(server, GOLDEN_BEAN_WITHDRAW_APPLY_URL, token, device_id, reward_session, proxies, {"withdraw_ticket": withdraw_ticket})
    adata = safe_data(apply_resp)
    apply_status = str(adata.get("status") or "")

    if apply_resp.get("status") != 200:
        return balance, f"提现申请失败: {apply_resp.get('msg') or json_preview(apply_resp, 200)}"

    if apply_status == "manual_review":
        return balance, "提现申请已提交审核"
    if apply_status == "success":
        return balance, f"提现成功 {withdrawable} 元"
    if adata.get("transfer"):
        return balance, f"提现发起（{withdrawable} 元），需在微信确认收款"
    return balance, f"提现状态: {apply_status or json_preview(adata, 200)}"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "adMsg": "-",
        "articleMsg": "-",
        "postMsg": "-",
        "chatMsg": "-",
        "fullRewardMsg": "-",
        "balance": "-",
        "withdrawMsg": "-",
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

    token, raw_login, device_id, reward_session = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        profile = api_get(server, PROFILE_URL, token, device_id, reward_session, proxies)
        nickname = safe_data(profile).get("nickname", "")
        if nickname:
            print(f"👤 [账号] 昵称: {nickname}")

        result["signMsg"] = task_sign_in(server, token, device_id, reward_session, proxies)
        print(f"📝 [签到] {result['signMsg']}")

        task_status = api_get(server, DAILY_TASK_STATUS_URL, token, device_id, reward_session, proxies)
        tasks = safe_data(task_status).get("tasks") or []
        task_map = {t.get("key"): t for t in tasks if isinstance(t, dict)}

        watch_task = task_map.get("watch_ad") or {}
        if watch_task.get("completed"):
            result["adMsg"] = f"已完成（{watch_task.get('current', 0)}/{watch_task.get('target', 20)}）"
            print(f"📺 [广告] {result['adMsg']}")
        else:
            result["adMsg"] = task_watch_ads(server, token, device_id, reward_session, proxies)
            print(f"📺 [广告] {result['adMsg']}")

        article_task = task_map.get("health_article") or {}
        article_need = max(0, int(article_task.get("target") or 3) - int(article_task.get("current") or 0))
        if article_task.get("completed") or article_need <= 0:
            result["articleMsg"] = f"已完成（{article_task.get('current', 0)}/{article_task.get('target', 3)}）"
            print(f"📖 [文章] {result['articleMsg']}")
        else:
            result["articleMsg"] = task_health_articles(server, token, device_id, reward_session, proxies, article_need)
            print(f"📖 [文章] {result['articleMsg']}")

        post_task = task_map.get("publish_post") or {}
        if post_task.get("completed"):
            result["postMsg"] = "今日已发帖"
            print(f"✍️ [发帖] {result['postMsg']}")
        else:
            result["postMsg"] = task_publish_post(server, token, device_id, reward_session, proxies)
            print(f"✍️ [发帖] {result['postMsg']}")

        chat_task = task_map.get("friend_chat") or {}
        if chat_task.get("completed"):
            result["chatMsg"] = "今日已互动"
            print(f"💬 [聊天] {result['chatMsg']}")
        else:
            result["chatMsg"] = task_friend_chat(server, token, device_id, reward_session, proxies)
            print(f"💬 [聊天] {result['chatMsg']}")

        result["fullRewardMsg"], _ = task_full_reward(server, token, device_id, reward_session, proxies)
        print(f"🎁 [全勤] {result['fullRewardMsg']}")

        balance, withdraw_msg = task_withdraw(server, token, device_id, reward_session, proxies)
        result["balance"] = balance
        result["withdrawMsg"] = withdraw_msg
        print(f"💰 [余额] {balance} 金豆")
        print(f"💸 [提现] {withdraw_msg}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🏮 商联道小程序四账号任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🌍 来源：{res["server"]}
🌐 代理：{res["proxyStatus"]}
📡 出口IP：{res["proxyIp"]}
🔐 Token：{res["token"]}
📝 签到：{res["signMsg"]}
📺 广告：{res["adMsg"]}
📖 文章：{res["articleMsg"]}
✍️ 发帖：{res["postMsg"]}
💬 聊天：{res["chatMsg"]}
🎁 全勤：{res["fullRewardMsg"]}
💰 金豆：{res["balance"]}
💸 提现：{res["withdrawMsg"]}
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
                "adMsg": "-",
                "articleMsg": "-",
                "postMsg": "-",
                "chatMsg": "-",
                "fullRewardMsg": "-",
                "balance": "-",
                "withdrawMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 商联道任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🏮 商联道四账号任务完成", build_notify(results))


if __name__ == "__main__":
    main()
