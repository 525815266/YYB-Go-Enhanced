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
君品荟（酒谷之旅）小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. garden 两段式登录（习酒 appid，每次消耗 2 个 code）
  3. 会员信息查询（积分/水滴/有机肥/种子）
  4. 每日签到（encryptData AES-192-CBC 加密）
  5. 任务列表与任务处理（分享/每日一答/实景相册/完善信息/订阅奖励）
  6. 农场自动化：收获/种植/浇水/施肥（多轮）
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

⚠️ 平台关键点（源脚本实测结论）：
   酒谷之旅(garden) 这套后端注册在【习酒】小程序 wx489f950decfeb93e 名下，
   不是君品荟自己 —— 登录、请求头、encryptData 的加密密钥都必须用习酒 appid，
   用君品荟 appid 一律 5001「用户信息异常」。会员是同一个（garden 登录返回的
   authorized_token 里 memberInfo.id 与君品荟侧一致）。
⚠️ encryptData 密钥依赖可访问的 wx_server（小猫系列）的 /wx/encryptkey 接口：
   请配置 WX_SERVER_URL / WX_AUTH；openid 通过 JUNPINHUI_OPENIDS 提供
   （与 code 服务账号顺序一致，& 或换行分隔），未配置时脚本尝试从
   authorized_token（JWT）中解出 openid，解不出则加密接口失败。
⚠️ 滑块验证(5008) 按规则不绕。

环境变量：
  code 服务列表：127.0.0.1:8088（CODE_SERVER 可覆盖为单个地址）
  WX_SERVER_URL     wx_server 地址（/wx/encryptkey 取密钥），默认 http://192.168.31.196:8787
  WX_AUTH           wx_server 的 auth 请求头，默认 your-api-key
  JUNPINHUI_OPENIDS 账号 openid 列表（& 或换行分隔，与 code 服务账号顺序一致），可选
  JUNPINHUI_FARM_ROUNDS  农场自动化最大轮数，默认 5
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests pycryptodome
  socks5 代理需：
  pip install requests[socks]
