#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
白鲸鱼旧衣服回收动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /api/app/weapp.php 使用 code 换 token
  3. 每日签到
  4. 九宫格抽奖（任务卡动态领取）
  5. 福利任务自动处理（收藏小程序等）
  6. 成长值任务汇总
  7. 查询积分与余额
  8. PushPlus 推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  BJY_CHANNEL_ID    渠道 ID，默认 wx1008
  BJY_OAID          设备 oaid，默认使用抓包中的值
  BJY_DRAW_MODE     task=任务卡动态抽奖，fixed=固定次数，默认 task
  BJY_DRAW_TIMES    固定模式抽奖次数，默认 1

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
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "白鲸鱼旧衣服回收小程序"
APPID = "wxc525caf8e3a9e434"
APP_PLATFORM = "wx"

# YYB 服务列表，格式为“地址@账号ID或OpenID”，每行一个账号
_SERVER_ENV = os.getenv("YYB_SERVER", "").strip()
SERVERS = [item.strip() for item in _SERVER_ENV.replace(",", "\n").splitlines() if item.strip()]
YYB_API_KEY = os.getenv("YYB_API_KEY", "").strip()

CHANNEL_ID = os.getenv("BJY_CHANNEL_ID", "wx1008")
OAID = os.getenv("BJY_OAID", "260827105351e14208")
DRAW_MODE = os.getenv("BJY_DRAW_MODE", "task").lower()
DRAW_TIMES = int(os.getenv("BJY_DRAW_TIMES", "1"))
CLUB_GID = os.getenv("BJY_CLUB_GID", "304")
AUTO_GROWTH_TYPES = ("weixin",)
CLUB_POST_CONTENTS = [
    "今天把家里的旧衣服整理了一大袋，预约了白鲸鱼上门回收，环保又省心，给平台点赞！",
    "换季整理出一堆旧衣服和旧书，交给白鲸鱼回收，还能攒成长值，一举两得。",
    "旧衣回收又一次打卡，快递小哥准时上门，流程很顺畅，支持环保事业！",
    "把闲置的衣物都回收了，白鲸鱼的处理方式很规范，推荐给大家。",
    "断舍离成功！旧衣服旧书统统交给白鲸鱼，为地球减负一点点。",
]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://www.52bjy.com"
APPKEY = "1f70a57fdf4061a7"
SECRET = "eBRaFLkuJ5"
LOGIN_URL = f"{BASE_URL}/api/app/weapp.php"

SIGN_INFO_URL = f"{BASE_URL}/api/app/user.php"
SIGN_URL = f"{BASE_URL}/api/app/user.php"
USERINFO_URL = f"{BASE_URL}/api/app/user.php"
DRAW_LIST_URL = f"{BASE_URL}/api/app/promotionjgg.php"
DRAW_RESULT_URL = f"{BASE_URL}/api/app/promotionjgg.php"
TASK_LIST_URL = f"{BASE_URL}/api/app/promotion.php"
GROWTH_URL = f"{BASE_URL}/api/app/membervip.php"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bjycookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541923) XWEB/25364"
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
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ ♻️ 白鲸鱼旧衣服回收动态 code 版             ║")
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


def bjy_query_value(value: Any) -> str:
    text = str(value)
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return js_encode(text)
    return text


def bjy_sign(params: Dict[str, Any]) -> str:
    """小程序 sign 算法：参数按键名排序，拼接 key=value，再追加 secret 取 md5。"""
    qs = "&".join(f"{key}={bjy_query_value(value)}" for key, value in sorted(params.items()))
    return hashlib.md5((qs + SECRET).encode("utf-8")).hexdigest()


def build_api_url(php: str, params: Dict[str, Any]) -> str:
    """构造 /api/app/{php}.php 的签名 URL。"""
    data = dict(params)
    data.setdefault("sign_type", "md5")
    data.pop("php", None)
    data.setdefault("appkey", APPKEY)
    query = "&".join(f"{key}={bjy_query_value(value)}" for key, value in sorted(data.items()))
    sign = bjy_sign(data)
    return f"{BASE_URL}/api/app/{php}.php?{query}&sign={sign}"

