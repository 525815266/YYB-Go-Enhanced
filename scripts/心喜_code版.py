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
心喜（习酒小程序·高粱园）code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /anti-channeling/.../Member/getJwt 使用 login_code 换 jwt token
  3. 每日签到（高粱园）
  4. 高粱园种植：解锁土地 / 种植 / 浇水 / 施肥 / 收获
  5. 高粱园任务：答题 / 分享 / 真实场景
  6. 添加好友（助力码）
  7. 制酒 / 收酒 / 兑换积分商城
  8. 查询积分与拥有酒
  9. PushPlus 推送
  10. 品赞代理，业务请求优先代理，失败直连兜底

⚠️ 登录接口 login_code 参数为推断（原脚本 login_code 来自抓包/滑块），未经真机验证，失败请抓包核对
⚠️ 心喜会员体系（api.xinc818.com，sso 抓包 token 型）无法 code 化，未纳入本脚本

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       本地 code 服务地址，可选（默认 127.0.0.1:8088）

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


APP_NAME = "心喜（习酒小程序·高粱园）"
APPID = "wx673f827a4c2c94fa"

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

LOGIN_BASE_URL = "https://xcx.exijiu.com"
LOGIN_URL = f"{LOGIN_BASE_URL}/anti-channeling/public/index.php/api/v2/Member/getJwt"

BASE_URL = "https://apimallwm.exijiu.com"
REAL_SCENE_URL = f"{BASE_URL}/garden/notice/realScene"
DAILY_SIGN_URL = f"{BASE_URL}/garden/sign/dailySign"
MEMBER_INFO_URL = f"{BASE_URL}/garden/Gardenmemberinfo/getMemberInfo"
SORGHUM_INDEX_URL = f"{BASE_URL}/garden/sorghum/index"
SORGHUM_EXTEND_URL = f"{BASE_URL}/garden/sorghum/extend"
SORGHUM_SEED_URL = f"{BASE_URL}/garden/sorghum/seed"
SORGHUM_HARVEST_URL = f"{BASE_URL}/garden/sorghum/harvest"
SORGHUM_WATERING_URL = f"{BASE_URL}/garden/sorghum/watering"
SORGHUM_MANURING_URL = f"{BASE_URL}/garden/sorghum/manuring"
TASKS_INDEX_URL = f"{BASE_URL}/garden/tasks/index"
QUESTION_INDEX_URL = f"{BASE_URL}/garden/Gardenquestiontask/index"
QUESTION_ANSWER_URL = f"{BASE_URL}/garden/Gardenquestiontask/answerResults?answer="
DAILY_SHARE_URL = f"{BASE_URL}/garden/gardenmemberinfo/dailyShare"
REAL_SCENE_REWARD_URL = f"{BASE_URL}/garden/realscene/reward"
FRIEND_TOKEN_URL = f"{BASE_URL}/garden/friends/addFriendToken"
WINE_INDEX_URL = f"{BASE_URL}/garden/gardenmemberwine/index"
WINE_MAKE_URL = f"{BASE_URL}/garden/gardenmemberwine/makeWine"
WINE_HARVEST_URL = f"{BASE_URL}/garden/gardenmemberwine/harvestWine"
WINE_EXCHANGE_URL = f"{BASE_URL}/garden/Gardenjifenshop/exchange"

# 1: 高粱，2: 小麦（与源脚本 cropType 一致）
CROP_TYPES = {1: "高粱", 2: "小麦"}