"""

import base64
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

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad as _aes_pad
    _HAS_CRYPTO = True
except Exception:
    _HAS_CRYPTO = False


APP_NAME = "君品荟小程序（酒谷之旅）"

# 酒谷之旅(garden) 后端注册在【习酒】小程序名下，登录/请求头/加密密钥都必须用这个 appid；
# 用君品荟自己的 appid 取 code / 密钥，服务端一律回 5001「用户信息异常」。
APPID = "wx489f950decfeb93e"
# 君品荟小程序 appid（garden 业务不直接用它的 code）
MINI_APP_ID = "wx8d41cdc44c8aeaab"

SERVERS = [
    "127.0.0.1:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

WX_SERVER_URL = (os.getenv("WX_SERVER_URL") or "http://192.168.31.196:8787").rstrip("/")
WX_AUTH = os.getenv("WX_AUTH", "your-api-key")
APP_VERSION = "1.0.12"
FARM_ROUNDS = int(os.getenv("JUNPINHUI_FARM_ROUNDS", "5") or "5")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

MAIN_BASE = "https://xcx.exijiu.com/anti-channeling/public/index.php/api/v2"
GARDEN_BASE = "https://apimallwm.exijiu.com"

SESSION_URL = f"{MAIN_BASE}/auth/session"
GARDEN_LOGIN_URL = f"{GARDEN_BASE}/garden/wechat/login"
ENCRYPTKEY_URL = f"{WX_SERVER_URL}/wx/encryptkey"

MEMBER_INFO_PATH = "/garden/Gardenmemberinfo/getMemberInfo"
SIGN_PATH = "/garden/sign/dailySign"
FARM_INDEX_PATH = "/garden/sorghum/index"
HARVEST_ALL_PATH = "/garden/Sorghum/harvestAll"
HARVEST_PATH = "/garden/sorghum/harvest"
SEED_PATH = "/garden/sorghum/seed"
WATER_PATH = "/garden/sorghum/watering"
MANURE_PATH = "/garden/sorghum/manuring"
TASKS_PATH = "/garden/tasks/index"
SHARE_TASK_PATH = "/garden/gardenmemberinfo/dailyShare"
QUESTION_LIST_PATH = "/garden/Gardenquestiontask/index"
QUESTION_ANSWER_PATH = "/garden/Gardenquestiontask/answerResultsJph"
REALITY_REWARD_PATH = "/garden/realscene/reward"
COMPLETE_INFO_PATH = "/garden/tasks/checkCompleteMemberInfo"
SUBSCRIBE_PRIZE_PATH = "/garden/tasks/getSubscribePrize"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "junpinhuicookie.json")

USER_AGENT = "Mozilla/5.0 MicroMessenger MiniProgramEnv/Windows"

# wx_server 中保存的账号 openid（& 或换行分隔，与 code 服务账号顺序一致）
OPENID_LIST: List[str] = [
    item.strip()
    for item in re.split(r"[&\n]", os.getenv("JUNPINHUI_OPENIDS", ""))
    if item.strip()
]

# garden 会话失效/加密异常的特征词
RELOGIN_PATTERN = re.compile(r"登录|授权|token|Token|未认证|失效|重新进入|用户信息异常")
ENCRYPT_HINT_PATTERN = re.compile(r"用户信息异常|请从小程序重新进入|请删除小程序")
SLIDER_PATTERN = re.compile(r"滑块|5008")


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
    print("║ 🍶 君品荟酒谷之旅动态 code 版                 ║")
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
    print(f"🔐 [授权] 请求本地 code 服务: {url}（appId={APPID} 习酒）")

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


def garden_headers(session: Dict[str, str] | None = None) -> Dict[str, str]:
    """garden 侧的请求头 —— 必须整套用【习酒】的身份（AppID 头 + authorized_token）。"""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": f"https://servicewechat.com/{APPID}/215/page-frame.html",
        "AppID": APPID,
        "App-Version": APP_VERSION,
    }
    session = session or {}
    if session.get("authorizedToken"):
        headers["Authorization"] = session["authorizedToken"]
    if session.get("loginCode"):
        headers["login_code"] = session["loginCode"]
    return headers


def common_headers(session: Dict[str, str] | None = None) -> Dict[str, str]:
    return garden_headers(session)


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("authorized_token"),
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("authorized_token"),
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def api_get(
    server: str,
    url: str,
    session: Dict[str, str] | None,
    proxies: Dict[str, str] | None,
    params: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=garden_headers(session),
        params=params,
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


def api_post(
    server: str,
    url: str,
    session: Dict[str, str] | None,
    proxies: Dict[str, str] | None,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=garden_headers(session),
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


def set_cached_token(server: str, token: str, expire_time: str, extra: Dict[str, Any] | None = None) -> None:
    cache = load_token_cache()
    entry: Dict[str, Any] = {
        "token": token,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    if extra:
        entry.update(extra)
    cache[server] = entry
    save_token_cache(cache)


def resolve_expire_time(raw: Any) -> str:
    expire_time = None
    if raw and isinstance(raw, dict):
        inner = raw.get("data")
        if isinstance(inner, dict):
            expire_time = inner.get("expireTime") or inner.get("expire_time")
            expires_in = inner.get("expiresIn")
            if not expire_time and isinstance(expires_in, (int, float)) and expires_in > 0:
                expire_time = datetime.fromtimestamp(time.time() + expires_in).isoformat()
    if not expire_time:
        expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    elif not isinstance(expire_time, str):
        expire_time = datetime.fromtimestamp(expire_time / 1000).isoformat()
    return expire_time


def ok_code(resp: Any) -> bool:
    """源脚本判定：code==10000 或 success===true 或 err==0"""
    if not isinstance(resp, dict):
        return False
    if str(resp.get("code")) == "10000":
        return True
    if resp.get("success") is True:
        return True
    err = resp.get("err")
    if err is not None:
        try:
            return float(err) == 0
        except (TypeError, ValueError):
            return False
    return False


def assert_ok(resp: Any, action: str) -> Dict[str, Any]:
    if not isinstance(resp, dict) or not ok_code(resp):
        msg = (
            (resp or {}).get("message")
            or (resp or {}).get("msg")
            or (resp or {}).get("errMsg")
            or json_preview(resp, 500)
        )
        raise RuntimeError(f"{action}失败: {msg}")
    return resp.get("data") or {}


def garden_login(server: str, proxies: Dict[str, str] | None) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """garden 会话：走【习酒】appid 的两段式登录（每次跑消耗 2 个 code）。"""
    print("🔐 [登录] garden 两段式登录（消耗 2 个 code）")

    code1 = get_code(server)
    if not code1:
        raise RuntimeError("获取 garden 会话 code 失败")

    sess_resp = api_get(server, SESSION_URL, None, proxies, params={"code": code1})
    login_code = ""
    if isinstance(sess_resp, dict):
        login_code = str(safe_data(sess_resp).get("login_code") or "")
    if not login_code:
        raise RuntimeError(f"auth/session 未返回 login_code: {json_preview(sess_resp, 300)}")

    code2 = get_code(server)
    if not code2:
        raise RuntimeError("获取 garden 登录 code 失败")

    auth_resp = api_get(
        server,
        GARDEN_LOGIN_URL,
        {"loginCode": login_code},
        proxies,
        params={"code": code2},
    )
    assert_ok(auth_resp, "garden 登录")
    authorized_token = ""
    if isinstance(auth_resp, dict):
        authorized_token = str(safe_data(auth_resp).get("authorized_token") or "")
    if not authorized_token:
        raise RuntimeError(f"garden 登录未返回 authorized_token: {json_preview(auth_resp, 500)}")

    session = {"loginCode": login_code, "authorizedToken": authorized_token}
    print(f"✅ [登录] garden 登录成功: {mask(authorized_token)}")
    return session, auth_resp


def login_by_code(
    server: str,
    code: str,
    proxies: Dict[str, str] | None,
) -> Tuple[Dict[str, str] | None, Dict[str, Any] | None]:
    """用第一个 code 走会话段，第二段登录 code 在内部再取一次。"""
    try:
        code1 = code
        sess_resp = api_get(server, SESSION_URL, None, proxies, params={"code": code1})
        login_code = ""
        if isinstance(sess_resp, dict):
            login_code = str(safe_data(sess_resp).get("login_code") or "")
        if not login_code:
            print(f"❌ [登录] auth/session 未返回 login_code: {json_preview(sess_resp, 300)}")
            return None, sess_resp

        code2 = get_code(server)
        if not code2:
            print("❌ [登录] 获取第二段登录 code 失败")
            return None, sess_resp

        auth_resp = api_get(
            server,
            GARDEN_LOGIN_URL,
            {"loginCode": login_code},
            proxies,
            params={"code": code2},
        )
        assert_ok(auth_resp, "garden 登录")
        authorized_token = str(safe_data(auth_resp).get("authorized_token") or "")
        if not authorized_token:
            print(f"❌ [登录] 未识别 authorized_token: {json_preview(auth_resp)}")
            return None, auth_resp

        print(f"✅ [登录] token 获取成功: {mask(authorized_token)}")
        return {"loginCode": login_code, "authorizedToken": authorized_token}, auth_resp
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def login_with_cache(
    server: str,
    proxies: Dict[str, str] | None,
) -> Tuple[Dict[str, str] | None, Dict[str, Any] | None]:
    """优先使用缓存 token（会员信息接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        cached = load_token_cache().get(server) or {}
        session = {
            "loginCode": str(cached.get("loginCode", "") or ""),
            "authorizedToken": cache_token,
        }
        print("🔍 [缓存] 验证 token")
        try:
            resp = api_get(server, f"{GARDEN_BASE}{MEMBER_INFO_PATH}", session, proxies)
            if ok_code(resp):
                print("✅ [缓存] token 有效")
                return session, None
            print(f"⚠️ [缓存] token 已失效，重新登录: {json_preview(resp, 200)}")
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")

    code = get_code(server)
    if not code:
        return None, None

    session, raw_login = login_by_code(server, code, proxies)
    if not session:
        return None, raw_login

    set_cached_token(server, session["authorizedToken"], resolve_expire_time(raw_login), {
        "loginCode": session["loginCode"],
    })
    return session, raw_login


