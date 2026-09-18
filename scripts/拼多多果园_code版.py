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


"""拼多多果园(多多果园)动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. /login 使用 code 换 access_token（Cookie 鉴权，含 Set-Cookie 回收）
  3. 每日签到（type=201811，tubetoken 必需）
  4. 附带浇水（水滴>=10 自动浇，最多 20 次，非核心）
  5. 风控识别：54002 人机验证 / 43042 风控拦截，明示无法绕过
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  PDDGY_MAX_WATER   浇水上限次数，默认 20

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

APP_NAME = "拼多多果园小程序"
APPID = "wx32540bd863b27570"

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

LOG_NAME = "拼多多果园"
LOG_ICON = "🍑"

XCX_VERSION = "v8.6.21"
PDD_APP_ID = 33
API_BASE = "https://api.pinduoduo.com"
ORCHARD_BASE = "https://mobile.yangkeduo.com"
MANOR_BASE = ORCHARD_BASE + "/proxy/api/api"
CHECKIN_TYPE = 201811
MAX_WATER_TIMES = int(os.getenv("PDDGY_MAX_WATER", "20"))

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pddgycookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF XWEB/19895 miniProgram/wx32540bd863b27570"
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



def as_obj(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return {}
    return {}


def extract_uid(cookie_str: str) -> str:
    m = __import__("re").search(r"pdd_user_id=(\d+)", cookie_str or "")
    return m.group(1) if m else ""


def ok_code(res: Dict[str, Any]) -> bool:
    if not res:
        return False
    if res.get("success") is True:
        return True
    if res.get("error_code") in (0, "0"):
        return True
    if res.get("code") == 0:
        return True
    return False


def resp_msg(res: Dict[str, Any]) -> str:
    if not res:
        return ""
    return res.get("error_msg") or res.get("msg") or res.get("message") or res.get("errorMsg") or ""


class AccountSession:
    """单个 code 服务地址的会话: Cookie 鉴权（PDDAccessToken + pdd_user_id）+ tubetoken"""

    def __init__(self, server: str):
        self.server = server
        self.cookie_str = ""
        self.pdduid = ""
        self.tubetoken = ""
        self.water = 0
        self.blocked = False

    def login(self, proxies: Dict[str, str] | None) -> None:
        code = get_code(self.server)
        if not code:
            raise RuntimeError("code 服务未返回 code")

        print("🔐 [登录] 使用 code 换 access_token")
        login_body = {
            "code": code,
            "has_auth": False,
            "app_id": PDD_APP_ID,
            "support_enhance_type": 3,
            "xcx_version": XCX_VERSION,
        }
        response = request_with_proxy(
            "POST", f"{API_BASE}/login",
            data=json.dumps(login_body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Referer": f"https://servicewechat.com/{APPID}/1840/page-frame.html",
                "x-xcx-queries": f"mini_program_name=pdd;mp_theme_version={XCX_VERSION}",
                "xweb_xhr": "1",
            },
            proxies=proxies, server=self.server,
        )
        result = as_obj(response.text)
        print(f"🔔 [登录] 登录返回: {json_preview(result, 260)}")

        # 风控拦截：无 anti-content 无法绕过（滑块/图形验证码）
        ec = int(result.get("error_code") or 0)
        if ec == 54002:
            self.blocked = True
            raise RuntimeError("BLOCKED:登录触发54002人机验证(滑块/图形验证码)，无 anti-content 无法绕过")
        if ec == 43042:
            self.blocked = True
            raise RuntimeError("BLOCKED:43042风控验证失败，当前账号被风控拦截")

        root = result.get("data") or result
        access_token = root.get("access_token") or root.get("token")
        uid = str(root.get("uid") or root.get("user_id") or "")
        uin = root.get("uin") or ""
        acid = root.get("acid") or ""
        if not uid or not access_token:
            msg = resp_msg(result) or json_preview(result, 200)
            if __import__("re").search(r"验证|风控|拦截|滑块|verify|forbidden|安全", str(msg), __import__("re").IGNORECASE) or ec == 40001:
                self.blocked = True
                raise RuntimeError(f"BLOCKED:登录被风控(error_code={ec}) {msg}")
            raise RuntimeError(f"登录未返回 uid/access_token: {json_preview(result, 200)}")

        # 回收 Set-Cookie（requests 的 cookie jar 等价于取每条 Set-Cookie 的首个 k=v）
        set_cookie_str = "; ".join(f"{c.name}={c.value}" for c in response.cookies)
        parts = [f"PDDAccessToken={access_token}", f"pdd_user_id={uid}"]
        if uin:
            parts.append(f"pdd_user_uin={uin}")
        if acid:
            parts.append(f"acid={acid}")
        if set_cookie_str:
            parts.append(set_cookie_str)

        self.cookie_str = "; ".join(parts)
        self.pdduid = uid
        cache = load_token_cache()
        cache[self.server] = {"cookieStr": self.cookie_str, "uid": uid, "updateTime": datetime.now().isoformat()}
        save_token_cache(cache)
        print(f"✅ [登录] 登录成功 uid={uid}")

    def manor_post(self, api_path: str, body: Dict[str, Any] | None,
                   proxies: Dict[str, str] | None) -> Dict[str, Any]:
        sep = "&" if "?" in api_path else "?"
        response = request_with_proxy(
            "POST",
            f"{MANOR_BASE}{api_path}{sep}pdduid={self.pdduid}",
            data=json.dumps(body or {}, ensure_ascii=False).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": ORCHARD_BASE,
                "Referer": f"{ORCHARD_BASE}/garden_index_lz_0.html",
                "Cookie": self.cookie_str,
            },
            proxies=proxies, server=self.server,
        )
        return as_obj(response.text)

    def get_home_page(self, proxies: Dict[str, str] | None) -> bool:
        """首页：刷新 tubetoken + 水滴，同时用作 Cookie 有效性探测"""
        body = {
            "mission_type": 0, "fun_id": "wechat_app_home", "message_source": None,
            "page_type": "HOME_PAGE", "push_source_mission_type": 0, "fruit_config_version": "",
            "unlock_scene_version": "", "app_home_click_icon_type": None, "tubetoken": self.tubetoken,
            "push_act_source": None, "need_show_home_popup": True, "fun_pl": 2,
        }
        result = self.manor_post("/manor-query/proxy/home/page", body, proxies)
        if not result:
            return False
        # 接口把业务字段嵌在 user_manor_info 里(顶层已不返回)
        info = result.get("user_manor_info") or result
        error_code = int(info.get("error_code") or result.get("error_code") or 0)
        if error_code == 40001:
            print("⚠️ [首页] 首页校验失败(40001)，Cookie 可能已过期")
            return False
        if info.get("tubetoken"):
            self.tubetoken = info["tubetoken"]
        if info.get("water_amount") is not None:
            self.water = info["water_amount"]
        return bool(self.tubetoken)

    def get_water(self, proxies: Dict[str, str] | None) -> int:
        result = self.manor_post("/manor-gateway/manor/query/user/water", {"is_back": 1}, proxies)
        return int(result.get("water_amount") or 0)

    def checkin(self, proxies: Dict[str, str] | None) -> Dict[str, Any]:
        body = {
            "type": CHECKIN_TYPE,
            "params": {"ui_id": 3, "type": 2},
            "fun_id": "wechat_app_home",
            "tubetoken": self.tubetoken,
            "fun_pl": 2,
        }
        res = self.manor_post("/manor/common/apply/activity", body, proxies)
        if ok_code(res):
            reward = res.get("water") or res.get("reward_amount") or safe_data(res).get("water") or ""
            print(f"✅ [签到] 签到成功{f'，+{reward}水滴' if reward else ''}")
            return {"ok": True, "msg": f"签到成功{f'，+{reward}水滴' if reward else ''}"}
        msg = resp_msg(res) or json_preview(res, 200)
        if __import__("re").search(r"已签|签到过|重复|已领取|已完成|already|repeat", str(msg), __import__("re").IGNORECASE):
            print(f"✅ [签到] 今日已签到（{msg}）")
            return {"ok": True, "msg": f"今日已签到（{msg}）"}
        if __import__("re").search(r"验证|风控|拦截|滑块|verify|forbidden", str(msg), __import__("re").IGNORECASE) or int(res.get("error_code") or 0) == 54002:
            self.blocked = True
            print(f"⛔ [签到] 签到被风控拦截: {msg}")
            return {"ok": False, "blocked": True, "msg": msg}
        print(f"❌ [签到] 签到失败: {msg}")
        return {"ok": False, "msg": msg}

    def water_tree(self, proxies: Dict[str, str] | None) -> str:
        """附带浇水（非核心：失败/风控都不影响签到判定）"""
        try:
            water = self.get_water(proxies)
            if water < 10:
                print(f"💧 [浇水] 水滴 {water} 不足10，跳过")
                return f"水滴 {water} 不足10，跳过"
            count = min(MAX_WATER_TIMES, water // 10)
            watered = 0
            curr = water
            for i in range(count):
                body = {
                    "atw": True, "location_auth": False, "last_stay_time": random.randint(10, 49),
                    "can_trigger_random_mission": False, "product_scene": 0, "minor": False,
                    "ext_params": {"can_trigger201824": True}, "mission_type": 0, "cost_water_amount": 10,
                    "merge_cost": False, "fun_id": "wechat_app_home", "lower_end_device": False,
                    "cost_water_competition_in_scene_icon": False, "is_small_screen": True,
                    "tubetoken": self.tubetoken, "fun_pl": 2,
                }
                res = self.manor_post("/manor/water/cost", body, proxies)
                left = res.get("now_water_amount")
                if left is not None and left < curr:
                    curr = left
                    watered += 1
                    if left < 10:
                        break
                    sleep(random.uniform(0.2, 0.4))
                else:
                    break
            print(f"💧 [浇水] 完成 {watered} 次，剩余水滴 {curr}")
            return f"浇水 {watered} 次，剩余水滴 {curr}"
        except Exception as exc:
            print(f"💧 [浇水] 跳过: {exc}")
            return "浇水跳过"

    def ensure_session(self, proxies: Dict[str, str] | None) -> None:
        cached = load_token_cache().get(self.server) or {}
        if cached.get("cookieStr"):
            self.cookie_str = cached["cookieStr"]
            self.pdduid = cached.get("uid") or extract_uid(self.cookie_str)
            print("✅ [缓存] 使用缓存Cookie")
            if self.get_home_page(proxies):
                print(f"✅ [缓存] 缓存有效，水滴={self.water}")
                return
            print("⚠️ [缓存] 缓存失效，重新登录")
            self.cookie_str = ""
            self.pdduid = ""
            self.tubetoken = ""
        if not self.cookie_str:
            self.login(proxies)
            if not self.pdduid:
                self.pdduid = extract_uid(self.cookie_str)
            if not self.get_home_page(proxies):
                raise RuntimeError("登录后首页加载失败(可能风控/Cookie无效)")
            print(f"💰 [水滴] 当前水滴={self.water}")


EMPTY_RESULT = {
    "server": "-",
    "success": False,
    "proxyStatus": "未使用代理",
    "proxyIp": "-",
    "uid": "-",
    "signMsg": "-",
    "waterMsg": "-",
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
        session.ensure_session(proxies)
        result["uid"] = session.pdduid

        checkin_result = session.checkin(proxies)
        result["signMsg"] = checkin_result.get("msg", "-")
        sleep(random.uniform(0.5, 1.0))
        if not checkin_result.get("blocked"):
            result["waterMsg"] = session.water_tree(proxies)
        result["success"] = bool(checkin_result.get("ok"))
        if session.blocked and not checkin_result.get("ok"):
            result["error"] = "触发拼多多风控(需在小程序内手动过验证，脚本无法绕过)"
        return result

    except RuntimeError as exc:
        msg = str(exc)
        if msg.startswith("BLOCKED"):
            result["error"] = msg.replace("BLOCKED:", "") + "（拼多多重风控，需在小程序内手动过验证，脚本无法绕过）"
            print(f"⛔ [账号] {result['error']}")
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

    content = f"""🍑 拼多多果园四账号任务结果

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
🆔 UID：{res["uid"]}
📝 签到：{res["signMsg"]}
💧 浇水：{res["waterMsg"]}
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
    print(f"║ 🏁 拼多多果园任务执行完成          ║")
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
