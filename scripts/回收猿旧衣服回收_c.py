#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
回收猿旧衣服回收动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /api/app/hsy.php?action=auth&method=weixin_bind 使用 code 换 token
  3. 每日签到（连签天数与奖励金）
  4. 福利任务状态汇总
  5. 查询奖励金余额
  6. 奖励金满额自动提现（每周一次）
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN     PushPlus token，可选
  PROXY_API          品赞代理提取 API，可选
  PROXY_TYPE         http / socks5，默认 http
  HSY_CHANNEL_ID     渠道 ID，默认 wx1008
  HSY_WITHDRAW_MIN   提现最低奖励金（元），默认 1

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import hashlib
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from yyb_account_guard import filter_accounts, update_from_result

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


APP_NAME = "回收猿旧衣服回收小程序"
APPID = "wxadd84841bd31a665"
APP_PLATFORM = "hsywx"

# YYB 服务列表，格式为“地址@账号ID或OpenID”，每行一个账号
_SERVER_ENV = os.getenv("YYB_SERVER", "").strip()
SERVERS = [item.strip() for item in _SERVER_ENV.replace(",", "\n").splitlines() if item.strip()]
SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)
YYB_API_KEY = os.getenv("YYB_API_KEY", "").strip()

CHANNEL_ID = os.getenv("HSY_CHANNEL_ID", "wx1008")
WITHDRAW_MIN = float(os.getenv("HSY_WITHDRAW_MIN", "1"))

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://www.52bjy.com"
APPKEY = "1079fb245839e765"
SECRET = "UppwYkfBlk"
MERCHANT_ID = "2"

LOGIN_URL = f"{BASE_URL}/api/app/hsy.php"
SIGN_INFO_URL = f"{BASE_URL}/api/app/hsy.php"
SIGN_URL = f"{BASE_URL}/api/app/hsy.php"
CENTER_URL = f"{BASE_URL}/api/app/hsy.php"
USERINFO_URL = f"{BASE_URL}/api/app/user.php"
TASK_LIST_URL = f"{BASE_URL}/api/app/promotion.php"
AWARD_LIST_URL = f"{BASE_URL}/api/app/envcash.php"
WITHDRAW_URL = f"{BASE_URL}/api/app/envcash.php"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hsycookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541c37) XWEB/25364 "
    f"miniProgram/{APPID}"
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


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return default


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    data = resp.get("data")
    return data if isinstance(data, dict) else {}


def resp_ok(resp: Dict[str, Any]) -> bool:
    return bool(resp.get("isSucess") or resp.get("is_success"))


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ ♻️ 回收猿旧衣服回收动态 code 版               ║")
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


def get_code(server: str) -> str | None:
    try:
        endpoint, ref = parse_yyb_entry(server)
        url = f"{endpoint}/wxapp/getCode"
        headers = {"X-API-Key": YYB_API_KEY} if YYB_API_KEY else {}
        print(f"🔐 [授权] YYB 获取 code（账号 {mask(ref)}）")
        response = direct_session().post(
            url,
            json={"ref": ref, "app_id": APPID},
            headers=headers,
            timeout=20,
        )
        data = response.json()

        result = data.get("data") or {}
        result = result.get("result") if isinstance(result, dict) else {}
        code = result.get("code") if isinstance(result, dict) else None
        if data.get("code") != 0 or not code:
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        print("✅ [授权] code 获取成功")
        return str(code)
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def parse_yyb_entry(raw: str) -> Tuple[str, str]:
    value = str(raw or "").strip()
    if "@" not in value:
        raise ValueError("YYB_SERVER 格式应为 地址@账号ID或OpenID")
    endpoint, ref = value.split("@", 1)
    endpoint = endpoint.strip().rstrip("/")
    ref = ref.strip()
    if not endpoint or not ref:
        raise ValueError("YYB_SERVER 缺少地址或账号标识")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"http://{endpoint}"
    return endpoint, ref


def js_encode(value: str) -> str:
    """Encode a value like JS encodeURIComponent with Destoon's extra escaping."""
    encoded = quote(str(value), safe="-_.~")
    encoded = encoded.replace("!", "%21").replace("'", "%27")
    encoded = encoded.replace("(", "%28").replace(")", "%29")
    encoded = encoded.replace("*", "%2A")
    return encoded


def hsy_query_value(value: Any) -> str:
    text = str(value)
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return js_encode(text)
    return text


def hsy_sign(params: Dict[str, Any]) -> str:
    """小程序 sign 算法：参数按键名排序，拼接 key=value，再追加 secret 取 md5。"""
    qs = "&".join(f"{key}={hsy_query_value(value)}" for key, value in sorted(params.items()))
    return hashlib.md5((qs + SECRET).encode("utf-8")).hexdigest()