def decode_jwt_openid(token: str) -> str:
    """从 authorized_token(JWT) 解出 openid，解不出返回空串。"""
    try:
        if "." not in token:
            return ""
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
        for key in ("openid", "openId", "open_id"):
            if payload.get(key):
                return str(payload[key])
        member = payload.get("memberInfo") or payload.get("member_info") or {}
        if isinstance(member, dict):
            for key in ("openid", "openId", "open_id"):
                if member.get(key):
                    return str(member[key])
    except Exception:
        pass
    return ""


def resolve_openid(account_index: int, session: Dict[str, str], raw_login: Dict[str, Any] | None) -> str:
    """openid 解析顺序：环境变量 JUNPINHUI_OPENIDS → 登录响应字段 → JWT 解码"""
    if 0 <= account_index - 1 < len(OPENID_LIST):
        return OPENID_LIST[account_index - 1]

    if raw_login and isinstance(raw_login, dict):
        data = raw_login.get("data") if isinstance(raw_login.get("data"), dict) else raw_login
        for key in ("openid", "openId", "open_id"):
            if data.get(key):
                return str(data[key])

    return decode_jwt_openid(session.get("authorizedToken", ""))


# ====================== encryptData 加密 ======================
def aes_cbc_pkcs7_hex(text: str, key: str, iv: str) -> str:
    if not _HAS_CRYPTO:
        raise RuntimeError("未安装 pycryptodome，无法生成 encryptData")
    key_buf = str(key).encode("utf-8")
    iv_buf = str(iv).encode("utf-8")
    if len(key_buf) not in (16, 24, 32):
        raise RuntimeError(f"encrypt_key长度异常: {len(key_buf)}")
    if len(iv_buf) != 16:
        raise RuntimeError(f"iv长度异常: {len(iv_buf)}")
    cipher = AES.new(key_buf, AES.MODE_CBC, iv_buf)
    return cipher.encrypt(_aes_pad(text.encode("utf-8"), AES.block_size)).hex()


