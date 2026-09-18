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


"""东风日产人车生活动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. /toc-login-service/nissan/v2/user/login/{code} 换 oneid + api_token
  3. 每日签到（wxapi JWT 鉴权，先查签到记录再签）
  4. 查询成长值（ariya SHA512 签名）
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

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
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests

import hashlib
from datetime import datetime, timezone

APP_NAME = "东风日产人车生活小程序"
APPID = "wxe3fd49854884240e"

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

LOG_NAME = "东风日产人车生活"
LOG_ICON = "🚙"

ARIYA_BASE = "https://ariya-api.dongfeng-nissan.com.cn"
WXAPI_BASE = "https://wxapi.dongfeng-nissan.com.cn"
CLIENT_ID = "nissanminiapp"
APP_CODE = "nissan"
APP_SKIN = "NISSANAPP"
PAGE_VERSION = "1285"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dfyccookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a1b)XWEB/14185"
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



def gen_noncestr() -> str:
    """复刻小程序 getNonce：32 位大写 hex，第 13 位（index 12）固定为 4"""
    chars = "0123456789abcdef"
    arr = ["4" if i == 12 else chars[random.randint(0, 15)] for i in range(32)]
    return "".join(arr).upper()


def random_uuid(length: int = 20) -> str:
    chars = "abcdef0123456789"
    return "".join(random.choice(chars) for _ in range(length))


def china_date_str() -> str:
    return datetime.fromtimestamp(time.time() + 8 * 3600, tz=timezone.utc).strftime("%Y-%m-%d")


def dig_data(result: Any) -> Dict[str, Any]:
    """从多层壳里挖登录返回体（rows / data）"""
    if not isinstance(result, dict):
        return {}
    for key in ("rows", "data"):
        value = result.get(key)
        if isinstance(value, dict):
            return value
    return {}


class AccountSession:
    """单个 code 服务地址的会话: oneid + ariya token + wxapi JWT"""

    def __init__(self, server: str):
        self.server = server
        self.wx_uuid = random_uuid()
        self.oneid = ""
        self.token = ""      # ariya 短 hex token（ariya 签名头用）
        self.api_token = ""  # wxapi JWT（signSave 用）
        self.openid = ""

    def login(self) -> None:
        code = get_code(self.server)
        if not code:
            raise RuntimeError("code 服务未返回 code")

        print("🔐 [登录] 使用 code 换 oneid/api_token")
        response = request_with_proxy(
            "GET",
            f"{ARIYA_BASE}/toc-login-service/nissan/v2/user/login/{quote(code)}",
            params={"wxUuid": self.wx_uuid, "sourcecode": "", "smartcode": ""},
            headers={
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
                "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
                "xweb_xhr": "1",
            },
            server=self.server,
        )
        try:
            result = response.json()
        except Exception:
            result = {"raw": response.text[:400]}
        print(f"🔔 [登录] 登录响应: {json_preview(result, 300)}")

        data = dig_data(result)
        self.oneid = str(
            data.get("oneid") or data.get("oneId") or data.get("ly_user_id") or data.get("uuid")
            or data.get("uid") or data.get("userId") or data.get("memberId") or result.get("oneid") or "")
        self.token = str(data.get("token") or data.get("access_token") or data.get("api_token") or result.get("token") or "")
        self.api_token = str(data.get("api_token") or "")
        oid = data.get("openid") or data.get("openId") or ""
        if oid:
            self.openid = str(oid)

        if not self.oneid:
            raise RuntimeError("NO_ACCOUNT:登录未返回 oneid")

        cache = load_token_cache()
        cache[self.server] = {
            "oneid": self.oneid, "token": self.token, "apiToken": self.api_token,
            "openid": self.openid, "updateTime": datetime.now().isoformat(),
        }
        save_token_cache(cache)
        print(f"✅ [登录] 登录成功 oneid={mask(self.oneid)} token={'有' if self.token else '无'} jwt={'有' if self.api_token else '无'}")

    def ensure_login(self) -> None:
        cached = load_token_cache().get(self.server) or {}
        if cached.get("oneid"):
            self.oneid = cached["oneid"]
            self.token = cached.get("token", "")
            self.api_token = cached.get("apiToken", "")
            self.openid = cached.get("openid", "") or self.openid
            print("✅ [缓存] 使用缓存登录态")
            return
        self.login()

    def wxapi_get(self, api_path: str) -> Dict[str, Any]:
        sep = "&" if "?" in api_path else "?"
        response = request_with_proxy(
            "GET",
            f"{WXAPI_BASE}{api_path}{sep}wxUuid={self.wx_uuid}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_token or self.token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
                "urid": self.openid or "",
                "xweb_xhr": "1",
            },
            server=self.server,
        )
        try:
            return response.json()
        except Exception:
            return {"code": -1, "message": f"JSON解析失败: {response.text[:300]}"}

    def ariya_post(self, api_path: str, body: Dict[str, Any] | None) -> Dict[str, Any]:
        ts = int(time.time() * 1000)
        nonce = gen_noncestr()
        rng = "1"
        sign = hashlib.sha512(f"{CLIENT_ID}{ts}{self.token}{nonce}{rng}{self.oneid}".encode("utf-8")).hexdigest()
        response = request_with_proxy(
            "POST",
            f"{ARIYA_BASE}{api_path}",
            json=body,
            headers={
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
                "appCode": APP_CODE, "appSkin": APP_SKIN, "clientid": CLIENT_ID,
                "noncestr": nonce, "oneid": self.oneid, "uuid": self.oneid,
                "range": rng, "sign": sign, "timestamp": str(ts), "token": self.token, "xweb_xhr": "1",
            },
            server=self.server,
        )
        try:
            return response.json()
        except Exception:
            return {"result": "-1", "message": f"JSON解析失败: {response.text[:300]}"}

    def sign(self, retry: bool = True) -> str:
        # 1) 先查今日签到记录（ariya 签名）
        today = china_date_str()
        try:
            sign_list = self.ariya_post(
                "/dfn-growth/rest/ly-mp-growth-service/ly/mgs/checkin/signList",
                {"brandCode": 1, "channel": "2", "startTime": today, "endTime": today})
            if sign_list and str(sign_list.get("result")) == "1":
                for row in sign_list.get("rows") or []:
                    if str(row.get("signTime") or "").startswith(today):
                        print("✅ [签到] 今日已签到")
                        return "今日已签到"
        except Exception:
            pass  # 查询失败不阻塞签到

        # 2) 执行签到（wxapi JWT，GET，无 body）
        res = self.wxapi_get("/api/small/v4/signin/mgs/checkin/signSave")
        code = res.get("code")
        msg = res.get("message") or res.get("msg") or json_preview(res, 200)
        if code == 10000:
            print("✅ [签到] 签到成功")
            return "签到成功"
        if code == 10010 or __import__("re").search(r"已签|签到过|重复|已有签到", str(msg)):
            print(f"✅ [签到] 今日已签到（{msg}）")
            return f"今日已签到（{msg}）"
        if (retry and __import__("re").search(r"token|登录|未授权|失效|过期|未登录|鉴权|unauth|invalid", str(msg), __import__("re").IGNORECASE)) or code == 401:
            print("⚠️ [签到] 会话失效，重新登录后重试")
            cache = load_token_cache()
            cache.pop(self.server, None)
            save_token_cache(cache)
            self.oneid = ""
            self.login()
            return self.sign(retry=False)
        print(f"❌ [签到] 签到失败: {msg}")
        return f"签到失败: {msg}"

    def query_growth(self) -> str:
        try:
            res = self.ariya_post(
                "/dfn-growth/rest/ly-mp-growth-service/ly/mgs/growth/growthvalue/medal", {})
            if res and str(res.get("result")) == "1":
                data = safe_data(res)
                if data.get("growthScore") is not None:
                    level = f"（{data['levelName']}）" if data.get("levelName") else ""
                    print(f"🌱 [成长] 成长值: {data['growthScore']}{level}")
                    return f"成长值 {data['growthScore']}{level}"
        except Exception:
            pass  # 非关键
        return "-"


EMPTY_RESULT = {
    "server": "-",
    "success": False,
    "proxyStatus": "未使用代理",
    "proxyIp": "-",
    "token": "-",
    "signMsg": "-",
    "growthMsg": "-",
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
        session.ensure_login()
        result["token"] = mask(session.oneid)

        result["signMsg"] = session.sign(retry=True)
        result["growthMsg"] = session.query_growth()
        result["success"] = "签到成功" in result["signMsg"] or "已签到" in result["signMsg"]
        return result

    except RuntimeError as exc:
        if str(exc).startswith("NO_ACCOUNT"):
            result["error"] = "该微信号还没在东风日产人车生活注册/激活会员，先在小程序里登录一次再跑"
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

    content = f"""🚙 东风日产人车生活四账号任务结果

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
🆔 OneID：{res["token"]}
📝 签到：{res["signMsg"]}
🌱 成长值：{res["growthMsg"]}
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
    print(f"║ 🏁 东风日产人车生活任务执行完成          ║")
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