def build_api_url(php: str, params: Dict[str, Any]) -> str:
    """构造 /api/app/{php}.php 的签名 URL。"""
    data = dict(params)
    data.pop("php", None)
    data.setdefault("appkey", APPKEY)
    query = "&".join(f"{key}={hsy_query_value(value)}" for key, value in sorted(data.items()))
    sign = hsy_sign(data)
    return f"{BASE_URL}/api/app/{php}.php?{query}&sign={sign}"


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/json",
        "EnvConnection": "test",
        "Referer": f"https://servicewechat.com/{APPID}/134/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["auth"] = token
    return headers


def parse_json(response: requests.Response) -> Dict[str, Any]:
    """按 utf-8 / gbk 顺序解析响应 JSON。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return json.loads(response.content.decode(encoding))
        except Exception:
            continue
    return {
        "code": -1,
        "message": f"JSON解析失败: {response.text[:300]}",
    }


def extract_auth(data: Any) -> Tuple[str | None, str | None]:
    if not isinstance(data, dict):
        return None, None

    inner = data.get("data")
    if isinstance(inner, dict):
        token = inner.get("jiufy_auth") or inner.get("token") or inner.get("accessToken")
        username = inner.get("username")
        if token and token != "null":
            return str(token), str(username or "")
    return None, None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        url = (
            f"{LOGIN_URL}?action=auth&appkey={APPKEY}&channel={CHANNEL_ID}"
            f"&code={code}&inviter=&iv=&merchant_id={MERCHANT_ID}"
            f"&login_source=scan&method=weixin_bind&version=2"
        )
        response = request_with_proxy(
            "POST",
            url,
            data={"encryptedData": ""},
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "EnvConnection": "test",
                "Referer": f"https://servicewechat.com/{APPID}/134/page-frame.html",
            },
            proxies=proxies,
            server=server,
        )
        data = parse_json(response)

        token, username = extract_auth(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, username, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None, None


def api_get(server: str, url: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(),
        proxies=proxies,
        server=server,
    )
    return parse_json(response)


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


def get_cached_token(server: str) -> Tuple[str | None, str | None]:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} token")
                return data["token"], data.get("username") or ""
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None, None


def set_cached_token(server: str, token: str, expire_time: str, username: str) -> None:
    cache = load_token_cache()
    cache[server] = {
        "token": token,
        "username": username,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（用户信息接口验证），失效自动 code 刷新"""
    cache_token, cache_username = get_cached_token(server)
    if cache_token and cache_username:
        print("🔍 [缓存] 验证 token")
        try:
            url = build_api_url(
                "user",
                {
                    "action": "userinfo",
                    "app": APP_PLATFORM,
                    "auth": cache_token,
                    "merchant_id": MERCHANT_ID,
                    "username": cache_username,
                },
            )
            account_resp = api_get(server, url, proxies)
            if resp_ok(account_resp):
                print("✅ [缓存] token 有效")
                return cache_token, cache_username, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None, None

    token, username, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, None, raw_login

    expire_time = datetime.fromtimestamp(time.time() + 7 * 24 * 3600).isoformat()
    set_cached_token(server, token, expire_time, username or "")
    return token, username, raw_login


# ====================== 任务辅助 ======================
def fetch_task_list(server: str, username: str, proxies) -> List[Dict[str, Any]]:
    resp = api_get(
        server,
        build_api_url(
            "promotion",
            {
                "action": "tasklist",
                "app": APP_PLATFORM,
                "merchant_id": MERCHANT_ID,
                "type": "welfare",
                "username": username,
            },
        ),
        proxies,
    )
    data = resp.get("data") or []
    return [item for item in data if isinstance(item, dict)]