# 浇水/施肥循环安全上限（源脚本为 while err==0 无限循环，由服务端终止）
LOOP_MAX_TIMES = 50

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xjxxcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) XWEB/9129"
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
    print("║ 🍶 心喜（习酒高粱园）code 版                   ║")
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
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://mallwm.exijiu.com",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/264/page-frame.html",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["Authorization"] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("jwt"),
        data.get("accessToken"),
        data.get("access_token"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("jwt"),
            inner.get("accessToken"),
            inner.get("access_token"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 login_code 换 jwt token")
        headers = {
            "User-Agent": USER_AGENT,
            "Connection": "keep-alive",
            "login_code": code,
            "Accept": "*/*",
            "Origin": "https://mallwm.exijiu.com",
            "Referer": f"https://servicewechat.com/{APPID}/264/page-frame.html",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        response = request_with_proxy(
            "GET",
            LOGIN_URL,
            headers=headers,
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


def api_post_raw(server: str, url: str, token: str, proxies: Dict[str, str] | None, body: str, form: bool = False) -> Dict[str, Any]:
    """按原始字符串 body 提交（源脚本 makePost 为 application/x-www-form-urlencoded）"""
    headers = common_headers(token)
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Accept"] = "application/json, text/plain, */*"
    response = request_with_proxy(
        "POST",
        url,
        headers=headers,
        data=body.encode("utf-8"),
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


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（会员信息接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            member_resp = api_get(server, MEMBER_INFO_URL, cache_token, proxies)
            if member_resp.get("code") == 0:
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
    set_cached_token(server, token, expire_time)
    return token, raw_login


def get_user_id(raw_login: Dict[str, Any] | None, index: int) -> str:
    """从登录响应中取用户 id（源脚本为抓包时的 phone_no），取不到用账号序号"""
    if isinstance(raw_login, dict):
        inner = raw_login.get("data")
        if isinstance(inner, dict):
            user_id = inner.get("phone_no") or inner.get("phoneNo") or inner.get("id")
            if user_id:
                return str(user_id)
    return str(index)


def wait_interval(low: float = 1.0, high: float = 2.0) -> None:
    wait_time = random.uniform(low, high)
    print(f"⏳ [间隔] 等待 {wait_time:.1f}s")
    sleep(wait_time)


def daily_sign(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """每日签到"""
    scene_resp = api_post_raw(server, REAL_SCENE_URL, token, proxies, "from=miniprogram_index")
    print(f"📢 [上报] {scene_resp.get('msg') or json_preview(scene_resp, 200)}")

    sign_resp = api_post(server, DAILY_SIGN_URL, token, proxies, {})
    if sign_resp.get("code") != 0:
        print(f"⚠️ [签到] {sign_resp.get('msg') or json_preview(sign_resp, 300)}")
        return sign_resp.get("msg") or "签到失败"

    inner = safe_data(sign_resp)
    if inner.get("isTodayFirstSign"):
        print(f"✅ [签到] {inner.get('tips') or '签到成功'}")
        return str(inner.get("tips") or "签到成功")
    print("✅ [签到] 今日已签到")
    return "今日已签到"


def seed_field(server: str, token: str, proxies: Dict[str, str] | None, field_id: Any, user_id: str) -> None:
    """按酒曲数量决定种植类型：有酒曲种高粱(1)，否则种小麦(2)"""
    info = api_get(server, MEMBER_INFO_URL, token, proxies)
    seed_type = 1 if (safe_data(info).get("wine_yeast") or 0) > 0 else 2
    seed_resp = api_post(server, SORGHUM_SEED_URL, token, proxies, {"id": field_id, "type": seed_type})
    if seed_resp.get("err") == 61010:
        print(f"用户：{user_id}\n{seed_resp.get('msg', '')}")
    print(f"{seed_resp.get('msg', '')}")


def water_and_manure(server: str, token: str, proxies: Dict[str, str] | None, field_id: Any) -> None:
    """循环浇水与施肥（源脚本 while err==0 循环，由服务端终止，此处加安全上限）"""
    err: Any = 0
    rounds = 0
    while err == 0 and rounds < LOOP_MAX_TIMES:
        resp = api_post(server, SORGHUM_WATERING_URL, token, proxies, {"id": field_id})
        print(f"💧 [浇水] {resp.get('msg', '')}")
        err = resp.get("err")
        rounds += 1

    err: Any = 0
    rounds = 0
    while err == 0 and rounds < LOOP_MAX_TIMES:
        resp = api_post(server, SORGHUM_MANURING_URL, token, proxies, {"id": field_id})
        print(f"🌱 [施肥] {resp.get('msg', '')}")
        err = resp.get("err")
        rounds += 1


def plant_garden(server: str, token: str, proxies: Dict[str, str] | None, user_id: str) -> str:
    """高粱园种植：解锁 / 种植 / 收获 / 浇水 / 施肥"""
    info = api_get(server, MEMBER_INFO_URL, token, proxies)
    inner = safe_data(info)
    print(
        f"拥有：高粱*{inner.get('sorghum', 0)} 小麦*{inner.get('wheat', 0)} "
        f"酒曲*{inner.get('wine_yeast', 0)} 酒*{inner.get('wine', 0)} "
        f"水*{inner.get('water', 0)} 肥料*{inner.get('manure', 0)}"
    )

    can_unlock = True
    harvest_count = 0

    index_resp = api_get(server, SORGHUM_INDEX_URL, token, proxies)
    fields = index_resp.get("data") if isinstance(index_resp.get("data"), list) else []

    for field in fields:
        if not isinstance(field, dict):
            continue
        field_id = field.get("id")
        serial = field.get("serial_number")
        crop_name = CROP_TYPES.get(field.get("crop"), "作物")

        if field.get("status") == -1:
            print(f"第{serial}块地：未解锁")
            if can_unlock:
                print("开始解锁土地")
                extend_resp = api_post(server, SORGHUM_EXTEND_URL, token, proxies, {"serial_number": serial})
                if extend_resp.get("code") == 0:
                    print(f"✅ [种植] {extend_resp.get('msg', '')}")
                    print("开始种植")
                    seed_field(server, token, proxies, field_id, user_id)
                else:
                    print(f"⚠️ [种植] {extend_resp.get('msg', '')}")
                    can_unlock = False
        else:
            print(f"第{serial}块地：已解锁")
            print(f"种植：{crop_name}*{field.get('volumn', 0)} 拥有酒：{field.get('crop_time', '-')}")

            if field.get("status") == 0:
                print(f"{crop_name}已收获，未种植")
                print("开始种植")
                seed_field(server, token, proxies, field_id, user_id)
            elif field.get("status") == 2:
                print(f"{crop_name}已成熟，开始收获")
                harvest_resp = api_post(server, SORGHUM_HARVEST_URL, token, proxies, {"id": field_id})
                print(f"🌾 [收获] {harvest_resp.get('msg', '')}")
                harvest_count += 1
                print("开始种植")
                seed_field(server, token, proxies, field_id, user_id)
            else:
                water_and_manure(server, token, proxies, field_id)

        # 每块地处理完后重新查询，若已成熟则收获补种，再浇水施肥
        wait_interval(0.5, 1.5)
        index_resp = api_get(server, SORGHUM_INDEX_URL, token, proxies)
        fields_now = index_resp.get("data") if isinstance(index_resp.get("data"), list) else []
        current = next((f for f in fields_now if isinstance(f, dict) and f.get("id") == field_id), None)
        if current and current.get("status") == 2:
            print(f"{crop_name}已成熟，开始收获")
            harvest_resp = api_post(server, SORGHUM_HARVEST_URL, token, proxies, {"id": field_id})
            print(f"🌾 [收获] {harvest_resp.get('msg', '')}")
            harvest_count += 1
            print("开始种植")
            seed_field(server, token, proxies, field_id, user_id)

        water_and_manure(server, token, proxies, field_id)

    return f"收获 {harvest_count} 次"


def do_tasks(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """高粱园任务：答题 / 分享 / 真实场景"""
    tasks_resp = api_get(server, TASKS_INDEX_URL, token, proxies)
    tasks = safe_data(tasks_resp)
    if not isinstance(tasks, list):
        print(f"⚠️ [任务] {tasks_resp.get('msg') or json_preview(tasks_resp, 300)}")
        return "-"

    done = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        print(f"任务：{task.get('name')} id：{task.get('id')}")
        if task.get("is_complete") == 1:
            print("✅ [任务] 任务已完成")
            done += 1
            continue

        if task.get("id") == 1:
            # 答题任务
            question_resp = api_get(server, QUESTION_INDEX_URL, token, proxies)
            questions = safe_data(question_resp)
            if isinstance(questions, list) and questions:
                first = questions[0]
                answer = [{"itemid": str(first.get("id")), "selected": str(first.get("answer", ""))}]
                answer_json = json.dumps(answer, separators=(",", ":"), ensure_ascii=False)
                # 对应源脚本 encodeURI(JSON.stringify(...))
                encoded = quote(answer_json, safe='{}":,[]')
                answer_resp = api_get(server, QUESTION_ANSWER_URL + encoded, token, proxies)
                print(f"📝 [答题] {answer_resp.get('msg', '')}")
        elif task.get("id") == 2:
            # 分享任务
            for _ in range(int(task.get("limit_num") or 0)):
                share_resp = api_get(server, DAILY_SHARE_URL, token, proxies)
                print(f"📤 [分享] {share_resp.get('msg', '')}")
        elif task.get("id") == 4:
            # 真实场景任务
            api_get(server, REAL_SCENE_URL, token, proxies)
            reward_resp = api_get(server, REAL_SCENE_REWARD_URL, token, proxies)
            print(f"🏞️ [场景] {reward_resp.get('msg', '')}")

        done += 1

    return f"处理任务 {done} 项"


def add_friend(server: str, token: str, proxies: Dict[str, str] | None, user_id: str) -> str:
    """添加好友（助力码）"""
    friend_resp = api_get(server, FRIEND_TOKEN_URL, token, proxies)
    friend_data = safe_data(friend_resp)
    if isinstance(friend_data, dict):
        friend_data["friend_id"] = user_id
        print(f"🤝 [好友] 助力码：{json.dumps(friend_data, ensure_ascii=False)}")
        return f"助力码：{json.dumps(friend_data, ensure_ascii=False)}"
    print(f"⚠️ [好友] {friend_resp.get('msg') or json_preview(friend_resp, 300)}")
    return "-"


def make_wine(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """制酒 / 收酒"""
    wine_resp = api_get(server, WINE_INDEX_URL, token, proxies)
    if wine_resp.get("crrent_volumn") == 0:
        print("没有正在酿造的酒，开始制酒")
        make_resp = api_post_raw(server, WINE_MAKE_URL, token, proxies, "volumn=200", form=True)
        print(f"🍶 [制酒] {make_resp.get('msg', '')}")

    wines = wine_resp.get("data") if isinstance(wine_resp.get("data"), list) else []
    harvested = 0
    for wine in wines:
        if not isinstance(wine, dict):
            continue
        print(f"酒*{wine.get('crrent_volumn', wine.get('volumn', '?'))} 拥有酒：{wine.get('crop_time', '-')}")
        if wine.get("status") == 4:
            harvest_url = f"{WINE_HARVEST_URL}?id={wine.get('id')}"
            harvest_resp = api_get(server, harvest_url, token, proxies)
            print(f"🍶 [收酒] {harvest_resp.get('msg', '')}")
            harvested += 1

    return f"收酒 {harvested} 坛"


def exchange_and_points(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """兑换积分商城并查询积分"""
    info = api_get(server, MEMBER_INFO_URL, token, proxies)
    inner = safe_data(info)
    wine_count = inner.get("wine", 0)
    print(f"🍷 [兑换] 拥有酒：{wine_count}")

    exchange_resp = api_get(server, f"{WINE_EXCHANGE_URL}?wine={wine_count}", token, proxies)
    print(f"🍷 [兑换] {exchange_resp.get('msg', '')}")

    info = api_get(server, MEMBER_INFO_URL, token, proxies)
    inner = safe_data(info)
    integration = inner.get("integration", 0)
    wine_count = inner.get("wine", 0)
    print(f"💰 [积分] 拥有积分：{integration} 积分：{wine_count}")
    return f"用户积分：{integration} 酒：{wine_count}"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "userMsg": "-",
        "signMsg": "-",
        "plantMsg": "-",
        "taskMsg": "-",
        "wineMsg": "-",
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

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)
    user_id = get_user_id(raw_login, index)

    try:
        wait_interval()
        result["signMsg"] = daily_sign(server, token, proxies)

        wait_interval()
        result["plantMsg"] = plant_garden(server, token, proxies, user_id)

        wait_interval()
        result["taskMsg"] = do_tasks(server, token, proxies)

        wait_interval()
        result["userMsg"] = add_friend(server, token, proxies, user_id)

        wait_interval()
        result["wineMsg"] = make_wine(server, token, proxies)

        wait_interval()
        result["pointsMsg"] = exchange_and_points(server, token, proxies)

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍶 心喜（习酒高粱园）任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🤝 好友：{res["userMsg"]}
📝 签到：{res["signMsg"]}
🌾 种植：{res["plantMsg"]}
🎯 任务：{res["taskMsg"]}
🍶 制酒：{res["wineMsg"]}
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
                "userMsg": "-",
                "signMsg": "-",
                "plantMsg": "-",
                "taskMsg": "-",
                "wineMsg": "-",
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
    print("║ 🏁 心喜任务执行完成                            ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🍶 心喜任务完成", build_notify(results))


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
