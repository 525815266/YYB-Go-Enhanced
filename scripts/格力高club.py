#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用 profile 接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# 抓包来源：格力高club.har
# ==========================================================


"""
格力高CLUB动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /auth/signin_oauth_weixin_mp 使用 code 换 token
  3. 每日签到
  4. 签到抽奖（ENABLE_LOTTERY 控制，默认开启）
  5. 查询乐币
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  GLK_ENABLE_LOTTERY 1 / 0，默认 1
  GLK_LOTTERY_ID    可指定抽奖活动 ID，默认从签到返回里取
  GLK_DISABLE_CACHE 1 / 0，默认 0；设为 1 时强制每次走 code 服务

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


APP_NAME = "格力高CLUB小程序"
APPID = "wx0245348276df851b"

_SERVER_ENV = os.getenv("YYB_SERVER", "").strip()
SERVERS = [item.strip() for item in _SERVER_ENV.replace(",", "\n").splitlines() if item.strip()]
YYB_API_KEY = os.getenv("YYB_API_KEY", "").strip()

LOTTERY_ID = os.getenv("GLK_LOTTERY_ID", "").strip()
ENABLE_LOTTERY = os.getenv("GLK_ENABLE_LOTTERY", "1").lower() in ("1", "true", "yes")
DISABLE_CACHE = os.getenv("GLK_DISABLE_CACHE", "0").lower() in ("1", "true", "yes")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30
CODE_RETRY_TIMES = 3
CODE_TIMEOUT = 12

BASE_URL = "https://crm.glico.cn/miniapp/member"
LOGIN_URL = f"{BASE_URL}/auth/signin_oauth_weixin_mp"
PROFILE_URL = f"{BASE_URL}/profile"
CHECKIN_URL = f"{BASE_URL}/checkin"
LOTTERY_DETAIL_BASE_URL = f"{BASE_URL}/lottery"
LOTTERY_SUBMIT_BASE_URL = f"{BASE_URL}/lotteries/submit"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glkclubcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541c37) XWEB/25364"
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


def to_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    data = resp.get("data") if isinstance(resp, dict) else None
    return data if isinstance(data, dict) else {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🍪 格力高CLUB动态 code 版                  ║")
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
    endpoint, ref = parse_yyb_entry(server)
    url = f"{endpoint}/wxapp/getCode"
    headers = {"X-API-Key": YYB_API_KEY} if YYB_API_KEY else {}
    print(f"🔐 [授权] YYB 获取 code（账号 {mask(ref)}）")

    for attempt in range(1, CODE_RETRY_TIMES + 1):
        try:
            response = direct_session().post(
                url,
                json={"ref": ref, "app_id": APPID},
                headers=headers,
                timeout=CODE_TIMEOUT,
            )
            data = response.json()

            result = data.get("data") or {}
            result = result.get("result") if isinstance(result, dict) else {}
            code = result.get("code") if isinstance(result, dict) else None
            if (
                data.get("code") == 0
                and code
                and code != "null"
            ):
                print("✅ [授权] code 获取成功")
                return code

            print(f"⚠️ [授权] 第 {attempt} 次 code 获取未就绪: {json_preview(data)}")
        except Exception as exc:
            print(f"⚠️ [授权] 第 {attempt} 次 code 获取异常: {exc}")

        if attempt < CODE_RETRY_TIMES:
            sleep(2)

    print("❌ [授权] code 获取失败")
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


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/214/page-frame.html",
        "X-App-Source": "weixin_mp",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["X-Auth-Token"] = token
    return headers


def extract_token(data: Any, headers: Any = None) -> str | None:
    if headers:
        for key in ("X-Auth-Token", "x-auth-token", "token", "Authorization"):
            value = headers.get(key)
            if value and value != "null":
                return str(value)

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

        user = inner.get("user")
        if isinstance(user, dict):
            candidates.extend([
                user.get("token"),
                user.get("accessToken"),
                user.get("access_token"),
                user.get("jwt"),
            ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json={"code": code},
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        token = extract_token(data, response.headers)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_request(
    method: str,
    server: str,
    url: str,
    token: str,
    proxies: Dict[str, str] | None,
    payload: Any = None,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "headers": common_headers(token),
    }
    if payload is not None:
        kwargs["json"] = payload

    response = request_with_proxy(
        method,
        url,
        **kwargs,
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


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    return api_request("GET", server, url, token, proxies)


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Any = None) -> Dict[str, Any]:
    return api_request("POST", server, url, token, proxies, payload)


def api_put(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    return api_request("PUT", server, url, token, proxies, {})


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
    if DISABLE_CACHE:
        print("⚠️ [缓存] GLK_DISABLE_CACHE=1，跳过读取 token 缓存")
        return None

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
    if DISABLE_CACHE:
        print("⚠️ [缓存] GLK_DISABLE_CACHE=1，跳过 token 缓存保存")
        return

    cache = load_token_cache()
    cache[server] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（profile 接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            account_resp = api_get(server, PROFILE_URL, cache_token, proxies)
            if account_resp.get("code") == 0 and safe_data(account_resp).get("id"):
                print("✅ [缓存] token 有效")
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

    expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    set_cached_token(server, token, expire_time)
    return token, raw_login


def extract_checkin_meta(data: Dict[str, Any]) -> Dict[str, Any]:
    task = data.get("signInTaskVo") or {}
    return {
        "reward": data.get("reward") or task.get("reward") or "",
        "rewardName": data.get("rewardName") or task.get("rewardName") or "",
        "topic": data.get("topic") or task.get("topic") or "",
        "lotteriesId": data.get("lotteriesId") or task.get("lotteriesId") or "",
        "continuousDays": data.get("continuousDays") or task.get("continuousDays") or 0,
    }


def do_signin(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[bool, str, str]:
    checkin_resp = api_get(server, CHECKIN_URL, token, proxies)
    if checkin_resp.get("code") != 0:
        msg = checkin_resp.get("msg") or checkin_resp.get("message") or "签到查询失败"
        return False, msg, ""

    data = safe_data(checkin_resp)
    meta = extract_checkin_meta(data)

    if data.get("isSignIn"):
        msg = f"今日已签到，连续 {meta['continuousDays']} 天"
        if meta["topic"]:
            msg += f"（{meta['topic']}）"
        return True, msg, str(meta["lotteriesId"])

    sign_resp = api_put(server, CHECKIN_URL, token, proxies)
    if sign_resp.get("code") != 0:
        msg = sign_resp.get("msg") or sign_resp.get("message") or "签到失败"
        return False, msg, ""

    sign_data = safe_data(sign_resp)
    sign_meta = extract_checkin_meta(sign_data)
    reward_text = sign_meta["rewardName"] or sign_meta["reward"] or ""
    msg = f"签到成功，连续 {sign_meta['continuousDays']} 天"
    if reward_text:
        msg += f"，奖励 {reward_text}"
    if sign_meta["topic"]:
        msg += f"（{sign_meta['topic']}）"

    lottery_id = sign_meta["lotteriesId"] or meta["lotteriesId"]
    return True, msg, str(lottery_id)


def extract_prize_name(data: Dict[str, Any]) -> str:
    if data.get("lotteryResult") == 0:
        return "未中奖"

    item = data.get("lotteryItem") or {}
    name = item.get("name") or ""
    if not name:
        commodity = item.get("commodity") or {}
        name = commodity.get("title") or commodity.get("name") or "未知奖品"
    return str(name)


def do_lottery(server: str, token: str, lottery_id: str, proxies: Dict[str, str] | None) -> str:
    if not ENABLE_LOTTERY:
        return "抽奖未开启"

    if not lottery_id:
        return "未发现抽奖活动"

    detail = api_get(server, f"{LOTTERY_DETAIL_BASE_URL}/{lottery_id}", token, proxies)
    if detail.get("code") != 0:
        msg = detail.get("msg") or detail.get("message") or "获取抽奖信息失败"
        return f"获取抽奖信息失败: {msg}"

    times = to_int(safe_data(detail).get("times"))
    if times <= 0:
        return "今日无可抽奖次数"

    submit_resp = api_post(
        server,
        f"{LOTTERY_SUBMIT_BASE_URL}/{lottery_id}?addressId=",
        token,
        proxies,
        {},
    )
    if submit_resp.get("code") != 0:
        msg = submit_resp.get("msg") or submit_resp.get("message") or "抽奖失败"
        return f"抽奖失败: {msg}"

    prize = extract_prize_name(safe_data(submit_resp))
    return f"抽奖完成：{prize}"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "memberMsg": "-",
        "signMsg": "-",
        "lotteryMsg": "-",
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

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        profile_resp = api_get(server, PROFILE_URL, token, proxies)
        if profile_resp.get("code") == 0:
            profile_data = safe_data(profile_resp)
            result["points"] = str(profile_data.get("points", ""))
            result["memberMsg"] = (
                f"会员ID：{profile_data.get('id', '-')}，乐币：{profile_data.get('points', 0)}"
            )
            print(f"👤 [会员] {result['memberMsg']}")
        else:
            print(f"⚠️ [会员] 获取 profile 失败: {profile_resp.get('msg') or json_preview(profile_resp, 300)}")

        sign_ok, sign_msg, lottery_id = do_signin(server, token, proxies)
        result["signMsg"] = sign_msg
        print(f"✅ [签到] {sign_msg}" if sign_ok else f"❌ [签到] {sign_msg}")

        if not lottery_id and LOTTERY_ID:
            lottery_id = LOTTERY_ID

        lottery_msg = do_lottery(server, token, lottery_id, proxies)
        result["lotteryMsg"] = lottery_msg
        print(f"🎰 [抽奖] {lottery_msg}")

        result["success"] = sign_ok
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍪 格力高CLUB签到任务结果

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
👤 会员：{res["memberMsg"]}
📝 签到：{res["signMsg"]}
🎰 抽奖：{res["lotteryMsg"]}
💰 乐币：{res["points"]}
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
                "memberMsg": "-",
                "signMsg": "-",
                "lotteryMsg": "-",
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
    print("║ 🏁 格力高CLUB任务执行完成               ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🍪 格力高CLUB签到任务完成", build_notify(results))


if __name__ == "__main__":
    main()