def bjy_sign_raw(params: Dict[str, Any]) -> str:
    """POST 表单签名：原始值按键名排序拼接后追加 secret 取 md5（与小程序 axPost 一致）。"""
    qs = "&".join(f"{key}={params[key]}" for key in sorted(params))
    return hashlib.md5((qs + SECRET).encode("utf-8")).hexdigest()


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/json",
        "EnvConnection": "test",
        "Referer": f"https://servicewechat.com/{APPID}/392/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["auth"] = token
    return headers


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
            f"{LOGIN_URL}?action=bind&app={APP_PLATFORM}&appkey={APPKEY}"
            f"&channel={CHANNEL_ID}&inviter=&merchant_id=1&login_source=scan"
        )
        response = request_with_proxy(
            "POST",
            url,
            data={
                "encryptedData": "",
                "iv": "",
                "code": code,
            },
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "EnvConnection": "test",
                "Referer": f"https://servicewechat.com/{APPID}/392/page-frame.html",
            },
            proxies=proxies,
            server=server,
        )
        response.encoding = "utf-8"
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

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
    response.encoding = "utf-8"
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }

def api_post(server: str, php: str, params: Dict[str, Any], proxies: Dict[str, str] | None) -> Dict[str, Any]:
    """POST 表单请求（小程序 axPost 风格）：参数放 body，sign 用原始值计算。"""
    data = dict(params)
    data.pop("php", None)
    data.pop("sign_type", None)
    data.setdefault("appkey", APPKEY)
    data["sign"] = bjy_sign_raw(data)
    response = request_with_proxy(
        "POST",
        f"{BASE_URL}/api/app/{php}.php",
        data=data,
        headers={**common_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        proxies=proxies,
        server=server,
    )
    response.encoding = "utf-8"
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
                    "auth": cache_token,
                    "username": cache_username,
                    "merchant_id": 1,
                },
            )
            account_resp = api_get(server, url, proxies)
            if account_resp.get("isSucess") or account_resp.get("is_success"):
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
    set_cached_token(server, token, expire_time, username)
    return token, username, raw_login


# ====================== 任务辅助 ======================
def fetch_task_list(server: str, task_type: str, username: str, proxies) -> List[Dict[str, Any]]:
    resp = api_get(
        server,
        build_api_url(
            "promotion",
            {
                "action": "tasklist",
                "app": APP_PLATFORM,
                "merchant_id": 1,
                "type": task_type,
                "username": username,
            },
        ),
        proxies,
    )
    data = resp.get("data") or []
    return [item for item in data if isinstance(item, dict)]


def summarize_tasks(tasks: List[Dict[str, Any]]) -> str:
    done = sum(1 for item in tasks if to_int(item.get("is_done")) == 1)
    pending = [str(item.get("title") or item.get("type")) for item in tasks if to_int(item.get("is_done")) != 1]
    text = f"{done}/{len(tasks)} 已完成"
    if pending:
        text += "，待完成：" + "、".join(pending[:4]) + ("..." if len(pending) > 4 else "")
    return text


def direct_task_action(server: str, task: Dict[str, Any], token: str, username: str, proxies) -> Tuple[Dict[str, Any] | None, str]:
    task_type = task.get("type") or ""
    if task_type == "fav":
        url = build_api_url("weapp", {"action": "addmyapp", "username": username})
        result = api_get(server, url, proxies)
        return result, result.get("message") or "收藏请求已提交"
    if task_type in ("sharefriends", "browse", "myminiprogram"):
        url = build_api_url(
            "user",
            {
                "action": "task",
                "app": APP_PLATFORM,
                "auth": token,
                "type": task_type,
                "username": username,
            },
        )
        result = api_get(server, url, proxies)
        return result, result.get("message") or f"{task.get('title') or task_type} 上报成功"
    return None, "需要真实客户端/业务动作"


def run_welfare_tasks(server: str, token: str, username: str, proxies) -> str:
    tasks = fetch_task_list(server, "welfare", username, proxies)
    action_messages: List[str] = []
    for task in tasks:
        if to_int(task.get("is_done")) == 1:
            continue
        action_resp, action_msg = direct_task_action(server, task, token, username, proxies)
        ok = bool(action_resp is None or action_resp.get("isSucess") or action_resp.get("is_success"))
        name = task.get("title") or task.get("type") or "未命名任务"
        if action_resp is not None and not ok:
            action_msg = action_resp.get("message") or action_msg
        action_messages.append(f"{name}: {'成功' if ok else '失败'} {action_msg}")
        sleep(random.randint(1, 2))

    refreshed = fetch_task_list(server, "welfare", username, proxies)
    profile = safe_data(api_get(
        server,
        build_api_url(
            "user",
            {
                "action": "userinfo",
                "auth": token,
                "username": username,
            },
        ),
        proxies,
    ))
    fav_flag = ""
    if isinstance(profile, dict) and profile.get("weapp_fav") is not None:
        fav_flag = f"，资料 weapp_fav={profile.get('weapp_fav', '?')}"
    result = summarize_tasks(refreshed)
    if action_messages:
        result += "；已处理：" + "；".join(action_messages[:5])
    result += fav_flag
    return result