def get_encrypt_key(state: Dict[str, Any]) -> Dict[str, Any]:
    """encryptData 密钥必须取【习酒】appid 的（服务端按 AppID 头找对应 appid 的用户密钥解密）。"""
    openid = state.get("openid")
    if not openid:
        raise RuntimeError("缺少 openid，无法生成 encryptData（请配置 JUNPINHUI_OPENIDS）")
    try:
        response = direct_session().post(
            ENCRYPTKEY_URL,
            json={"appid": APPID, "openid": openid},
            headers={"auth": WX_AUTH},
            timeout=30,
        )
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"wx_server 获取 encryptkey 失败: {exc}")

    if not data.get("status"):
        raise RuntimeError(data.get("message") or "wx_server 获取 encryptkey 失败")
    info = data.get("data") or {}
    encrypt_key = info.get("encryptKey") or info.get("encrypt_key")
    iv = info.get("iv")
    version = info.get("version")
    if not encrypt_key or not iv or version is None:
        raise RuntimeError(f"wx_server encryptkey 缺少必要字段: {json_preview(data, 300)}")
    return {"encryptKey": str(encrypt_key), "iv": str(iv), "version": version}


def encrypt_data(data: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(data or {})
    key = get_encrypt_key(state)
    payload["ts"] = int(time.time() * 1000)
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    payload["encryptData"] = aes_cbc_pkcs7_hex(plain, key["encryptKey"], key["iv"])
    payload["version"] = key["version"]
    return payload


# ====================== garden 业务请求 ======================
def ensure_garden(state: Dict[str, Any], server: str, proxies: Dict[str, str] | None) -> None:
    if not (state.get("session") or {}).get("authorizedToken"):
        session, raw_login = garden_login(server, proxies)
        state["session"] = session
        if raw_login is not None:
            state["raw_login"] = raw_login


def garden_request(
    method: str,
    server: str,
    path: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    params: Dict[str, Any] | None = None,
    payload: Dict[str, Any] | None = None,
    encrypted: bool = False,
) -> Any:
    """garden 接口统一入口：失败按源脚本重登一次，仍失败抛错并附加根因提示。"""
    ensure_garden(state, server, proxies)
    session = state["session"]

    req_params, req_payload = params, payload
    if encrypted:
        enc = encrypt_data(payload if method == "POST" else (params or {}), state)
        if method == "POST":
            req_payload = enc
        else:
            req_params = enc

    def call() -> Dict[str, Any]:
        url = f"{GARDEN_BASE}{path}"
        if method == "POST":
            return api_post(server, url, session, proxies, req_payload or {})
        return api_get(server, url, session, proxies, params=req_params)

    resp = call()
    if not ok_code(resp) and state.get("openid"):
        msg_text = str(resp.get("message") or "") + str(resp.get("msg") or "")
        if RELOGIN_PATTERN.search(msg_text):
            print("⚠️ [会话] garden 会话疑似失效，重新登录")
            session, _ = garden_login(server, proxies)
            state["session"] = session
            resp = call()

    if not ok_code(resp):
        msg = (
            str(resp.get("message") or "")
            or str(resp.get("msg") or "")
            or str(resp.get("errMsg") or "")
            or json_preview(resp, 500)
        )
        if encrypted:
            if ENCRYPT_HINT_PATTERN.search(msg):
                raise RuntimeError(f"{msg} ← encryptData 校验失败：密钥必须取 {APPID}(习酒) 的，见文件头说明")
            if SLIDER_PATTERN.search(msg):
                raise RuntimeError(f"{msg} ← 触发滑块验证，按规则不绕，请在小程序里手动过一次")
        raise RuntimeError(f"{path} 失败: {msg}")

    return resp.get("data")


def garden_get(
    server: str,
    path: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    params: Dict[str, Any] | None = None,
    encrypted: bool = False,
) -> Any:
    return garden_request("GET", server, path, state, proxies, params=params, encrypted=encrypted)


def garden_post(
    server: str,
    path: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    payload: Dict[str, Any] | None = None,
    encrypted: bool = False,
) -> Any:
    return garden_request("POST", server, path, state, proxies, payload=payload, encrypted=encrypted)


# ====================== 数据辅助 ======================
def listify(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("list", "data", "records", "items", "rows"):
        if isinstance(value.get(key), list):
            return [item for item in value[key] if isinstance(item, dict)]
    return []


def pick_id(item: Dict[str, Any]) -> Any:
    for key in ("id", "sorghum_id", "sorghumId", "member_sorghum_id", "memberSorghumId", "land_id", "landId"):
        if item.get(key) is not None and item.get(key) != "":
            return item[key]
    return None


def land_no(item: Dict[str, Any]) -> str:
    value = item.get("serial_number")
    if value is None:
        value = item.get("serialNumber")
    if value is None:
        value = pick_id(item)
    if value is None:
        value = "?"
    return str(value)


def land_status(item: Dict[str, Any]) -> int:
    try:
        return int(float(item.get("status", -1)))
    except (TypeError, ValueError):
        return -1


def is_plantable(item: Dict[str, Any]) -> bool:
    return bool(pick_id(item)) and land_status(item) == 0


def is_growing(item: Dict[str, Any]) -> bool:
    status = land_status(item)
    return bool(pick_id(item)) and status > 0 and status not in (10, 11)


def is_harvestable(item: Dict[str, Any]) -> bool:
    return bool(pick_id(item)) and land_status(item) in (10, 11)


def is_completed(task: Dict[str, Any]) -> bool:
    for key in ("is_complete", "isComplete", "complete", "status_complete"):
        if task.get(key) is not None:
            try:
                return int(float(task.get(key))) == 1
            except (TypeError, ValueError):
                return False
    return False


def land_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    ident = pick_id(item)
    return {
        "id": ident,
        "sorghum_id": ident,
        "member_sorghum_id": ident,
        "land_id": ident,
    }


# ====================== 君品荟业务 ======================
def query_member(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> Dict[str, Any]:
    data = garden_get(server, MEMBER_INFO_PATH, state, proxies)
    member = data if isinstance(data, dict) else {}
    nick = member.get("nick_name") or mask(str(member.get("member_id") or "")) or "未知"
    print(
        f"👤 [会员] {nick}，积分{member.get('integration', '未知')}，"
        f"水滴{member.get('water', 0)}，有机肥{member.get('manure', 0)}，种子{member.get('sorghum', 0)}"
    )
    return member


def daily_sign(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
    try:
        data = garden_post(server, SIGN_PATH, state, proxies, payload={}, encrypted=True)
        text = data if isinstance(data, str) else json_preview(data or "ok", 200)
        msg = f"签到成功: {text}"
        print(f"✅ [签到] {msg}")
        return msg
    except Exception as exc:
        msg = f"签到失败: {exc}"
        print(f"⚠️ [签到] {msg}")
        return msg


def query_farm(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> List[Dict[str, Any]]:
    try:
        data = garden_get(server, FARM_INDEX_PATH, state, proxies)
        lands = listify(data)
        summary = " ".join(
            f"#{land.get('serial_number') or land.get('id') or '?'}:{land.get('status') or '?'}"
            for land in lands
        )
        print(f"🌾 [农场] 地块 {len(lands)} 块 {summary}".strip())
        return lands
    except Exception as exc:
        print(f"⚠️ [农场] 查询地块失败: {exc}")
        return []


def harvest_farm(
    server: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    lands: List[Dict[str, Any]],
) -> bool:
    candidates = [land for land in lands if is_harvestable(land)]
    if not candidates:
        return False

    try:
        data = garden_get(server, HARVEST_ALL_PATH, state, proxies, encrypted=True)
        print(f"✅ [农场] 一键收获成功: {json_preview(data or 'ok', 200)}")
        return True
    except Exception as exc:
        print(f"⚠️ [农场] 一键收获失败，尝试单块收获: {exc}")

    acted = False
    for land in candidates:
        try:
            data = garden_post(server, HARVEST_PATH, state, proxies, payload=land_payload(land), encrypted=True)
            print(f"✅ [农场] 收获成功: 地块{land_no(land)} {json_preview(data or 'ok', 200)}")
            acted = True
        except Exception as exc:
            print(f"❌ [农场] 收获失败[地块{land_no(land)}]: {exc}")
        sleep(random.uniform(0.5, 1.2))
    return acted


def seed_farm(
    server: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    lands: List[Dict[str, Any]],
) -> bool:
    seed_count = 0
    try:
        seed_count = int(float(state.get("member", {}).get("sorghum") or 0))
    except (TypeError, ValueError):
        seed_count = 0
    if seed_count <= 0:
        print("⚠️ [农场] 无可用种子，跳过种植")
        return False

    candidates = [land for land in lands if is_plantable(land)]
    if not candidates:
        print("⚠️ [农场] 未识别到可种植地块")
        return False

    acted = False
    limit = min(seed_count, len(candidates))
    for land in candidates[:limit]:
        try:
            data = garden_post(server, SEED_PATH, state, proxies, payload=land_payload(land), encrypted=True)
            print(f"✅ [农场] 种植成功: 地块{land_no(land)} {json_preview(data or 'ok', 200)}")
            acted = True
        except Exception as exc:
            print(f"❌ [农场] 种植失败[地块{land_no(land)}]: {exc}")
        sleep(random.uniform(0.5, 1.2))
    return acted


def water_farm(
    server: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    lands: List[Dict[str, Any]],
) -> bool:
    water_count = 0
    try:
        water_count = int(float(state.get("member", {}).get("water") or 0))
    except (TypeError, ValueError):
        water_count = 0
    if water_count <= 0:
        print("⚠️ [农场] 无可用水滴，跳过浇水")
        return False

    candidates = [land for land in lands if is_growing(land)]
    if not candidates:
        print("⚠️ [农场] 未识别到可浇水地块")
        return False

    acted = False
    limit = min(water_count, len(candidates))
    for land in candidates[:limit]:
        try:
            data = garden_post(server, WATER_PATH, state, proxies, payload=land_payload(land), encrypted=True)
            print(f"✅ [农场] 浇水成功: 地块{land_no(land)} {json_preview(data or 'ok', 200)}")
            acted = True
        except Exception as exc:
            print(f"❌ [农场] 浇水失败[地块{land_no(land)}]: {exc}")
        sleep(random.uniform(0.5, 1.2))
    return acted


def manure_farm(
    server: str,
    state: Dict[str, Any],
    proxies: Dict[str, str] | None,
    lands: List[Dict[str, Any]],
) -> bool:
    manure_count = 0
    try:
        manure_count = int(float(state.get("member", {}).get("manure") or 0))
    except (TypeError, ValueError):
        manure_count = 0
    if manure_count <= 0:
        print("⚠️ [农场] 无可用有机肥，跳过施肥/养护")
        return False

    candidates = [land for land in lands if is_growing(land)]
    if not candidates:
        print("⚠️ [农场] 未识别到可施肥/养护地块")
        return False

    acted = False
    limit = min(manure_count, len(candidates))
    for land in candidates[:limit]:
        try:
            data = garden_post(server, MANURE_PATH, state, proxies, payload=land_payload(land), encrypted=True)
            print(f"✅ [农场] 施肥/养护成功: 地块{land_no(land)} {json_preview(data or 'ok', 200)}")
            acted = True
        except Exception as exc:
            print(f"❌ [农场] 施肥/养护失败[地块{land_no(land)}]: {exc}")
        sleep(random.uniform(0.5, 1.2))
    return acted


def run_farm_automation(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
    any_action = False
    for round_no in range(1, FARM_ROUNDS + 1):
        print(f"🌾 [农场] 自动化第 {round_no} 轮")
        state["member"] = query_member(server, state, proxies)
        lands = query_farm(server, state, proxies)

        harvested = harvest_farm(server, state, proxies, lands)
        if harvested:
            any_action = True
            state["member"] = query_member(server, state, proxies)
            lands = query_farm(server, state, proxies)

        seeded = seed_farm(server, state, proxies, lands)
        if seeded:
            any_action = True
            state["member"] = query_member(server, state, proxies)
            lands = query_farm(server, state, proxies)

        watered = water_farm(server, state, proxies, lands)
        if watered:
            any_action = True
            state["member"] = query_member(server, state, proxies)
            lands = query_farm(server, state, proxies)

        manured = manure_farm(server, state, proxies, lands)
        if manured:
            any_action = True
            state["member"] = query_member(server, state, proxies)
            query_farm(server, state, proxies)

        if not (harvested or seeded or watered or manured):
            break
        sleep(random.uniform(0.8, 1.6))

    if not any_action:
        print("🌾 [农场] 暂无可执行动作")
        return "暂无可执行动作"
    return "已执行收获/种植/浇水/施肥"


def query_tasks(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> List[Dict[str, Any]]:
    try:
        data = garden_get(server, TASKS_PATH, state, proxies)
        tasks = listify(data)
        if not tasks:
            print("📋 [任务] 未查询到任务列表")
            return []
        detail = "，".join(
            f"{task.get('name') or task.get('code') or task.get('id')}:{'已完成' if is_completed(task) else '未完成'}"
            for task in tasks
        )
        print(f"📋 [任务] {detail}")
        return tasks
    except Exception as exc:
        print(f"⚠️ [任务] 查询任务失败: {exc}")
        return []


def do_share_task(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> None:
    try:
        data = garden_post(server, SHARE_TASK_PATH, state, proxies, payload={}, encrypted=True)
        print(f"✅ [任务] 分享任务完成: {json_preview(data or 'ok', 200)}")
    except Exception as exc:
        print(f"⚠️ [任务] 分享任务失败: {exc}")


def do_question_task(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> None:
    try:
        questions = listify(garden_get(server, QUESTION_LIST_PATH, state, proxies))
        if not questions:
            print("📋 [任务] 每日一答无题目")
            return
        for question in questions:
            qid = question.get("id")
            answer = question.get("answer")
            if not qid or not answer:
                continue
            try:
                data = garden_get(
                    server,
                    QUESTION_ANSWER_PATH,
                    state,
                    proxies,
                    params={"question_id": qid, "answer": answer},
                    encrypted=True,
                )
                title = json_preview(question.get("title"), 45) if question.get("title") else qid
                print(f"✅ [任务] 每日一答完成: {title} => {json_preview(data or 'ok', 200)}")
            except Exception as exc:
                print(f"❌ [任务] 每日一答提交失败[{qid}]: {exc}")
            sleep(random.uniform(0.5, 1.2))
    except Exception as exc:
        print(f"⚠️ [任务] 每日一答失败: {exc}")


def do_reality_task(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> None:
    try:
        data = garden_get(server, REALITY_REWARD_PATH, state, proxies)
        print(f"✅ [任务] 实景相册任务: {json_preview(data or 'ok', 200)}")
    except Exception as exc:
        print(f"⚠️ [任务] 实景相册任务失败: {exc}")


def do_complete_info_task(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> None:
    try:
        data = garden_get(server, COMPLETE_INFO_PATH, state, proxies)
        print(f"✅ [任务] 完善信息任务: {json_preview(data or 'ok', 200)}")
    except Exception as exc:
        print(f"⚠️ [任务] 完善信息任务失败: {exc}")


def do_subscribe_prize(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None) -> None:
    try:
        data = garden_get(server, SUBSCRIBE_PRIZE_PATH, state, proxies)
        print(f"✅ [任务] 订阅奖励: {json_preview(data or 'ok', 200)}")
    except Exception as exc:
        print(f"⚠️ [任务] 订阅奖励失败: {exc}")


def do_tasks(server: str, state: Dict[str, Any], proxies: Dict[str, str] | None, tasks: List[Dict[str, Any]]) -> int:
    pending = [task for task in tasks if not is_completed(task)]
    if not pending:
        print("📋 [任务] 暂无未完成任务")
        return 0

    for task in pending:
        code = str(task.get("code") or "")
        name = task.get("name") or code or task.get("id")
        try:
            if code == "answer_survey":
                do_question_task(server, state, proxies)
            elif code == "garden_share":
                do_share_task(server, state, proxies)
            elif code == "view_organic_sorghum":
                do_reality_task(server, state, proxies)
            elif code == "complete_member_info":
                do_complete_info_task(server, state, proxies)
            elif re.search(r"subscribe", code, re.I):
                do_subscribe_prize(server, state, proxies)
            else:
                print(f"⚠️ [任务] 未适配任务: {name}")
        except Exception as exc:
            print(f"❌ [任务] 任务处理失败[{name}]: {exc}")
        sleep(random.uniform(0.5, 1.2))
    return len(pending)


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "user": "-",
        "points": "-",
        "signMsg": "-",
        "tasksMsg": "-",
        "farmMsg": "-",
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

    session, raw_login = login_with_cache(server, proxies)
    if not session:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(session["authorizedToken"])

    state: Dict[str, Any] = {
        "session": session,
        "raw_login": raw_login,
        "member": {},
        "openid": "",
    }
    openid = resolve_openid(index, session, raw_login)
    state["openid"] = openid
    if openid:
        print(f"🔑 [openid] {mask(openid)}")
    else:
        print("⚠️ [openid] 未获取到 openid，加密接口将失败（请配置 JUNPINHUI_OPENIDS）")

    try:
        state["member"] = query_member(server, state, proxies)
        nick = state["member"].get("nick_name") or mask(str(state["member"].get("member_id") or "")) or "未知"
        result["user"] = nick
        result["points"] = str(state["member"].get("integration", "未知"))

        result["signMsg"] = daily_sign(server, state, proxies)

        tasks = query_tasks(server, state, proxies)
        done_count = do_tasks(server, state, proxies, tasks)
        result["tasksMsg"] = f"任务 {len(tasks)} 个，未完成已处理 {done_count} 个"

        result["farmMsg"] = run_farm_automation(server, state, proxies)

        final_member = query_member(server, state, proxies)
        result["points"] = str(final_member.get("integration", result["points"]))
        query_farm(server, state, proxies)

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍶 君品荟酒谷之旅动态 code 任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
👤 会员：{res["user"]}
💰 积分：{res["points"]}
📝 签到：{res["signMsg"]}
📋 任务：{res["tasksMsg"]}
🌾 农场：{res["farmMsg"]}
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
                "user": "-",
                "points": "-",
                "signMsg": "-",
                "tasksMsg": "-",
                "farmMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 君品荟酒谷之旅任务执行完成                ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🍶 君品荟酒谷之旅任务完成", build_notify(results))


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
