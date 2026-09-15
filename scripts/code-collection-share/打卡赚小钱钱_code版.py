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
打卡赚小钱钱（芃儒文章）code 版

功能：
  1. 本地 code 服务获取微信 code
  2. 芃儒文章流程（weiqing.lingchuangwang.com，mof_shortvideo 模块）：
     每日签到 / 观看广告任务 / 观看视频任务 / 查询余额 / 满足条件自动提现
  3. 打卡赚小钱钱流程（rr.qq66.cn，bh_rising 模块）：
     每日打卡 5 次（开始-等待-打卡）/ 查询打卡币与余额 / 满足最低金额自动提现
  4. PushPlus 推送
  5. 品赞代理，业务请求优先代理，失败直连兜底

⚠️ 登录接口为推断，未经真机验证，失败请抓包核对
（两个流程源脚本均为抓包 state / token 型，无登录调用，此处按微擎 wxapp 惯例推断 do=login / action=login 接口）

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

import hashlib
import json
import math
import os
import random
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qsl, quote, urlparse

import requests


APP_NAME = "打卡赚小钱钱（芃儒文章）"
APPID = "wxd03a809cfc99930b"

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

# 流程 A：芃儒文章（weiqing mof_shortvideo）
BASE_A = "https://weiqing.lingchuangwang.com/app/index.php"
SECRET_A = "wq_mof_short_video_by_moufer_2020"
# 流程 B：打卡赚小钱钱（weiqing bh_rising）
BASE_B = "https://rr.qq66.cn/app/index.php"
# 源脚本 getSign 的 secret 为 undefined，实际签名串为 c + '&' + 'undefined'，此处如实复刻
SECRET_B = "undefined"

DAILY_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frwz_state.json")

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dkcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541923) XWEB/19823"
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
    print("║ 📝 打卡赚小钱钱（芃儒文章）code 版             ║")
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
    return {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/4/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def extract_field(data: Any, names: List[str]) -> str | None:
    """按候选字段名从响应中提取凭证（data 内层与嵌套 dict 一并查找）"""
    if not isinstance(data, dict):
        return None

    candidates: List[Any] = [data.get(name) for name in names]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([inner.get(name) for name in names])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def get_sign(url: str, extra: Dict[str, Any] | None = None, secret: str = "") -> str:
    """复刻源脚本 getSign: 取 URL 全部 query 参数并合并附加参数，
    按参数名排序去重后以 k=v 用 & 连接（跳过空值），再拼 '&secret' 取 md5。"""
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)

    if extra:
        for name, value in extra.items():
            if name and value != "":
                pairs.append((name, str(value)))

    pairs.sort(key=lambda item: item[0])

    seen = set()
    filtered: List[Tuple[str, str]] = []
    for name, value in pairs:
        if not name or name in seen:
            continue
        seen.add(name)
        filtered.append((name, value))

    concat = "&".join(f"{name}={value}" for name, value in filtered if name and value != "")
    return hashlib.md5(f"{concat}&{secret}".encode("utf-8")).hexdigest()


def build_url_a(do_action: str, state: str, extra: Dict[str, Any] | None = None) -> str:
    """芃儒文章接口 URL（签名后附加原始参数，与源脚本 getUrl 一致）"""
    t = int(time.time() * 1000)
    url = (
        f"{BASE_A}?i=89&t=0&v=1.0.6&from=wxapp&c=entry&a=wxapp&do={do_action}"
        f"&state={state}&m=mof_shortvideo&_vi=76&_t={t}&_ut={t + 1}"
    )
    url += f"&sign={get_sign(url, extra, SECRET_A)}"
    if extra:
        for name, value in extra.items():
            url += f"&{name}={value}"
    return url