def run_growth_summary(server: str, username: str, proxies) -> str:
    resp = api_get(
        server,
        build_api_url(
            "membervip",
            {
                "action": "growth",
                "app": APP_PLATFORM,
                "merchant_id": 1,
                "username": username,
            },
        ),
        proxies,
    )
    if not (resp.get("isSucess") or resp.get("is_success")):
        return resp.get("message") or "成长值查询失败"

    data = safe_data(resp)
    growth_task = data.get("growth_task") or {}
    daily_tasks = list(growth_task.get("day") or []) + list(growth_task.get("new") or [])
    daily_done = sum(1 for item in daily_tasks if to_int(item.get("is_done")) == 1)
    pending = [str(item.get("title")) for item in daily_tasks if to_int(item.get("is_done")) != 1]
    base_task = data.get("base_task") or {}
    brand_orders = (base_task.get("brandorder") or {}).get("list") or []
    brand_done = sum(1 for item in brand_orders if to_int(item.get("is_done")) == 1)
    info = base_task.get("information") or {}

    parts = [
        f"累计 {data.get('growths', 0)}（今日 +{data.get('growth', 0)}）",
        f"每日任务 {daily_done}/{len(daily_tasks)}",
        f"基础资料 {'已完成' if to_int(info.get('is_done')) == 1 else '未完成'}",
        f"回收成长任务 {brand_done}/{len(brand_orders)}",
    ]
    if pending:
        parts.append("待完成：" + "、".join(pending[:5]) + ("..." if len(pending) > 5 else ""))
    return "；".join(parts)

def fetch_club_topics(server: str, username: str, proxies) -> List[Dict[str, Any]]:
    resp = api_get(server, build_api_url("club", {"action": "list", "username": username}), proxies)
    data = resp.get("data") or []
    return [item for item in data if isinstance(item, dict) and str(item.get("catid")) != "507"]

def count_today_posts(server: str, token: str, username: str, proxies) -> int:
    resp = api_get(
        server,
        build_api_url(
            "club",
            {"action": "mypublish", "auth": token, "username": username},
        ),
        proxies,
    )
    today = datetime.now().strftime("%Y-%m-%d")
    posts = resp.get("data") or []
    return sum(1 for item in posts if isinstance(item, dict) and str(item.get("addtime", "")).startswith(today))

