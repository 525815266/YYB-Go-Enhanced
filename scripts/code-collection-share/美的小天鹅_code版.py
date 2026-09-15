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
美的小天鹅 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. 美的 uc 登录 getLoginInfo.do 用 code 换 ucAccessToken
  3. 小天鹅 uc_token 登录换 access_token（Bearer）
  4. 主任务中心（含每日签到）逐个完成
  5. 小天鹅日常（喂草/领工作奖励/开工）
  6. 召唤精灵任务中心逐个完成
  7. 查询当前积分
  8. PushPlus 推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

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
from urllib.parse import quote, urlencode

import requests


APP_NAME = "美的小天鹅"
APPID = "wx33856a6b31431c6e"

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

PAGE_VERSION = "1"
UC_LOGIN_URL = "https://mcsp.midea.com/api/cms_bff/mcsp-uc-mvip-bff/app/login/wx/mini/getLoginInfo.do"
BASE_URL = "https://littleswanmp.midea.com"
UC_TOKEN_LOGIN_URL = f"{BASE_URL}/api/auth/login/uc_token"

MAIN_TASK_QUERY_URL = f"{BASE_URL}/api/web/mobile/swanPrize/queryPrizeRuleUserComplete"
MAIN_TASK_BEGIN_URL = f"{BASE_URL}/api/web/mobile/swanPrize/beginTask"
MAIN_TASK_COMPLETE_URL = f"{BASE_URL}/api/web/mobile/swanPrize/completeTask"
AVATAR_TASK_QUERY_URL = f"{BASE_URL}/api/web/mobile/avatarRule/queryPrizeRuleUserComplete"
AVATAR_TASK_BEGIN_URL = f"{BASE_URL}/api/web/mobile/avatarRule/beginTask"
AVATAR_TASK_COMPLETE_URL = f"{BASE_URL}/api/web/mobile/avatarRule/completeTask"
SWAN_INFO_URL = f"{BASE_URL}/api/web/mobile/swan/getSwanByToken"
SWAN_FEED_URL = f"{BASE_URL}/api/web/mobile/swan/feedGrass"
SWAN_GAIN_WORK_URL = f"{BASE_URL}/api/web/mobile/swan/userGainWorkPrize"
SWAN_START_WORK_URL = f"{BASE_URL}/api/web/mobile/swan/swanStartWorking"
POINTS_URL = f"{BASE_URL}/api/web/mobile/avatar/getUserPoints"

TOKEN_TTL_HOURS = 2  # ucAccessToken 约 2 小时

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mdxtncookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF"
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


def is_ok_code(resp: Any) -> bool:
    """小天鹅系接口成功码：code 200/0/"200"/"0"/"000000" 或 success===true"""
    if not isinstance(resp, dict):
        return False
    code = resp.get("code")
    if code in (200, 0, "200", "0", "000000"):
        return True
    return resp.get("success") is True


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🦢 美的小天鹅 code 版                        ║")
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


def common_headers(token: str | None = None, uc_token: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
    }
    if uc_token:
        headers["ucAccessToken"] = uc_token
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = []

    content = data.get("content")
    if isinstance(content, dict):
        candidates.extend([
            content.get("access_token"),
            content.get("accessToken"),
            content.get("token"),
        ])

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("access_token"),
            inner.get("accessToken"),
            inner.get("token"),
        ])

    candidates.extend([
        data.get("access_token"),
        data.get("accessToken"),
        data.get("token"),
    ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """两步登录（照源脚本）：code -> ucAccessToken -> access_token"""
    try:
        print("🔐 [登录] Step1 code 换 ucAccessToken")
        response = request_with_proxy(
            "POST",
            UC_LOGIN_URL,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
            },
            json={
                "jsCode": code,
                "platformType": "WX_LS_MINI",
                "loginMode": 1,
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        if str(data.get("code")) != "000000" or not isinstance(data.get("data"), dict):
            print(f"❌ [登录] 获取 ucAccessToken 失败: {data.get('msg') or data.get('message') or json_preview(data)}")
            return None, data

        uc_token = str(data["data"].get("ucAccessToken") or "")
        if not uc_token:
            print(f"❌ [登录] 登录未返回 ucAccessToken: {json_preview(data)}")
            return None, data

        print("🔐 [登录] Step2 ucAccessToken 换 access_token")
        form = urlencode({"uc_token": uc_token})
        response = request_with_proxy(
            "POST",
            UC_TOKEN_LOGIN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "ucAccessToken": uc_token,
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
            },
            data=form,
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        if not is_ok_code(data):
            msg = str(data.get("chnDesc") or data.get("msg") or data.get("message") or json_preview(data))
            if re.search(r"未注册|注册|会员|not.*regist|no.*user|绑定", msg, re.I):
                print(f"⚠️ [登录] 该微信号还没在美的小天鹅注册/登录过，先在小程序里登录一次再跑（{msg}）")
            else:
                print(f"❌ [登录] 获取 access_token 失败: {msg}")
            return None, {"ucToken": uc_token, "login": data}

        token = extract_token(data)
        if not token:
            print(f"❌ [登录] 登录未返回 access_token: {json_preview(data)}")
            return None, {"ucToken": uc_token, "login": data}

        print(f"✅ [登录] token 获取成功: {mask(token)}")
        return token, {"ucToken": uc_token, "login": data}
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, uc_token: str = "") -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token, uc_token),
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


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any], uc_token: str = "") -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token, uc_token),
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