def build_url_b(action: str, contr: str, token: str, extra: Dict[str, Any] | None = None) -> str:
    """打卡赚小钱钱接口 URL（签名后附加原始参数，与源脚本 getUrl 一致）"""
    url = (
        f"{BASE_B}?i=157&t=0&v=1.0.1&from=wxapp&c=entry&a=wxapp&do=distribute"
        f"&m=bh_rising&action={action}&contr={contr}&token={token}&version=3.5.4"
    )
    url += f"&sign={get_sign(url, extra, SECRET_B)}"
    if extra:
        for name, value in extra.items():
            url += f"&{name}={value}"
    return url


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


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """使用 code 换取两个流程的凭证（state / token），以 JSON 串作为缓存 token。

    ⚠️ 两个登录端点均为推断（源脚本为抓包 state/token 型，无登录调用）。
    """
    cred: Dict[str, str] = {}
    raw: Dict[str, Any] = {}

    try:
        print("🔐 [登录] 流程A（芃儒文章）使用 code 换 state")
        resp_a = api_get(server, f"{BASE_A}?i=89&t=0&v=1.0.6&from=wxapp&c=entry&a=wxapp&do=login&m=mof_shortvideo&_vi=76&code={quote(code)}", "", proxies)
        raw["loginA"] = resp_a
        state = extract_field(resp_a, ["state", "token", "access_token"])
        if state:
            print(f"✅ [登录] 流程A state 获取成功: {mask(state)}")
            cred["state"] = state
        else:
            print(f"❌ [登录] 流程A 未识别 state 字段: {json_preview(resp_a)}")
    except Exception as exc:
        print(f"❌ [登录] 流程A 请求异常: {exc}")

    try:
        print("🔐 [登录] 流程B（打卡赚小钱钱）使用 code 换 token")
        resp_b = api_get(server, f"{BASE_B}?i=157&t=0&v=1.0.1&from=wxapp&c=entry&a=wxapp&do=distribute&m=bh_rising&action=login&contr=index&code={quote(code)}&version=3.5.4", "", proxies)
        raw["loginB"] = resp_b
        token_b = extract_field(resp_b, ["token", "state", "access_token"])
        if token_b:
            print(f"✅ [登录] 流程B token 获取成功: {mask(token_b)}")
            cred["token"] = token_b
        else:
            print(f"❌ [登录] 流程B 未识别 token 字段: {json_preview(resp_b)}")
    except Exception as exc:
        print(f"❌ [登录] 流程B 请求异常: {exc}")

    if not cred:
        return None, raw

    return json.dumps(cred, ensure_ascii=False), raw


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


def verify_credentials(server: str, cred: Dict[str, str], proxies: Dict[str, str] | None) -> bool:
    """验证缓存凭证：流程A 用任务列表接口，流程B 用信息接口"""
    valid = False

    state = cred.get("state")
    if state:
        try:
            resp = api_get(server, build_url_a("MyScoreTasks", state), "", proxies)
            if resp.get("message") == "ok":
                valid = True
        except Exception as exc:
            print(f"⚠️ [缓存] 流程A 验证异常: {exc}")

    token_b = cred.get("token")
    if token_b:
        try:
            resp = api_get(server, build_url_b("today", "index", token_b), "", proxies)
            if resp.get("info"):
                valid = True
        except Exception as exc:
            print(f"⚠️ [缓存] 流程B 验证异常: {exc}")

    return valid


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存凭证（任务列表/信息接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            cred = json.loads(cache_token)
            if isinstance(cred, dict) and verify_credentials(server, cred, proxies):
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


