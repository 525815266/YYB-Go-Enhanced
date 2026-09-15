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


"""宝妈上班(张团)动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. UniCloud 匿名 accessToken → uni-id-co/loginByWeixin 用 code 换 uniIdToken
  3. DCloud-clientDB 查询 uid/昵称/积分/余额
  4. wolf-order/createContribution 循环赚取贡献值(默认最多 20 次)
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  BMSB_MAX_RUNS     单账号最多领取次数，默认 20（服务端到上限会自动停）

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

import base64
import hashlib
import hmac

APP_NAME = "宝妈上班小程序"
APPID = "wxe6cb23a7f02277ed"

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

LOG_NAME = "宝妈上班"
LOG_ICON = "🍼"

UNI_APPID = "__UNI__AE9315F"
CLIENT_APP_NAME = "张团--小程序22"
SPACE_ID = "mp-50d375d9-5c5e-4271-8517-b09cb093334b"
CLIENT_SECRET = "Gf/DmFLzvUNIqaty2aIXEQ=="
MAX_RUNS = int(os.getenv("BMSB_MAX_RUNS", "20"))

BASE_URL = "https://api.next.bspapp.com"
CLIENT_URL = f"{BASE_URL}/client"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bmsbcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; Redmi K30 Pro Build/SKQ1.211006.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/146.0.7680.178 Mobile Safari/537.36 MicroMessenger/8.0.71"
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



def gen_sign(body: Dict[str, Any]) -> str:
    """x-serverless-sign = HMAC-MD5(CLIENT_SECRET, 顶层字段按key排序 k=v 过滤空值后 & 连接)"""
    parts = [f"{k}={body[k]}" for k in sorted(body.keys()) if str(body[k]) != ""]
    return hmac.new(CLIENT_SECRET.encode("utf-8"), "&".join(parts).encode("utf-8"), hashlib.md5).hexdigest()


def build_client_info() -> Dict[str, Any]:
    return {
        "PLATFORM": "mp-weixin", "OS": "android", "APPID": UNI_APPID,
        "DEVICEID": str(random.randint(10**18, 9 * 10**18)), "scene": 1011,
        "appId": UNI_APPID, "appName": CLIENT_APP_NAME, "appVersion": "1.0.0", "appVersionCode": "100",
        "appLanguage": "zh-Hans", "hostVersion": "8.0.71", "hostName": "WeChat", "uniPlatform": "mp-weixin",
        "uniCompilerVersion": "5.07", "uniRuntimeVersion": "5.07", "deviceType": "phone",
        "deviceBrand": "redmi", "deviceModel": "Redmi K30 Pro", "osName": "android", "osVersion": "12",
        "locale": "zh-Hans", "LOCALE": "zh-Hans",
    }


def serverless_headers(token: str | None = None, sign_body: Dict[str, Any] | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json;charset=utf-8",
        "Referer": f"https://servicewechat.com/{APPID}/3/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["x-basement-token"] = token
    if sign_body is not None:
        headers["x-serverless-sign"] = gen_sign(sign_body)
    return headers


def jwt_remaining_hours(token: str) -> float | None:
    """JWT 剩余小时数，无法解析返回 None"""
    try:
        payload_b64 = str(token).split(".")[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
        return (payload.get("exp", 0) - int(time.time())) / 3600
    except Exception:
        return None


class AccountSession:
    """单个 code 服务地址的会话: accessToken + uniIdToken"""

    def __init__(self, server: str):
        self.server = server
        self.access_token = ""
        self.access_expire = 0
        self.uni_id_token = ""
        self.uid = ""

    def get_access_token(self) -> str:
        if self.access_token and time.time() * 1000 < self.access_expire - 30000:
            return self.access_token

        body = {
            "method": "serverless.auth.user.anonymousAuthorize",
            "params": "{}",
            "spaceId": SPACE_ID,
            "timestamp": int(time.time() * 1000),
        }
        response = request_with_proxy(
            "POST", CLIENT_URL, headers=serverless_headers(sign_body=body), json=body, server=self.server,
        )
        data = response.json() if response.text.strip() else {}
        if not data.get("success") or not safe_data(data).get("accessToken"):
            raise RuntimeError(f"accessToken 获取失败: {json_preview(data, 200)}")
        self.access_token = data["data"]["accessToken"]
        self.access_expire = time.time() * 1000 + (data["data"].get("expiresInSecond") or 600) * 1000
        return self.access_token

    def call_api(self, function_target: str, function_args: Dict[str, Any], retry: bool = True) -> Dict[str, Any]:
        token = self.get_access_token()
        args = {**function_args, "clientInfo": build_client_info(), "uniIdToken": self.uni_id_token or ""}
        body = {
            "method": "serverless.function.runtime.invoke",
            "params": json.dumps({"functionTarget": function_target, "functionArgs": args},
                                 ensure_ascii=False, separators=(",", ":")),
            "spaceId": SPACE_ID,
            "timestamp": int(time.time() * 1000),
            "token": token,
        }
        response = request_with_proxy(
            "POST", CLIENT_URL, headers=serverless_headers(token=token, sign_body=body), json=body, server=self.server,
        )
        data = response.json() if response.text.strip() else {}
        if retry and not data.get("success") and (data.get("error") or {}).get("code") == "GATEWAY_INVALID_TOKEN":
            self.access_token = ""
            self.access_expire = 0
            return self.call_api(function_target, function_args, retry=False)
        return data

    def login_by_weixin(self) -> None:
        code = get_code(self.server)
        if not code:
            raise RuntimeError("code 服务未返回 code")
        print("🔐 [登录] loginByWeixin 使用 code 换 uniIdToken")
        res = self.call_api("uni-id-co", {"method": "loginByWeixin", "params": [{"code": code}]})
        token = (safe_data(res).get("newToken") or {}).get("token") or safe_data(res).get("token")
        if not res.get("success") or not token:
            raise RuntimeError(f"loginByWeixin 失败: {json_preview(res, 200)}")
        self.uni_id_token = token
        print(f"✅ [登录] uniIdToken 获取成功: {mask(token)}")

    def ensure_login(self) -> None:
        cached = load_token_cache().get(self.server) or {}
        cached_token = cached.get("uniIdToken")
        if cached_token:
            hours = jwt_remaining_hours(cached_token)
            if hours is not None and hours > 1:
                self.uni_id_token = cached_token
                print(f"✅ [缓存] 使用缓存token（剩余 {hours:.1f}h）")
                return
            print("⚠️ [缓存] 缓存token已过期，重新登录")
        self.login_by_weixin()
        set_cached_token(self.server, self.uni_id_token)

    def extract_uid(self) -> str:
        res = self.call_api("DCloud-clientDB", {
            "command": {"$db": [
                {"$method": "collection", "$param": ["uni-id-users"]},
                {"$method": "where", "$param": ["'_id' == $cloudEnv_uid"]},
                {"$method": "field", "$param": ["uid,_id,mobile,nickname,my_invite_code,money,score,level"]},
                {"$method": "get", "$param": []},
            ]},
        })
        users = safe_data(res).get("data") or []
        user = users[0] if users and isinstance(users[0], dict) else {}
        self.uid = str(user.get("uid") or user.get("_id") or "")
        if user:
            nickname = user.get('nickname') or '-'
            score = user.get('score') if user.get('score') is not None else '-'
            money = user.get('money') if user.get('money') is not None else '-'
            print(f"👤 [会员] 昵称 {nickname} | 积分 {score} | 余额 {money}")
        return self.uid

    def create_contribution(self) -> Dict[str, Any]:
        return self.call_api("wolf-order", {"method": "createContribution", "params": [{"uid": self.uid}]})


def set_cached_token(server: str, token: str) -> None:
    cache = load_token_cache()
    cache[server] = {"uniIdToken": token, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


EMPTY_RESULT = {
    "server": "-",
    "success": False,
    "proxyStatus": "未使用代理",
    "proxyIp": "-",
    "token": "-",
    "uid": "-",
    "earnMsg": "-",
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
        result["token"] = mask(session.uni_id_token)

        if not session.extract_uid():
            result["error"] = "未能获取 uid（该微信号可能未注册），跳过"
            print("⚠️ [账号] 未能获取 uid（该微信号可能未注册），跳过")
            return result
        result["uid"] = session.uid

        earned = 0
        ok_count = 0
        fails = 0
        for i in range(1, MAX_RUNS + 1):
            res = session.create_contribution()
            if not res or not res.get("success"):
                fails += 1
                if fails >= 3:
                    print("⚠️ [赚取] 连续3次失败，终止")
                    break
                sleep(random.randint(8, 15))
                continue

            data = safe_data(res)
            err_code = data.get("errCode")
            err_code = -1 if err_code is None else int(err_code)
            if err_code == 0:
                inner = data.get("data") or {}
                earned += inner.get("cons") or 0
                ok_count += 1
                fails = 0
                print(f"✅ [赚取] [{i}/{MAX_RUNS}] 发放成功 贡献值 {inner.get('cons', '?')}（今日第 {inner.get('count', '?')} 次）")
                sleep(random.randint(10, 18))
            else:
                err_msg = data.get("errMsg") or json_preview(data, 200)
                if __import__("re").search(r"频繁|稍后|too frequent|请等待|间隔", str(err_msg), __import__("re").IGNORECASE):
                    print(f"⏳ [赚取] [{i}/{MAX_RUNS}] 限频（{err_msg}），等待后重试")
                    sleep(random.randint(12, 22))
                    continue
                print(f"⚠️ [赚取] 已停止：{err_msg}")
                break

        result["earnMsg"] = f"本次成功 {ok_count} 次，累计贡献值 +{earned}"
        print(f"🏁 [完成] ✅ 完成，本次成功 {ok_count} 次，累计贡献值 +{earned}")
        result["success"] = ok_count > 0
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result
def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍼 宝妈上班四账号任务结果

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
🆔 UID：{res["uid"]}
💎 贡献值：{res["earnMsg"]}
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
    print(f"║ 🏁 宝妈上班任务执行完成          ║")
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