def run_club_post(server: str, token: str, username: str, proxies, limit: int = 3) -> str:
    ban = api_get(server, build_api_url("club", {"action": "ban_status", "username": username}), proxies)
    if to_int(safe_data(ban).get("status")) == 1:
        return f"账号被禁言：{safe_data(ban).get('tip') or '无法发帖'}"

    posted_today = count_today_posts(server, token, username, proxies)
    if posted_today >= limit:
        return f"今日已发帖 {posted_today}/{limit} 篇，达到上限"

    topics = fetch_club_topics(server, username, proxies)
    gid = CLUB_GID
    title = ""
    for item in topics:
        if str(item.get("gid")) == str(CLUB_GID):
            title = str(item.get("title") or "")
            break
    if not title and topics:
        gid = str(topics[0].get("gid"))
        title = str(topics[0].get("title") or "")
    if not title:
        return "未找到可发布的话题"

    content = CLUB_POST_CONTENTS[(datetime.now().toordinal() + posted_today) % len(CLUB_POST_CONTENTS)]
    sec_response = request_with_proxy(
        "POST",
        f"{BASE_URL}/api/app/weapp.php?action=msgseccheck",
        data={"content": content},
        headers={**common_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        proxies=proxies,
        server=server,
    )
    try:
        sec_data = sec_response.json()
    except Exception:
        sec_data = {}
    errcode = (sec_data.get("data") or {}).get("errcode")
    if str(errcode) != "0":
        return f"内容安全检测未通过（{errcode}），跳过发帖"

    add_resp = api_post(
        server,
        "club",
        {
            "action": "add",
            "auth": token,
            "content": content,
            "gid": gid,
            "thumb": "",
            "title": title,
            "username": username,
            "version": "2",
        },
        proxies,
    )
    if add_resp.get("isSucess") or add_resp.get("is_success"):
        return f"发帖成功（话题：{title}），成长值待审核后发放"
    return f"发帖失败：{add_resp.get('message') or '未知错误'}"

def run_growth_tasks(server: str, token: str, username: str, proxies) -> str:
    messages: List[str] = []
    resp = api_get(
        server,
        build_api_url(
            "membervip",
            {"action": "growth", "app": APP_PLATFORM, "merchant_id": 1, "username": username},
        ),
        proxies,
    )
    if resp.get("isSucess") or resp.get("is_success"):
        growth_task = safe_data(resp).get("growth_task") or {}
        for task in list(growth_task.get("day") or []) + list(growth_task.get("new") or []):
            if to_int(task.get("is_done")) == 1:
                continue
            task_type = task.get("type") or ""
            name = task.get("title") or task_type
            if task_type in AUTO_GROWTH_TYPES:
                report = api_get(
                    server,
                    build_api_url(
                        "user",
                        {"action": "task", "app": APP_PLATFORM, "auth": token, "type": task_type, "username": username},
                    ),
                    proxies,
                )
                ok = bool(report.get("isSucess") or report.get("is_success"))
                messages.append(f"{name}: {'上报成功' if ok else '上报失败 ' + str(report.get('message') or '')}")
                sleep(random.randint(1, 2))
            elif task_type == "club":
                limit = to_int(task.get("period_num"), 3) or 3
                if to_int(task.get("counter")) < limit:
                    messages.append("发帖: " + run_club_post(server, token, username, proxies, limit=limit))
                    sleep(random.randint(1, 2))

    summary = run_growth_summary(server, username, proxies)
    if messages:
        summary += "；已处理：" + "；".join(messages[:6])
    return summary


def get_draw_cards(server: str, username: str, proxies) -> int:
    resp = api_get(
        server,
        build_api_url(
            "promotionjgg",
            {
                "action": "count",
                "channel": "promotion_jgg",
                "merchant_id": 1,
                "username": username,
            },
        ),
        proxies,
    )
    return max(0, to_int(safe_data(resp).get("new_count")))


def run_jgg_tasks(server: str, token: str, username: str, proxies) -> Tuple[str, List[str]]:
    tasks = fetch_task_list(server, "jgg", username, proxies)
    completed: List[str] = []
    skipped: List[str] = []
    for task in tasks:
        if to_int(task.get("is_done")) == 1:
            continue
        task_type = task.get("type") or ""
        name = task.get("title") or task_type
        if task_type == "qiandao":
            skipped.append(f"{name} 由签到流程处理")
            continue
        if task_type in ("sharefriends", "browse", "myminiprogram"):
            action_resp, action_msg = direct_task_action(server, task, token, username, proxies)
            ok = bool(action_resp is None or action_resp.get("isSucess") or action_resp.get("is_success"))
            completed.append(f"{name}: {'成功' if ok else '失败'} {action_msg}")
            sleep(random.randint(1, 2))
        else:
            skipped.append(f"{name} 需真实预约/捐赠/回复")
    text = "；".join(completed) if completed else "无可自动上报任务"
    if skipped:
        text += "；待真实动作：" + "、".join(skipped[:6])
    return text, completed


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
        "welfareMsg": "-",
        "growthMsg": "-",
        "drawTaskMsg": "-",
        "lotteryMsg": "-",
        "balance": "-",
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
                "user",
                {
                    "action": "getsigninfo",
                    "app": APP_PLATFORM,
                    "auth": token,
                    "username": username,
                },
            ),
            proxies,
        )
        if not (sign_info.get("isSucess") or sign_info.get("is_success")):
            result["signMsg"] = sign_info.get("message") or "签到状态查询失败"
            print(f"⚠️ [签到] {result['signMsg']}")
        else:
            sign_data = safe_data(sign_info)
            has_sign = str(sign_data.get("hassign", "0"))
            days = to_int(sign_data.get("thisturn"))
            if has_sign == "1":
                result["signMsg"] = f"今日已签到，连续 {days} 天"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                sign_resp = api_get(
                    server,
                    build_api_url(
                        "user",
                        {
                            "action": "qiandao",
                            "app": APP_PLATFORM,
                            "auth": token,
                            "username": username,
                        },
                    ),
                    proxies,
                )
                if sign_resp.get("isSucess") or sign_resp.get("is_success"):
                    result["signMsg"] = f"每日签到成功，连续 {days + 1} 天"
                    print(f"✅ [签到] {result['signMsg']}")
                else:
                    result["signMsg"] = sign_resp.get("message") or "签到失败"
                    print(f"⚠️ [签到] {result['signMsg']}")

        # ====================== 福利任务 ======================
        result["welfareMsg"] = run_welfare_tasks(server, token, username, proxies)
        print(f"🧧 [福利] {result['welfareMsg']}")

        # ====================== 成长值任务 ======================
        result["growthMsg"] = run_growth_tasks(server, token, username, proxies)
        print(f"🌱 [成长] {result['growthMsg']}")

        # ====================== 九宫格任务卡与抽奖 ======================
        jgg_task_msg, _ = run_jgg_tasks(server, token, username, proxies)
        result["drawTaskMsg"] = jgg_task_msg
        print(f"🎟️ [抽奖任务] {jgg_task_msg}")

        cards = get_draw_cards(server, username, proxies)
        if DRAW_MODE == "fixed":
            cards = max(cards, DRAW_TIMES)
        print(f"🎰 [抽奖] 当前任务卡 {cards} 张")

        draw_config = api_get(
            server,
            build_api_url(
                "promotionjgg",
                {
                    "action": "list",
                    "app": APP_PLATFORM,
                    "merchant_id": "1",
                    "username": username,
                },
            ),
            proxies,
        )
        prize_count = len(safe_data(draw_config).get("items") or []) if (draw_config.get("isSucess") or draw_config.get("is_success")) else 0

        prizes: List[str] = []
        success_draws = 0
        for draw_index in range(1, cards + 1):
            wait_time = random.randint(2, 5)
            print(f"⏳ [抽奖] 第 {draw_index} 次抽奖前等待 {wait_time}s")
            sleep(wait_time)

            draw_resp = api_get(
                server,
                build_api_url(
                    "promotionjgg",
                    {
                        "action": "prize_draw",
                        "app": APP_PLATFORM,
                        "merchant_id": 1,
                        "username": username,
                    },
                ),
                proxies,
            )
            if not (draw_resp.get("isSucess") or draw_resp.get("is_success")):
                msg = draw_resp.get("message") or "抽奖失败"
                prizes.append(f"第{draw_index}次: {msg}")
                print(f"❌ [抽奖] {msg}")
                if "用完" in str(msg):
                    break
                continue

            prize_data = draw_resp.get("data") or {}
            prize_name = prize_data.get("title") or prize_data.get("goodName") or "未知奖品"
            prizes.append(prize_name)
            success_draws += 1
            print(f"✅ [抽奖] 第 {draw_index} 次获得: {prize_name}")

        cards_after = get_draw_cards(server, username, proxies)
        result["lotteryMsg"] = (
            f"奖品格 {prize_count} 个；任务卡后剩余 {cards_after} 张；"
            f"成功 {success_draws} 次；" + ("、".join(prizes) if prizes else "无奖品")
        )

        # ====================== 余额查询 ======================
        user_resp = api_get(
            server,
            build_api_url(
                "user",
                {
                    "action": "userinfo",
                    "auth": token,
                    "username": username,
                    "merchant_id": 1,
                },
            ),
            proxies,
        )
        if not (user_resp.get("isSucess") or user_resp.get("is_success")):
            result["balance"] = user_resp.get("message") or "余额获取失败"
            print(f"⚠️ [余额] {result['balance']}")
        else:
            user_data = safe_data(user_resp)
            credit = user_data.get("credit", 0)
            money = user_data.get("money", "0.00")
            result["balance"] = f"鲸币 {credit} / 现金 {money} 元"
            print(f"💰 [余额] {result['balance']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""♻️ 白鲸鱼旧衣服回收任务结果

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
🧧 福利：{res["welfareMsg"]}
🌱 成长：{res["growthMsg"]}
🎟️ 抽奖任务：{res["drawTaskMsg"]}
🎰 抽奖：{res["lotteryMsg"]}
💰 余额：{res["balance"]}
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
                "welfareMsg": "-",
                "growthMsg": "-",
                "drawTaskMsg": "-",
                "lotteryMsg": "-",
                "balance": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 白鲸鱼旧衣服回收任务执行完成              ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("♻️ 白鲸鱼旧衣服回收任务完成", build_notify(results))


if __name__ == "__main__":
    main()