# ====================== 每日完成状态（对应源脚本 frwz 本地存储） ======================
def load_daily_state() -> Dict[str, Any]:
    try:
        if os.path.exists(DAILY_STATE_FILE):
            with open(DAILY_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_daily_state(state: Dict[str, Any]) -> None:
    try:
        with open(DAILY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"⚠️ [状态] 保存失败: {exc}")


# ====================== 流程 A：芃儒文章 ======================
def a_score_task_sign_in(server: str, state: str, proxies: Dict[str, str] | None) -> Tuple[str, bool]:
    """每日签到，返回 (描述, 是否已完成)"""
    resp = api_get(server, build_url_a("ScoreTaskSignIn", state), "", proxies)
    if resp.get("message") == "ok":
        reward = safe_data(resp).get("reward_score", 0)
        print(f"✅ [签到] 签到成功，积分+{reward}")
        return f"签到成功，积分+{reward}", True
    if "已签到" in str(resp.get("message") or ""):
        print(f"✅ [签到] {resp.get('message')}")
        return str(resp.get("message")), True
    print(f"❌ [签到] 签到失败，{resp.get('message')}")
    return f"签到失败: {resp.get('message')}", False


def a_score_task_video_ad(server: str, state: str, proxies: Dict[str, str] | None) -> None:
    """观看广告任务"""
    resp = api_get(server, build_url_a("ScoreTaskVideoAd", state), "", proxies)
    if resp.get("message") == "ok":
        print(f"✅ [广告] 观看广告成功，积分+{safe_data(resp).get('reward_score', 0)}")
    else:
        print(f"❌ [广告] 观看广告失败，{resp.get('message')}")


def a_score_task_roll_video(server: str, state: str, proxies: Dict[str, str] | None) -> None:
    """观看视频任务"""
    resp = api_get(server, build_url_a("ScoreTaskRollVideo", state), "", proxies)
    if resp.get("message") == "ok":
        print(f"✅ [视频] 观看视频成功，积分+{safe_data(resp).get('reward_score', 0)}")
    else:
        print(f"❌ [视频] 观看视频失败，{resp.get('message')}")


def a_my_score_tasks(server: str, state: str, proxies: Dict[str, str] | None) -> Dict[str, int] | None:
    """获取积分任务列表剩余次数，首次运行模拟一轮任务"""
    def fetch() -> Dict[str, Any] | None:
        resp = api_get(server, build_url_a("MyScoreTasks", state), "", proxies)
        if resp.get("message") != "ok":
            return None
        return safe_data(resp)

    data = fetch()
    if data is None:
        print("❌ [任务] 获取积分任务列表失败")
        return None

    ad = data.get("videoAd")
    sp = data.get("rollVideo")
    if ad is None or sp is None:
        print("🔍 [任务] 检查到为今天首次运行,将模拟第一次运行")
        a_score_task_video_ad(server, state, proxies)
        wait_time = random.uniform(8, 16)
        print(f"⏳ [任务] 等待 {wait_time:.1f}s")
        sleep(wait_time)
        a_score_task_roll_video(server, state, proxies)
        data = fetch() or {}

    ad = data.get("videoAd") or {}
    sp = data.get("rollVideo") or {}
    ad_step = ad.get("step") or 0
    ad_total = ad.get("total_step") or 0
    sp_step = sp.get("step") or 0
    sp_total = sp.get("total_step") or 0
    print(f"📋 [任务] 任务{ad.get('type_text')}({ad_step}/{ad_total})")
    print(f"📋 [任务] 任务{sp.get('type_text')}({sp_step}/{sp_total})")
    return {"ad": int(ad_total) - int(ad_step), "sp": int(sp_total) - int(sp_step)}


def a_withdraw_user_score(server: str, state: str, proxies: Dict[str, str] | None) -> str:
    """查询余额，满足条件自动提现"""
    resp = api_get(server, build_url_a("WithdrawUserScore", state), "", proxies)
    if resp.get("message") != "ok":
        print(f"❌ [余额] 获取余额失败，{resp.get('message')}")
        return "获取余额失败"

    cash = math.floor(to_float(safe_data(resp).get("max")) * 10) / 10
    print(f"💸 [余额] 可提现最大余额：{cash}")
    if cash > 99999:
        print("💸 [提现] 准备提现")
        apply_resp = api_get(server, build_url_a("WithdrawApply", state, {"cash": cash}), "", proxies)
        if apply_resp.get("message") == "ok":
            print("✅ [提现] 提现成功")
            return f"已提现 {cash}"
        print(f"❌ [提现] 提现失败，{apply_resp.get('message')}")
        return f"提现失败: {apply_resp.get('message')}"
    return f"可提现最大余额 {cash}（未达提现条件）"


def flow_a(server: str, state: str, proxies: Dict[str, str] | None) -> Dict[str, str]:
    """芃儒文章每日流程：签到 / 广告任务 / 视频任务 / 余额提现"""
    today = datetime.now().strftime("%Y-%m-%d")
    remark = str(state)[:8]

    daily = load_daily_state()
    entry = daily.setdefault(remark, {}).setdefault(today, {"sign": False, "ad": False, "sp": False})
    if entry.get("sign") and entry.get("ad") and entry.get("sp"):
        print("✅ [流程A] 今日任务已经全部完成啦")
        return {"signMsg": "今日任务已全部完成", "taskMsg": "-"}

    sign_msg, sign_done = a_score_task_sign_in(server, state, proxies)
    if sign_done:
        entry["sign"] = True
        save_daily_state(daily)

    times = a_my_score_tasks(server, state, proxies)
    wait_time = random.uniform(3, 5)
    print(f"⏳ [间隔] 等待 {wait_time:.1f}s")
    sleep(wait_time)

    ad_msg = "-"
    if times:
        for i in range(times.get("ad") or 0):
            wait_time = random.uniform(30, 40)
            print(f"🔁 [广告] 开始第{i + 1}次模拟观看广告，等待 {wait_time:.1f}s...")
            sleep(wait_time)
            a_score_task_video_ad(server, state, proxies)
        entry["ad"] = True
        save_daily_state(daily)

        wait_time = random.uniform(3, 5)
        print(f"⏳ [间隔] 等待 {wait_time:.1f}s")
        sleep(wait_time)

        for j in range(times.get("sp") or 0):
            wait_time = random.uniform(8, 16)
            print(f"🔁 [视频] 开始第{j + 1}次模拟观看视频，等待 {wait_time:.1f}s...")
            sleep(wait_time)
            a_score_task_roll_video(server, state, proxies)
        entry["sp"] = True
        save_daily_state(daily)
        ad_msg = f"广告+{times.get('ad') or 0} 次、视频+{times.get('sp') or 0} 次"
    else:
        ad_msg = "任务列表获取失败"

    wait_time = random.uniform(3, 5)
    print(f"⏳ [间隔] 等待 {wait_time:.1f}s")
    sleep(wait_time)

    money_msg = a_withdraw_user_score(server, state, proxies)
    return {"signMsg": sign_msg, "taskMsg": f"{ad_msg}；{money_msg}"}


# ====================== 流程 B：打卡赚小钱钱 ======================
def b_info(server: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any] | None:
    """获取打卡信息"""
    resp = api_get(server, build_url_b("today", "index", token), "", proxies)
    info = resp.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("today"), dict):
        print(f"❌ [打卡] 获取信息失败，{json_preview(resp, 300)}")
        return None
    today = info.get("today") or {}
    print(
        f"✅ [打卡] 获取信息成功,打卡币：{today.get('currency')}，余额：{today.get('money')}，"
        f"今日打卡次数：{today.get('clock')}"
    )
    return today