def set_cached_token(server: str, token: str, expire_time: str, uc_token: str = "") -> None:
    cache = load_token_cache()
    cache[server] = {
        "token": token,
        "ucToken": uc_token,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（积分接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            uc_token = str(load_token_cache().get(server, {}).get("ucToken") or "")
            points_resp = api_post(server, POINTS_URL, cache_token, proxies, {}, uc_token=uc_token)
            if is_ok_code(points_resp):
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

    uc_token = ""
    if isinstance(raw_login, dict):
        uc_token = str(raw_login.get("ucToken") or "")
    expire_time = datetime.fromtimestamp(time.time() + TOKEN_TTL_HOURS * 3600).isoformat()
    set_cached_token(server, token, expire_time, uc_token=uc_token)
    return token, raw_login


def run_task_center(
    server: str,
    token: str,
    uc_token: str,
    proxies: Dict[str, str] | None,
    label: str,
    query_url: str,
    begin_url: str,
    complete_url: str,
    query_body: Dict[str, Any],
) -> Tuple[int, bool]:
    """通用任务中心：查询 -> 未完成逐个 begin -> 停留 -> complete（照源脚本）"""
    done = 0
    sign_hit = False

    resp = api_post(server, query_url, token, proxies, query_body, uc_token=uc_token)
    if not is_ok_code(resp):
        print(f"⚠️ [{label}] 查询失败: {resp.get('chnDesc') or resp.get('msg') or json_preview(resp, 300)}")
        return 0, False

    tasks = resp.get("content") if isinstance(resp.get("content"), list) else []
    undone = [item for item in tasks if isinstance(item, dict) and not item.get("isUserCompleted")]
    print(f"📋 [{label}] 共 {len(tasks)} 个任务，未完成 {len(undone)} 个")

    for task in undone:
        rule_id = str(task.get("id") or "")
        rule_name = str(task.get("ruleName") or task.get("prizeName") or rule_id)
        if re.search(r"签到|每日签到|打卡", rule_name):
            sign_hit = True

        # 任务需按 timeInterval(秒) 停留后才能领取，遵循原脚本；上限 15s 防卡死
        stay = min(15, max(2, to_float(task.get("timeInterval")) or 2))
        try:
            api_post(server, begin_url, token, proxies, {"ruleId": rule_id}, uc_token=uc_token)
            print(f"⏳ [{label}] {rule_name} 停留 {int(stay)}s 后领取")
            sleep(stay)

            complete_resp = api_post(server, complete_url, token, proxies, {"ruleId": rule_id}, uc_token=uc_token)
            if is_ok_code(complete_resp):
                done += 1
                content = complete_resp.get("content") if isinstance(complete_resp.get("content"), dict) else {}
                delta = content.get("changeValue")
                extra = ""
                if delta:
                    extra = f" +{delta}{content.get('prizeName') or content.get('ruleName') or ''}"
                print(f"✅ [{label}] 完成任务: {rule_name}{extra}")
            else:
                msg = complete_resp.get("chnDesc") or complete_resp.get("msg") or json_preview(complete_resp, 200)
                print(f"⚠️ [{label}] 任务未领取: {rule_name} ({msg})")
        except Exception as exc:
            print(f"⚠️ [{label}] 任务出错: {rule_name} ({exc})")

        sleep(random.uniform(0.8, 1.5))

    return done, sign_hit


def swan_daily(server: str, token: str, uc_token: str, proxies: Dict[str, str] | None) -> str:
    """小天鹅日常动作（喂草/领工作奖励/开工），旧天鹅活动下线则静默跳过"""
    try:
        info = api_get(server, SWAN_INFO_URL, token, proxies, uc_token=uc_token)
    except Exception as exc:
        print(f"⚠️ [天鹅] 喂草跳过: {exc}")
        return "旧天鹅日常活动不可用"

    if not is_ok_code(info) or not info.get("content"):
        print("⚠️ [天鹅] 旧天鹅日常活动不可用，跳过喂草/工作间")
        return "旧天鹅日常活动不可用"

    content = info.get("content") or {}
    nick = content.get("swanNick") or "?"
    grass = content.get("grassAmount")
    shell = content.get("shellAmount")
    grass_text = grass if grass is not None else "?"
    shell_text = shell if shell is not None else "?"
    print(f"🦢 [天鹅] 天鹅[{nick}] 草 {grass_text} / 贝壳 {shell_text}")

    fed = 0
    feed_count = min(int(to_float(grass)), 5)
    for i in range(feed_count):
        feed_resp = api_post(server, SWAN_FEED_URL, token, proxies, {}, uc_token=uc_token)
        if not is_ok_code(feed_resp):
            break
        fed += 1
        feed_content = feed_resp.get("content") if isinstance(feed_resp.get("content"), dict) else {}
        shell_after = feed_content.get("shellAmount")
        print(f"✅ [天鹅] 第{i + 1}次喂草，贝壳 {shell_after if shell_after is not None else '?'}")
        sleep(random.uniform(0.6, 1.2))

    api_post(server, SWAN_GAIN_WORK_URL, token, proxies, {}, uc_token=uc_token)
    api_post(server, SWAN_START_WORK_URL, token, proxies, {}, uc_token=uc_token)
    print("✅ [天鹅] 领工作奖励 + 开工完成")
    return f"天鹅[{nick}] 喂草 {fed} 次，已领工作奖励并开工"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "taskMsg": "-",
        "swanMsg": "-",
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
    uc_token = str(load_token_cache().get(server, {}).get("ucToken") or "")

    try:
        done_main, sign_hit_main = run_task_center(
            server, token, uc_token, proxies,
            "主任务",
            MAIN_TASK_QUERY_URL, MAIN_TASK_BEGIN_URL, MAIN_TASK_COMPLETE_URL,
            {"ruleType": "1", "ruleClass": "3"},
        )

        swan_msg = swan_daily(server, token, uc_token, proxies)

        done_avatar, sign_hit_avatar = run_task_center(
            server, token, uc_token, proxies,
            "精灵任务",
            AVATAR_TASK_QUERY_URL, AVATAR_TASK_BEGIN_URL, AVATAR_TASK_COMPLETE_URL,
            {"ruleTypeId": "1", "ruleClassId": "2", "seq": 0},
        )

        sign_hit = sign_hit_main or sign_hit_avatar
        done_total = done_main + done_avatar

        if sign_hit:
            result["signMsg"] = "每日签到任务已处理"
            print(f"✅ [签到] 每日签到任务已处理，本次共完成 {done_total} 项日常")
        else:
            result["signMsg"] = "未发现签到类任务，日常已全部处理"
            print(f"✅ [签到] 每日任务完成（未含签到项），本次共完成 {done_total} 项日常")

        result["taskMsg"] = f"主任务 {done_main} 项 + 精灵任务 {done_avatar} 项"
        result["swanMsg"] = swan_msg

        points_resp = api_post(server, POINTS_URL, token, proxies, {}, uc_token=uc_token)
        if is_ok_code(points_resp):
            points = points_resp.get("content")
            result["pointsMsg"] = str(points)
            print(f"💰 [积分] 当前积分: {points}")
        else:
            print(f"⚠️ [积分] 查询失败: {json_preview(points_resp, 300)}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🦢 美的小天鹅任务结果

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
📋 任务：{res["taskMsg"]}
🦢 天鹅：{res["swanMsg"]}
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
                "taskMsg": "-",
                "swanMsg": "-",
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
    print("║ 🏁 美的小天鹅任务执行完成                    ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🦢 美的小天鹅任务完成", build_notify(results))


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
