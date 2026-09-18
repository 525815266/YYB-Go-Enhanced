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


"""携程会员(xchy 惠 YadCard)动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. PASSPORT 三步登录: wechatLogin → authenticate → thirdPartyLogin 换 cticket
  3. 每日签到（getSignTodayInfoProxy 查状态 + signToday 签到，Cookie 直连通道）
  4. PushPlus 推送
  5. 品赞代理，业务请求优先代理，失败直连兜底
  注：签到写操作若被携程运行态校验拦截(404002)，脚本无法绕过，会如实报告

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

APP_NAME = "携程会员小程序"
APPID = "wx0e6ed4f51db9d078"

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

LOG_NAME = "携程会员"
LOG_ICON = "✈️"

API_BASE = "https://m.ctrip.com"
PASSPORT_BASE = "https://passport.ctrip.com/gateway/api"
CLIENT_ID = "09031101311473737701"
ACCESS_CODE = "XTHYY69RNSKLWEICHATMINI"
PACKAGE_VERSION = "1055"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xchycookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF"
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



def parse_json_maybe(text: Any) -> Any:
    if isinstance(text, str) and text:
        try:
            return json.loads(text)
        except Exception:
            return text
    return text


def ok_response_status(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    rs = data.get("ResponseStatus") or data.get("responseStatus") or {}
    return (rs.get("Ack") or rs.get("ack")) == "Success"


class AccountSession:
    """单个 code 服务地址的会话: cticket cookie + duid/udl/uid"""

    def __init__(self, server: str):
        self.server = server
        self.ticket = ""
        self.duid = ""
        self.udl = ""
        self.uid = ""
        self.ctrip_open_id = ""

    def h5_cookie(self) -> str:
        cookies = []
        if self.ticket:
            cookies.append(f"cticket={self.ticket}")
        if self.duid:
            cookies.append(f"DUID={quote(self.duid)}")
        if self.udl:
            cookies.append(f"_udl={self.udl}")
        cookies.append(f"GUID={CLIENT_ID}")
        return f"{'; '.join(cookies)};"

    def gateway(self, pathname: str, data: Dict[str, Any], proxies: Dict[str, str] | None) -> Dict[str, Any]:
        response = request_with_proxy(
            "POST", f"{PASSPORT_BASE}/{pathname}",
            data=json.dumps(data, ensure_ascii=False),
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Referer": f"https://servicewechat.com/{APPID}/{PACKAGE_VERSION}/page-frame.html",
            },
            proxies=proxies, server=self.server,
        )
        if response.status_code != 200 or response.json().get("ReturnCode") != 0:
            raise RuntimeError(f"{pathname} 失败[{response.status_code}]: {response.text[:250]}")
        return parse_json_maybe(response.json().get("Result") or "{}")

    def h5_api(self, pathname: str, data: Dict[str, Any] | None,
               proxies: Dict[str, str] | None) -> Dict[str, Any]:
        response = request_with_proxy(
            "POST", f"{API_BASE}{pathname}",
            data=json.dumps(data or {}, ensure_ascii=False),
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Referer": "https://m.ctrip.com/",
                "Cookie": self.h5_cookie(),
            },
            proxies=proxies, server=self.server,
        )
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:400]}
        return {"status": response.status_code, "data": body, "text": response.text[:600]}

    def set_cached_token(self) -> None:
        cache = load_token_cache()
        cache[self.server] = {"ticket": self.ticket, "duid": self.duid, "udl": self.udl,
                              "uid": self.uid, "ctripOpenId": self.ctrip_open_id,
                              "updateTime": datetime.now().isoformat()}
        save_token_cache(cache)

    def login(self, proxies: Dict[str, str] | None) -> None:
        code = get_code(self.server)
        if not code:
            raise RuntimeError("code 服务未返回 code")

        print("🔐 [登录] 三步 passport 登录")
        wx_login = self.gateway("soa2/14553/wechatLogin.json", {
            "AccountHead": {}, "Data": {"authCode": code, "thirdConfigCode": ACCESS_CODE, "Context": {}},
        }, proxies)
        if not wx_login.get("wechatCode") or wx_login.get("resultStatus", {}).get("returnCode") != 0:
            raise RuntimeError(f"wechatLogin未返回wechatCode: {json_preview(wx_login, 200)}")

        auth = self.gateway("soa2/14553/authenticate.json", {
            "AccountHead": {},
            "Data": {"authCode": wx_login["wechatCode"], "thirdType": "wechat_app",
                     "thirdConfigCode": ACCESS_CODE, "context": {"encryptedData": "", "iv": "", "uuid": ""}},
        }, proxies)
        if not auth.get("token") or auth.get("resultStatus", {}).get("returnCode") != 0:
            raise RuntimeError(f"authenticate未返回token: {json_preview(auth, 200)}")

        login = self.gateway("soa2/12559/thirdPartyLogin.json", {
            "AccountHead": {},
            "Data": {
                "accountHead": {"locale": "zh_CN", "platform": "MINIAPP"},
                "token": auth["token"],
                "extendedProperties": {
                    "clientID": CLIENT_ID, "page_id": "", "Url": "", "thirdConfigCode": ACCESS_CODE,
                    "deviceName": "Windows PC", "OsType": "windows",
                },
            },
        }, proxies)
        if not login.get("ticket") or login.get("resultStatus", {}).get("returnCode") != 0:
            raise RuntimeError(f"NO_ACCOUNT:thirdPartyLogin未返回ticket: {json_preview(login, 200)}")

        self.ticket = login["ticket"]
        ext = login.get("extendedProperties") or {}
        self.duid = login.get("duid") or ext.get("duid") or ""
        self.udl = login.get("udl") or ""
        self.uid = login.get("uid") or ext.get("uid") or ""
        self.ctrip_open_id = login.get("openId") or ext.get("openId") or ""
        self.set_cached_token()
        print(f"✅ [登录] 登录成功: {mask(self.uid or self.ticket)}")

    def ensure_login(self, proxies: Dict[str, str] | None) -> None:
        cached = load_token_cache().get(self.server) or {}
        if cached.get("ticket"):
            self.ticket = cached["ticket"]
            self.duid = cached.get("duid", "")
            self.udl = cached.get("udl", "")
            self.uid = cached.get("uid", "")
            self.ctrip_open_id = cached.get("ctripOpenId", "")
            print("✅ [缓存] 使用缓存ticket")
            return
        self.login(proxies)

    def query_sign_status(self, proxies: Dict[str, str] | None) -> "bool | None":
        res = self.h5_api("/restapi/soa2/13012/getSignTodayInfoProxy", {}, proxies)
        if res["status"] != 200 or not ok_response_status(res["data"]):
            return None
        info = parse_json_maybe(res["data"].get("responseJson") or "{}")
        signed = bool(isinstance(info, dict) and info.get("sign") is True)
        total = info.get("totalPoint") if isinstance(info, dict) else None
        print(f"🔍 [状态] 今日签到状态: {'已签到' if signed else '未签到'}"
              f"{f'，积分{total}' if total is not None else ''}")
        return signed

    def sign(self, proxies: Dict[str, str] | None, retry: bool = True) -> str:
        st = self.query_sign_status(proxies)
        if st is True:
            return "今日已签到"

        open_id = self.ctrip_open_id or ""
        res = self.h5_api("/restapi/soa2/22769/signToday", {"openId": open_id}, proxies)

        if res["status"] == 401 and str(res["data"].get("code")) == "11001":
            print("❌ [签到] 签到接口被携程运行态校验拦截(401/11001)")
            return "被运行态校验拦截(401/11001)"
        if res["status"] != 200:
            if retry and (res["status"] == 403 or __import__("re").search(
                    r"token|登录|未授权|失效|过期|未登录|鉴权", res["text"], __import__("re").IGNORECASE)):
                print("⚠️ [签到] 会话可能失效，重新登录后重试")
                cache = load_token_cache()
                cache.pop(self.server, None)
                save_token_cache(cache)
                self.ticket = ""
                self.login(proxies)
                return self.sign(proxies, retry=False)
            print(f"❌ [签到] 签到请求异常[{res['status']}]: {res['text'][:250]}")
            return f"请求异常[{res['status']}]"

        d = res["data"] or {}
        message = d.get("message") or d.get("Message") or ""
        code = int(d.get("code") or 0)
        points = int(d.get("baseIntegratedPoint") or 0) + int(d.get("extraIntegratedPoint") or 0)
        if ok_response_status(d) and (code == 0 or __import__("re").search(r"成功", message)):
            msg = f"签到成功{('：' + message) if message else ''}{f'，积分+{points}' if points else ''}"
            print(f"✅ [签到] {msg}")
            return msg
        if __import__("re").search(r"已签到|已经签到|无法补签|重复", message) or code == 400001:
            print(f"✅ [签到] 今日已签到（{message or code}）")
            return f"今日已签到（{message or code}）"
        if code == 404002 or __import__("re").search(r"未满足当前活动参与条件", message):
            warn = f"签到被携程运行态校验(风控)拦截：{message or code}（写操作需运行态私有签名，无法绕过）"
            print(f"⚠️ [签到] {warn}")
            return warn
        if ok_response_status(d):
            print(f"ℹ️ [签到] 签到返回：{res['text'][:250]}")
            return "签到返回异常内容"
        if retry and __import__("re").search(r"token|登录|未授权|失效|过期|未登录|鉴权", res["text"], __import__("re").IGNORECASE):
            print("⚠️ [签到] 会话可能失效，重新登录后重试")
            self.ticket = ""
            self.login(proxies)
            return self.sign(proxies, retry=False)
        print(f"❌ [签到] 签到失败：{res['text'][:250]}")
        return f"签到失败"


EMPTY_RESULT = {
    "server": "-",
    "success": False,
    "proxyStatus": "未使用代理",
    "proxyIp": "-",
    "token": "-",
    "signMsg": "-",
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
        result["token"] = mask(session.uid or session.ticket)
        result["signMsg"] = session.sign(proxies)
        result["success"] = "签到成功" in result["signMsg"] or "已签到" in result["signMsg"]
        return result

    except RuntimeError as exc:
        if str(exc).startswith("NO_ACCOUNT"):
            result["error"] = "该微信号还没在携程注册/绑定会员，先在小程序里登录一次再跑"
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

    content = f"""✈️ 携程会员四账号任务结果

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
🆔 UID：{res["token"]}
📝 签到：{res["signMsg"]}
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
    print(f"║ 🏁 携程会员任务执行完成          ║")
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