def b_start(server: str, token: str, proxies: Dict[str, str] | None) -> bool:
    """开始打卡"""
    resp = api_get(server, build_url_b("addTips", "my", token), "", proxies)
    return resp.get("status") == 1


def b_end(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    """打卡（模拟广告后提交）"""
    resp = api_get(server, build_url_b("sign", "clock", token, {"captcha_code": "", "click_ad": 1}), "", proxies)
    if resp.get("status") == 1:
        print("✅ [打卡] 模拟打卡成功")
        return "模拟打卡成功"
    print(f"❌ [打卡] 模拟打卡失败，原因: {json_preview(resp, 300)}")
    return "模拟打卡失败"


def b_withdraw(server: str, token: str, money: Any, proxies: Dict[str, str] | None) -> str:
    """提现"""
    resp = api_get(server, build_url_b("withdrawals", "my", token, {"money": money}), "", proxies)
    if resp.get("status") == 1:
        print("✅ [提现] 提现成功")
        return f"已提现 {money}"
    print(f"❌ [提现] 提现失败，原因: {json_preview(resp, 300)}")
    return f"提现失败: {json_preview(resp, 200)}"


def flow_b(server: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, str]:
    """打卡赚小钱钱每日流程：打卡 5 次 / 余额提现"""
    today = b_info(server, token, proxies)
    clock_msg = "-"
    if today is not None:
        clock = int(to_float(today.get("clock")))
        if clock < 5:
            for i in range(clock, 5):
                print(f"📝 [打卡] 开始第 {i + 1} 次打卡")
                if b_start(server, token, proxies):
                    wait_time = random.uniform(30, 40)
                    print(f"⏳ [打卡] 等待 {wait_time:.1f}s")
                    sleep(wait_time)
                    b_end(server, token, proxies)
                wait_time = random.uniform(10, 16)
                print(f"⏳ [间隔] 等待 {wait_time:.1f}s")
                sleep(wait_time)
            today = b_info(server, token, proxies)

        if today is not None:
            clock_msg = f"打卡币 {today.get('currency')}，打卡 {today.get('clock')} 次"

    wait_time = random.uniform(3, 5)
    print(f"⏳ [间隔] 等待 {wait_time:.1f}s")
    sleep(wait_time)

    money_msg = "-"
    resp = api_get(server, build_url_b("cash", "my", token), "", proxies)
    if resp.get("status") == 1:
        info = resp.get("info") or {}
        money = info.get("member", {}).get("money") if isinstance(info.get("member"), dict) else None
        least_money = info.get("least_money")
        print(f"✅ [余额] 获取信息成功,余额：{money}，提现最低金额：{least_money}")
        money_msg = f"余额 {money}"
        if money is not None and least_money is not None and to_float(money) >= to_float(least_money):
            money_msg = b_withdraw(server, token, money, proxies)
    else:
        print(f"❌ [余额] 获取信息失败，原因: {json_preview(resp, 300)}")
        money_msg = "获取余额失败"

    return {"clockMsg": clock_msg, "moneyMsg": money_msg}


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "taskMsg": "-",
        "clockMsg": "-",
        "moneyMsg": "-",
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
        cred = json.loads(token)
        if not isinstance(cred, dict):
            cred = {}

        state = cred.get("state")
        token_b = cred.get("token")

        if state:
            wait_time = random.uniform(3, 5)
            print(f"⏳ [间隔] 等待 {wait_time:.1f}s")
            sleep(wait_time)
            flow_a_result = flow_a(server, state, proxies)
            result["signMsg"] = flow_a_result["signMsg"]
            result["taskMsg"] = flow_a_result["taskMsg"]
        else:
            print("⚠️ [流程A] 未获取到 state，跳过芃儒文章流程")

        if token_b:
            wait_time = random.uniform(3, 5)
            print(f"⏳ [间隔] 等待 {wait_time:.1f}s")
            sleep(wait_time)
            flow_b_result = flow_b(server, token_b, proxies)
            result["clockMsg"] = flow_b_result["clockMsg"]
            result["moneyMsg"] = flow_b_result["moneyMsg"]
        else:
            print("⚠️ [流程B] 未获取到 token，跳过打卡赚小钱钱流程")

        result["success"] = bool(state or token_b)
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""📝 打卡赚小钱钱（芃儒文章）任务结果

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
🎯 任务：{res["taskMsg"]}
🕒 打卡：{res["clockMsg"]}
💰 余额：{res["moneyMsg"]}
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
                "clockMsg": "-",
                "moneyMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 打卡赚小钱钱任务执行完成                    ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("📝 打卡赚小钱钱任务完成", build_notify(results))


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