def summarize_tasks(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return "无福利任务"
    done = sum(1 for item in tasks if to_int(item.get("is_done")) == 1)
    pending = [str(item.get("title") or item.get("type")) for item in tasks if to_int(item.get("is_done")) != 1]
    text = f"{done}/{len(tasks)} 已完成"
    if pending:
        text += "，待完成：" + "、".join(pending[:4]) + ("..." if len(pending) > 4 else "")
    return text


# ====================== 账号流程 ======================
def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "username": "-",
        "signMsg": "-",
        "taskMsg": "-",
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

    token, username, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)
    result["username"] = username

    try:
        # ====================== 每日签到 ======================
        sign_info = api_get(
            server,
            build_api_url(
                "hsy",
                {
                    "action": "user",
                    "app": APP_PLATFORM,
                    "merchant_id": MERCHANT_ID,
                    "method": "getsigninfo",
                    "username": username,
                    "version": "4",
                },
            ),
            proxies,
        )
        if not resp_ok(sign_info):
            result["signMsg"] = sign_info.get("message") or "签到状态查询失败"
            print(f"⚠️ [签到] {result['signMsg']}")
        else:
            sign_data = safe_data(sign_info)
            has_sign = to_int(sign_data.get("hassign"))
            days = to_int(sign_data.get("thisturn"))
            if has_sign == 1:
                result["signMsg"] = f"今日已签到，连续 {days} 天"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                sign_resp = api_get(
                    server,
                    build_api_url(
                        "hsy",
                        {
                            "action": "user",
                            "app": APP_PLATFORM,
                            "merchant_id": MERCHANT_ID,
                            "method": "qiandao",
                            "username": username,
                            "version": "4",
                        },
                    ),
                    proxies,
                )
                if resp_ok(sign_resp):
                    award = safe_data(sign_resp).get("qiandao_award", "?")
                    result["signMsg"] = f"每日签到成功，连续 {days + 1} 天，奖励金 +{award} 元"
                    print(f"✅ [签到] {result['signMsg']}")
                else:
                    result["signMsg"] = sign_resp.get("message") or "签到失败"
                    print(f"⚠️ [签到] {result['signMsg']}")

        # ====================== 福利任务汇总 ======================
        tasks = fetch_task_list(server, username, proxies)
        result["taskMsg"] = summarize_tasks(tasks)
        print(f"🧧 [福利] {result['taskMsg']}")

        # ====================== 余额查询 ======================
        center_resp = api_get(
            server,
            build_api_url(
                "hsy",
                {
                    "action": "user",
                    "merchant_id": MERCHANT_ID,
                    "method": "center",
                    "username": username,
                },
            ),
            proxies,
        )
        if not resp_ok(center_resp):
            result["balance"] = center_resp.get("message") or "余额获取失败"
            print(f"⚠️ [余额] {result['balance']}")
            result["success"] = True
            return result

        center = safe_data(center_resp)
        award = to_float(center.get("award"))
        sign_days = to_int(center.get("day"))
        result["balance"] = f"奖励金 {award:.2f} 元（累计签到 {sign_days} 天）"
        print(f"💰 [余额] {result['balance']}")

        # ====================== 自动提现 ======================
        award_resp = api_get(
            server,
            build_api_url(
                "envcash",
                {
                    "action": "awardlist",
                    "genre": "0",
                    "merchant_id": MERCHANT_ID,
                    "type": "award",
                    "username": username,
                },
            ),
            proxies,
        )
        if not resp_ok(award_resp):
            result["withdrawMsg"] = award_resp.get("message") or "提现信息查询失败"
            print(f"⚠️ [提现] {result['withdrawMsg']}")
            result["success"] = True
            return result

        award_info = safe_data(award_resp)
        award_amount = to_float(award_info.get("award_amount"))
        freeze_amount = to_float(award_info.get("freeze_amount"))
        cash_min = max(to_float(award_info.get("award_cash")), WITHDRAW_MIN)
        cash_most = to_float(award_info.get("award_cash_most")) or award_amount
        remain_counter = to_int(award_info.get("user_remain_counter"))
        cash_block = to_int(award_info.get("cash_block"))
        withdrawable = round(award_amount - freeze_amount, 2)

        if cash_block == 1:
            result["withdrawMsg"] = "账号提现功能被限制"
            print(f"⚠️ [提现] {result['withdrawMsg']}")
            result["success"] = True
            return result

        if remain_counter <= 0:
            result["withdrawMsg"] = "本周提现次数已用完"
            print(f"⚠️ [提现] {result['withdrawMsg']}")
            result["success"] = True
            return result

        if withdrawable < cash_min:
            result["withdrawMsg"] = f"可提现 {withdrawable:.2f} 元，未达最低 {cash_min:g} 元"
            print(f"⚠️ [提现] {result['withdrawMsg']}")
            result["success"] = True
            return result

        amount = min(withdrawable, cash_most)
        print(f"💸 [提现] 发起提现 {amount:.2f} 元（剩余次数 {remain_counter}）")
        withdraw_resp = api_get(
            server,
            build_api_url(
                "envcash",
                {
                    "action": "add",
                    "amount": f"{amount:.2f}",
                    "app": "wx",
                    "merchant_id": MERCHANT_ID,
                    "type": "award",
                    "username": username,
                    "version": "2",
                },
            ),
            proxies,
        )
        if resp_ok(withdraw_resp):
            transfer_pkg = safe_data(withdraw_resp).get("package_info")
            result["withdrawMsg"] = f"提现 {amount:.2f} 元已提交" + (
                "，微信端确认收款或 24 小时内自动到账" if transfer_pkg else "，预计 24 小时内到账"
            )
        else:
            result["withdrawMsg"] = withdraw_resp.get("message") or "提现失败"
        print(f"💸 [提现] {result['withdrawMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""♻️ 回收猿旧衣服回收任务结果

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
👤 用户：{res["username"]}
📝 签到：{res["signMsg"]}
🧧 福利：{res["taskMsg"]}
💰 余额：{res["balance"]}
💸 提现：{res["withdrawMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    if not SERVERS:
        print("❌ 未配置 YYB_SERVER，格式：地址@账号ID或OpenID，每行一个")
        return

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
            update_from_result(server, result, app_id=APPID)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "username": "-",
                "signMsg": "-",
                "taskMsg": "-",
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
    print("║ 🏁 回收猿任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("♻️ 回收猿旧衣服回收任务完成", build_notify(results))


if __name__ == "__main__":
    main()
