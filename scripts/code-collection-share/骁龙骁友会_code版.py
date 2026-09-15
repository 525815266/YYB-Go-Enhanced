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


"""骁龙骁友会动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. /api/user/getOpenId 使用 code 换 userId/sessionKey/openId（三件套放请求头 + md5 签名）
  3. 每日签到（前置点击埋点 + signIn，芯动值+10）
  4. 每日免费抽奖 1 次（前置埋点）
  5. 每日任务：文章点赞 / 阅读5分钟 / VLOG1分钟（阅读/VLOG 需真等时长）
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN      PushPlus token，可选
  QYWX_TOKEN          企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API           品赞代理提取 API，可选
  PROXY_TYPE          http / socks5，默认 http
  WX_XLXYH_READ_TASK  阅读/VLOG 长任务开关，默认 1（置 0 只做签到/抽奖/点赞）

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

import hashlib

APP_NAME = "骁龙骁友会小程序"
APPID = "wx026c06df6adc5d06"

SERVERS = [
    "192.168.31.179:8088",
    "192.168.31.36:8088",
    "192.168.31.88:8088",
    "192.168.31.62:8088",
]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

LOG_NAME = "骁龙骁友会"
LOG_ICON = "🎮"

API_BASE = "https://qualcomm.boysup.cn/qualcomm-app"
PAGE_VERSION = "644"
LUCK_ACTIVITY_ID = 7
READ_SECONDS = 300
VLOG_SECONDS = 60
VLOG_SWITCH_KEY = "task_switch_DAILY_PLAY_VIDEO_1_MINUTES"
READ_TASK = os.getenv("WX_XLXYH_READ_TASK", "1").lower() not in ("0", "false", "no", "off")

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xlxyhcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF "
    "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254162e) XWEB/18151"
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


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ {LOG_ICON} {LOG_NAME:<44}║")
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

        code = str(data.get("code") or "")
        status = data.get("status")
        if data.get("err") != 0 or not code or code == "null" or code == "invalid" or (status is not None and status != "ok"):
            print(f"❌ [授权] code 服务返回无效 code (status={status}, codeType={data.get('codeType')})——该账号微信会话可能已失效，请重新扫码登录 code 服务")
            return None

        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


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



SUCCESS_CODES = ("200", "40003")
CODE_SESSION_EXPIRED = "40001"


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def join_json(data: Dict[str, Any]) -> str:
    return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in data.items())


def request_id() -> str:
    chars = "0123456789abcdef"
    t = [chars[random.randint(0, 15)] for _ in range(36)]
    t[14] = "4"
    t[19] = chars[(3 & int(t[19], 16)) | 8]
    return "".join(t[:8] + t[9:13] + t[14:18] + t[19:23] + t[24:])


def biz_code(result: Dict[str, Any]) -> str:
    return str(result.get("code") if result.get("code") is not None else result.get("state") if result.get("state") is not None else result.get("statusCode") or "")


def is_success(result: Dict[str, Any]) -> bool:
    return biz_code(result) in SUCCESS_CODES


def is_already_done(text: str) -> bool:
    return bool(__import__("re").search(r"已签|已经签|签到过|重复|已完成|已领|already", str(text or ""), __import__("re").IGNORECASE))


class AccountSession:
    """单个 code 服务地址的会话: userId/sessionKey/openId 三件套放请求头 + md5 签名"""

    def __init__(self, server: str):
        self.server = server
        self.user_id = 0
        self.session_key = ""
        self.open_id = ""

    def request(self, endpoint: str, method: str = "GET", data: Dict[str, Any] | None = None,
                with_session: bool = True, retry: bool = True,
                proxies: Dict[str, str] | None = None) -> Dict[str, Any]:
        body = join_json(data or {})
        ts = int(time.time() * 1000)
        rid = request_id()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "userId": str(self.user_id or 0) if with_session else "0",
            "sessionKey": self.session_key or "" if with_session else "",
            "openId": self.open_id or "" if with_session else "",
            "timestamp": str(ts),
            "requestId": rid,
            "sign": md5_hex(body + rid + str(ts)),
            "User-Agent": USER_AGENT,
            "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
            "Accept": "*/*",
            "xweb_xhr": "1",
        }
        url = f"{API_BASE}{endpoint}"
        if method == "GET":
            if body:
                url += f"?{body}"
            kwargs: Dict[str, Any] = {"headers": headers, "timeout": REQUEST_TIMEOUT}
            if proxies:
                kwargs["proxies"] = proxies
            try:
                response = requests.get(url, **kwargs)
            except Exception:
                if not proxies or not ENABLE_DIRECT_FALLBACK:
                    raise
                kwargs.pop("proxies", None)
                response = requests.get(url, **kwargs)
        else:
            kwargs = {"headers": headers, "timeout": REQUEST_TIMEOUT,
                      "data": body.encode("utf-8")}
            if proxies:
                kwargs["proxies"] = proxies
            try:
                response = requests.post(url, **kwargs)
            except Exception:
                if not proxies or not ENABLE_DIRECT_FALLBACK:
                    raise
                kwargs.pop("proxies", None)
                response = requests.post(url, **kwargs)
        try:
            return response.json()
        except Exception:
            return {"code": -1, "message": f"JSON解析失败/HTTP {response.status_code}: {response.text[:200]}"}

    def set_cached_token(self) -> None:
        cache = load_token_cache()
        cache[self.server] = {"userId": self.user_id, "sessionKey": self.session_key,
                              "openId": self.open_id, "updateTime": datetime.now().isoformat()}
        save_token_cache(cache)

    def login(self, proxies: Dict[str, str] | None) -> None:
        code = get_code(self.server)
        if not code:
            raise RuntimeError("code 服务未返回 code")

        print("🔐 [登录] 使用 code 换 userId/sessionKey/openId")
        result = self.request("/api/user/getOpenId", method="POST", data={"code": code},
                              with_session=False, retry=False, proxies=proxies)
        if not is_success(result):
            raise RuntimeError(f"getOpenId 失败: {result.get('message') or biz_code(result)}")
        data = safe_data(result) or {}
        user = data.get("userInfo") or {}
        self.user_id = int(user.get("id") or 0)
        if not self.user_id:
            raise RuntimeError("NO_ACCOUNT:该微信号还没在骁友会注册（userInfo.id=0），先在小程序里完成注册/授权手机号")
        self.session_key = str(data.get("sessionKey") or "")
        self.open_id = str(data.get("openId") or "")
        self.set_cached_token()
        print(f"✅ [登录] 登录成功 userId={self.user_id}")

    def ensure_login(self, proxies: Dict[str, str] | None) -> None:
        cached = load_token_cache().get(self.server) or {}
        if cached.get("sessionKey") and cached.get("userId"):
            self.user_id = int(cached["userId"])
            self.session_key = cached["sessionKey"]
            self.open_id = cached.get("openId", "")
            check = self.request("/api/user/info", data={"userId": self.user_id}, retry=False, proxies=proxies)
            if is_success(check) and (safe_data(check) or {}).get("id"):
                print("✅ [缓存] 使用缓存会话")
                return
            print("⚠️ [缓存] 缓存会话失效，重新登录")
        self.login(proxies)

    def bury_point(self, page: str, proxies: Dict[str, str] | None) -> bool:
        pages = {
            "sign": {"urlPath": "pages/task-center/index", "urlName": "任务中心", "elementName": "每日签到"},
            "wheel": {"urlPath": "pages/wheel/index", "urlName": "幸运大转盘", "elementName": "立即抽奖"},
        }
        p = pages[page]
        result = self.request("/api/buryPointApp/save", method="POST", data={
            "userId": self.user_id,
            "openId": self.open_id or "",
            "activitySource": "Xcx_MeiRiRenWu",
            "urlPath": p["urlPath"], "urlName": p["urlName"],
            "elementName": p["elementName"], "elementType": "页面", "eventNameEn": "MPClick",
        }, proxies=proxies)
        return is_success(result)

    def sign_task(self, proxies: Dict[str, str] | None) -> str:
        sign_list = self.request("/api/user/signList", data={"userId": self.user_id}, proxies=proxies)
        if not is_success(sign_list):
            msg = f"读取签到状态失败: {sign_list.get('message') or biz_code(sign_list)}"
            print(f"❌ [签到] {msg}")
            return msg
        info = safe_data(sign_list) or {}
        if int(info.get("isSignToday") or 0) == 1:
            msg = f"今日已签到（本月连续 {info.get('signContinuityMonth') or 0} 天）"
            print(f"✅ [签到] {msg}")
            return msg
        if not self.bury_point("sign", proxies):
            print("⚠️ [签到] 签到前置埋点没成功，继续试签到（大概率会被拒）")
        # retry:false —— 这里的 40001 不是会话过期，是前置埋点没到位，重登没用
        result = self.request("/api/user/signIn", data={"userId": self.user_id}, retry=False, proxies=proxies)
        if is_success(result) and result.get("data"):
            msg = f"签到成功 芯动值+{(result.get('data') or {}).get('coreCoin', 0)}"
            print(f"✅ [签到] {msg}")
            return msg
        if is_already_done(result.get("message")):
            print(f"✅ [签到] 今日已签到（{result.get('message')}）")
            return f"今日已签到（{result.get('message')}）"
        if biz_code(result) == CODE_SESSION_EXPIRED:
            msg = "签到回 40001：前置埋点没被服务端认下，稍后重试或在小程序里手点一次"
            print(f"❌ [签到] {msg}")
            return msg
        msg = f"签到失败: {result.get('message') or biz_code(result)}"
        print(f"❌ [签到] {msg}")
        return msg

    def draw_task(self, proxies: Dict[str, str] | None) -> str:
        if not self.bury_point("wheel", proxies):
            print("⚠️ [抽奖] 抽奖前置埋点没成功，继续试（大概率会被拒）")
        sign_list = self.request("/api/luckDraw/list", data={"page": 1, "userId": self.user_id,
                                                             "activityId": LUCK_ACTIVITY_ID}, proxies=proxies)
        if not is_success(sign_list):
            msg = f"读取抽奖信息失败: {sign_list.get('message') or biz_code(sign_list)}"
            print(f"🎡 {msg}")
            return msg
        d = safe_data(sign_list) or {}
        used = int(d.get("luckDrawSumCount") or 0) - int(d.get("luckDrawCount") or 0)
        free = int(d.get("freeCountDay") or 0)
        if int(d.get("luckDrawCount") or 0) <= 0:
            print("🎡 [抽奖] 抽奖次数已用完，跳过")
            return "抽奖次数已用完"
        if used >= free:
            print(f"🎡 [抽奖] 免费次数已用完（今日已抽 {used}/{free}），再抽要扣 {d.get('luckCoreCoin')} 芯动值，跳过")
            return "免费次数已用完"
        result = self.request("/api/luckDraw/getLuck", method="POST",
                              data={"userId": self.user_id, "activityId": LUCK_ACTIVITY_ID}, proxies=proxies)
        if is_success(result) and result.get("data"):
            msg = f"抽奖成功: {(result.get('data') or {}).get('name') or '已中奖'}"
            print(f"🎉 [抽奖] {msg}")
            return msg
        if is_already_done(result.get("message")):
            print(f"🎡 [抽奖] 今日抽奖已完成（{result.get('message')}）")
            return "今日抽奖已完成"
        if __import__("re").search(r"非法请求", str(result.get("message") or "")):
            print("🎡 [抽奖] 抽奖回「非法请求」：前置埋点没被认下，稍后重试")
            return "抽奖待重试"
        msg = f"抽奖失败: {result.get('message') or biz_code(result)}"
        print(f"❌ [抽奖] {msg}")
        return msg

    def fetch_articles(self, proxies: Dict[str, str] | None) -> list:
        result = self.request("/api/home/articles", data={
            "page": 1, "size": 20, "userId": self.user_id, "type": 0,
            "searchDate": "", "articleShowPlace": "骁友资讯列表页",
        }, proxies=proxies)
        if not is_success(result):
            print(f"⚠️ [任务] 读取资讯列表失败: {result.get('message') or biz_code(result)}")
            return []
        records = (safe_data(result) or {}).get("records") or (safe_data(result) or {}).get("list") or []
        return records if isinstance(records, list) else []

    def like_task(self, articles: list, proxies: Dict[str, str] | None) -> str:
        target = next((a for a in articles if int(a.get("isLike") or 0) == 0), None)
        if not target:
            print("👍 [点赞] 列表里的文章都点过赞了，跳过")
            return "已点赞过，跳过"
        result = self.request("/api/article/like", data={"articleId": str(target.get("id")), "userId": self.user_id},
                              proxies=proxies)
        if is_success(result):
            print(f"👍 [点赞] 点赞成功: {str(target.get('title'))[:30]}")
            return "点赞成功"
        if is_already_done(result.get("message")):
            print(f"👍 [点赞] 已点赞（{result.get('message')}）")
            return "已点赞"
        print(f"❌ [点赞] 点赞失败: {result.get('message') or biz_code(result)}")
        return "点赞失败"

    def read_task(self, articles: list, proxies: Dict[str, str] | None) -> str:
        target = next((a for a in articles if int(a.get("lookTimes") or 0) < READ_SECONDS), None)
        if not target:
            print("📖 [阅读] 列表里的文章阅读时长都够了，跳过")
            return "阅读时长已够，跳过"
        print(f"📖 [阅读] 开始阅读: {str(target.get('title'))[:30]}（已读 {target.get('lookTimes') or 0}s）")
        enter = self.request("/api/article/enterReadDaily", method="POST",
                             data={"articleId": target.get("id"), "userId": self.user_id}, proxies=proxies)
        if not is_success(enter):
            print(f"❌ [阅读] 进入阅读失败: {enter.get('message') or biz_code(enter)}")
            return "进入阅读失败"
        wait = READ_SECONDS + 5
        print(f"⏳ [阅读] 停留 {wait}s（源码要求满 5 分钟）")
        sleep(wait)
        exit_resp = self.request("/api/article/exitReadDaily", method="POST",
                                 data={"articleId": target.get("id"), "userId": self.user_id}, proxies=proxies)
        if is_success(exit_resp):
            print("✅ [阅读] 阅读任务已提交")
            return "阅读任务已提交"
        print(f"❌ [阅读] 退出阅读失败: {exit_resp.get('message') or biz_code(exit_resp)}")
        return "退出阅读失败"

    def vlog_task(self, proxies: Dict[str, str] | None) -> str:
        cfg = self.request("/api/sysConfig/detail", method="POST", data={"propertyKey": VLOG_SWITCH_KEY}, proxies=proxies)
        if is_success(cfg) and str((safe_data(cfg) or {}).get("propertyValue")) == "0":
            print("🎬 [VLOG] 任务开关关闭，跳过")
            return "VLOG 开关关闭，跳过"
        vlog_list = self.request("/api/article/vlogList", data={"page": 1, "size": 20, "userId": self.user_id, "sortBy": 1},
                                 proxies=proxies)
        if not is_success(vlog_list):
            print(f"❌ [VLOG] 读取 VLOG 列表失败: {vlog_list.get('message') or biz_code(vlog_list)}")
            return "VLOG 列表失败"
        records = (safe_data(vlog_list) or {}).get("records") or []
        target = next((v for v in records if int(v.get("lookTimes") or 0) < VLOG_SECONDS), None)
        if not target:
            print("🎬 [VLOG] 列表里的 VLOG 观看时长都够了，跳过")
            return "观看时长已够，跳过"
        print(f"🎬 [VLOG] 开始观看: {str(target.get('title'))[:30]}（已看 {target.get('lookTimes') or 0}s）")
        enter = self.request("/api/article/enterReadDaily", method="POST",
                             data={"articleId": target.get("id"), "userId": self.user_id}, proxies=proxies)
        if not is_success(enter):
            print(f"❌ [VLOG] 进入 VLOG 失败: {enter.get('message') or biz_code(enter)}")
            return "进入 VLOG 失败"
        self.request("/api/article/vlogPlay", data={"articleId": target.get("id")}, proxies=proxies)
        wait = VLOG_SECONDS + 5
        print(f"⏳ [VLOG] 停留 {wait}s（源码要求满 1 分钟）")
        sleep(wait)
        exit_resp = self.request("/api/article/exitReadDaily", method="POST",
                                 data={"articleId": target.get("id"), "userId": self.user_id}, proxies=proxies)
        if is_success(exit_resp):
            print("✅ [VLOG] VLOG 任务已提交")
            return "VLOG 任务已提交"
        print(f"❌ [VLOG] 退出 VLOG 失败: {exit_resp.get('message') or biz_code(exit_resp)}")
        return "退出 VLOG 失败"


EMPTY_RESULT = {
    "server": "-",
    "success": False,
    "proxyStatus": "未使用代理",
    "proxyIp": "-",
    "token": "-",
    "signMsg": "-",
    "drawMsg": "-",
    "taskMsg": "-",
    "error": "",
}


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = dict(EMPTY_RESULT)
    result["server"] = server

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    session = AccountSession(server)

    try:
        session.ensure_login(proxies)
        result["token"] = mask(str(session.user_id))
        result["signMsg"] = session.sign_task(proxies)
        result["drawMsg"] = session.draw_task(proxies)
        if READ_TASK:
            articles = session.fetch_articles(proxies)
            like_msg = session.like_task(articles, proxies)
            read_msg = session.read_task(articles, proxies)
            vlog_msg = session.vlog_task(proxies)
            result["taskMsg"] = f"{like_msg} | {read_msg} | {vlog_msg}"
        else:
            result["taskMsg"] = "长任务已关闭"
        result["success"] = "签到成功" in result["signMsg"] or "已签到" in result["signMsg"]
        return result

    except RuntimeError as exc:
        if str(exc).startswith("NO_ACCOUNT"):
            result["error"] = str(exc)[11:]
            print(f"⚠️ [账号] {result['error']}")
            return result
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result
def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🎮 骁龙骁友会四账号任务结果

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
🆔 UserID：{res["token"]}
📝 签到：{res["signMsg"]}
🎡 抽奖：{res["drawMsg"]}
📋 任务：{res["taskMsg"]}
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
            results.append({**EMPTY_RESULT, "server": server, "error": traceback.format_exc().strip()})

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ 🏁 骁龙骁友会任务执行完成          ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus(f"{LOG_ICON} {LOG_NAME}四账号任务完成", build_notify(results))


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
