#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
东风奕派小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /appv3/api 使用 wxLogin 换 token
  3. 查询签到信息
  4. 每日签到
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER        YYB Go 地址@账号ID或OpenID，每行一个；配置后支持备注/昵称
  EP_SERVERS        旧版 code 服务地址，每行一个；仅在未配置 YYB_SERVER 时使用
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  EP_PHONES         多账号手机号映射，每行“账号ID或OpenID#手机号”；未匹配时回退 EP_PHONE
  EP_PHONE          单账号兼容写法，所有未单独配置手机号的账号共用
  EP_CHANNEL_ID     渠道 ID，默认 1234
  EP_EQUIP_NO       设备号，默认 1234

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import hashlib
import json
import os
import random
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "东风奕派小程序"
APPID = "wx272cd36461ba9b05"

SERVERS = [
    "192.168.31.179:8088",
    "10.30.9.183:8088",
]


@dataclass
class AccountTarget:
    server: str
    ref: str = ""
    remark: str = ""
    nickname: str = ""
    phone: str = ""
    index: int = 0

    @property
    def label(self) -> str:
        display = self.remark or self.nickname
        if display and self.ref:
            number = self.ref if self.ref.isdigit() else str(self.index or "?")
            return f"{display}（账号 {number}）"
        if self.ref:
            return f"账号 {self.ref if self.ref.isdigit() else (self.index or '?')}"
        return display or self.server


def normalize_server(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value.rstrip("/")


def load_account_targets() -> List[AccountTarget]:
    raw = os.getenv("YYB_SERVER", "").strip()
    if not raw:
        legacy = os.getenv("EP_SERVERS", "").strip()
        servers = [line.strip() for line in legacy.splitlines() if line.strip()] if legacy else SERVERS
        return [AccountTarget(server=server, index=index) for index, server in enumerate(servers, 1)]
    accounts: List[AccountTarget] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "@" not in line:
            print(f"⚠️ [配置] YYB_SERVER 忽略无账号标识行：{line}")
            continue
        server, ref = (part.strip() for part in line.split("@", 1))
        if server and ref:
            accounts.append(AccountTarget(normalize_server(server), ref=ref, index=len(accounts) + 1))
    if not accounts:
        raise RuntimeError("YYB_SERVER 未读取到有效账号，格式：yyb-go:8000@账号ID或OpenID")
    load_account_labels(accounts)
    return accounts


def load_account_labels(accounts: List[AccountTarget]) -> None:
    grouped: Dict[str, List[AccountTarget]] = {}
    for account in accounts:
        grouped.setdefault(account.server, []).append(account)
    for server, rows in grouped.items():
        try:
            response = direct_session().get(server + "/accounts", timeout=10)
            payload = response.json()
            items = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(items, dict):
                items = items.get("accounts") or items.get("items") or []
            if not response.ok or not isinstance(items, list):
                continue
        except (requests.RequestException, ValueError):
            continue
        for account in rows:
            for item in items:
                if not isinstance(item, dict):
                    continue
                identifiers = {str(item.get("id") or ""), str(item.get("openid") or ""), str(item.get("uin") or "")}
                if account.ref in identifiers:
                    apply_account_label(account, item)
                    break


def apply_account_label(account: AccountTarget, item: Any) -> None:
    if not isinstance(item, dict):
        return
    account.remark = str(item.get("remark") or item.get("alias") or account.remark).strip()
    account.nickname = str(item.get("nickname") or account.nickname).strip()


def parse_account_phones(raw: str) -> Dict[str, str]:
    """Parse one account phone mapping per line: account-ref#phone."""
    mappings: Dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            ref, phone = (part.strip() for part in line.split("#", 1))
        elif "=" in line:
            ref, phone = (part.strip() for part in line.split("=", 1))
        else:
            print(f"⚠️ [配置] EP_PHONES 忽略无效行：{line}")
            continue
        if not ref or not phone:
            print(f"⚠️ [配置] EP_PHONES 忽略空账号或手机号：{line}")
            continue
        mappings[ref] = phone
    return mappings


def assign_account_phones(accounts: List[AccountTarget]) -> None:
    mappings = parse_account_phones(os.getenv("EP_PHONES", ""))
    default_phone = os.getenv("EP_PHONE", "").strip()
    for account in accounts:
        account.phone = (
            mappings.get(account.ref)
            or mappings.get(str(account.index))
            or default_phone
        )


CURRENT_ACCOUNTS: List[AccountTarget] = []

CHANNEL_ID = os.getenv("EP_CHANNEL_ID", "1234")
EQUIP_NO = os.getenv("EP_EQUIP_NO", "1234")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

GATEWAY_URL = "https://sapp.dfmc.com.cn/appv3/api"
APP_AID = "app1vbsGs1cRiUDA7RFBOv6ZyFk60wjSz9Z"
APP_KEY = "JvIfhWqA8lzewUI5bxChXqsAbpIOIqERlSy4N9xBFeJJWTbyLGkxPrRK8COs7fM2EMhOOthshEEv576cDsSfVlhRC4U2rZVZYh9wThdOiQjXetT2c8DE7nS4XvfJGUHl"
LOGIN_API = "ly.mp.miniprogram.user.v2.wxLogin"
SIGN_LIST_API = "ly.mp.miniprogram.growth.signin.list"
SIGN_API = "ly.mp.miniprogram.growth.taskCenter.signin"

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


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'rows' from an API response, handling null/missing."""
    return resp.get("rows") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🚗 东风奕派小程序动态 code 版                   ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(CURRENT_ACCOUNTS):<34}║")
    print("╚" + "═" * 50 + "╝")


def log_account_header(index: int, total: int, account: AccountTarget) -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 👤 账号 {account.label:<38}│")
    print(f"│ 🌍 来源 {account.server:<40}│")
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


def get_code(account: AccountTarget) -> str | None:
    server = account.server
    if account.ref:
        url = server.rstrip("/") + "/wxapp/getCode"
        method = "POST"
        request_kwargs = {"json": {"ref": account.ref, "app_id": APPID}}
    else:
        url = f"http://{server}/login"
        method = "GET"
        request_kwargs = {"params": {"appId": APPID}}
    print(f"🔐 [授权] 请求本地 code 服务: {url}")
    for attempt in range(1, 3):
        try:
            response = direct_session().request(method, url, timeout=20, **request_kwargs)
            data = response.json()
            if account.ref:
                nested = data.get("data") if isinstance(data, dict) else None
                if isinstance(nested, dict):
                    apply_account_label(account, nested.get("account"))
                    nested = nested.get("result") or nested.get("data") or nested
                code = str((nested.get("code") if isinstance(nested, dict) else None) or (data.get("code") if isinstance(data, dict) else "") or "").strip()
                valid = response.ok and bool(code) and code.lower() != "null"
            else:
                code = str(data.get("code") or "").strip()
                valid = data.get("err") == 0 and bool(code) and code.lower() != "null"
            if valid:
                print("✅ [授权] code 获取成功")
                return code
            print(f"❌ [授权] 第 {attempt} 次 code 获取失败: {json_preview(data)}")
        except Exception as exc:
            print(f"❌ [授权] 第 {attempt} 次 code 获取异常: {exc}")
        if attempt < 2:
            sleep(3)
    return None


def gateway_request(
    api: str,
    payload: Dict[str, Any],
    uid: str,
    token: str,
    proxies: Dict[str, str] | None,
    server: str,
) -> Dict[str, Any]:
    """请求 DFMC 网关 /appv3/api，自动生成 sign / keysign 签名。"""
    timestamp = str(int(time.time() * 1000))
    noncestr = uuid.uuid4().hex

    wire = ""
    if payload not in ({}, None):
        wire = json.dumps(payload, separators=(",", ":"))

    sign = hashlib.sha512(
        f"{uid}{api}{noncestr}{timestamp}{token}{wire}".encode()
    ).hexdigest()
    keysign = hashlib.sha512(
        f"{APP_AID}{APP_KEY}{api}{noncestr}{timestamp}{wire}".encode()
    ).hexdigest()

    headers = {
        "apitype": "8",
        "appid": APP_AID,
        "timestamp": timestamp,
        "lang": "cn",
        "sign": sign,
        "appsystem": "miniprogram",
        "uid": uid,
        "keysign": keysign,
        "xweb_xhr": "1",
        "api": api,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "appcode": "miniprogram",
        "noncestr": noncestr,
        "Referer": f"https://servicewechat.com/{APPID}/129/page-frame.html",
    }

    response = request_with_proxy(
        "POST",
        GATEWAY_URL,
        data=(wire or "{}").encode(),
        headers=headers,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "result": "-1",
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def extract_token(data: Any) -> str | None:
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

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def extract_uid(data: Any) -> str:
    if not isinstance(data, dict):
        return ""

    user = data.get("user")
    if isinstance(user, dict) and user.get("userId"):
        return str(user["userId"])

    for key in ("centerUserId", "userId", "uid"):
        if data.get(key):
            return str(data[key])

    return ""


def login_by_code(
    server: str,
    code: str,
    phone: str,
    proxies: Dict[str, str] | None,
) -> Tuple[str | None, str, Dict[str, Any] | None]:
    try:
        if not phone:
            print("❌ [登录] 未配置该账号手机号，请设置 EP_PHONES 或 EP_PHONE")
            return None, "", None

        print("🔐 [登录] 使用 code 换 token")
        data = gateway_request(
            LOGIN_API,
            {
                "phone": phone,
                "loginCode": code,
                "appCode": "miniprogram",
                "equipNo": EQUIP_NO,
                "channelId": CHANNEL_ID,
                "isAutoLogin": "1",
            },
            uid="",
            token="",
            proxies=proxies,
            server=server,
        )

        token = extract_token(data)
        uid = extract_uid(data)
        if token and uid:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, uid, data

        print(f"❌ [登录] 登录失败: {json_preview(data)}")
        return None, "", data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, "", None


def run_account(index: int, total: int, account: AccountTarget) -> Dict[str, Any]:
    server = account.server
    result = {
        "server": server,
        "accountLabel": account.label,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signInfo": "-",
        "signMsg": "-",
        "error": "",
    }

    log_account_header(index, total, account)

    if not account.phone:
        result["error"] = "未配置该账号手机号，请设置 EP_PHONES 或 EP_PHONE"
        print(f"❌ [配置] {account.label} 未配置手机号，跳过登录")
        return result

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    code = get_code(account)
    if not code:
        result["error"] = "获取 code 失败"
        return result

    result["accountLabel"] = account.label
    if account.remark or account.nickname:
        print(f"👤 [YYB] {account.label}")

    token, uid, raw_login = login_by_code(server, code, account.phone, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        sign_info_resp = gateway_request(SIGN_LIST_API, {"type": "1"}, uid, token, proxies, server)
        if sign_info_resp.get("result") == "1":
            rows = safe_data(sign_info_resp)
            continuous_days = rows.get("continuousDays") or "-"
            total_days = rows.get("totalDays") or "-"
            sign_date = rows.get("signDate") or "-"
            result["signInfo"] = f"连续 {continuous_days} 天 / 累计 {total_days} 天 / 已签 {sign_date}"
            print(f"📅 [签到] {result['signInfo']}")
        else:
            result["signInfo"] = sign_info_resp.get("msg") or "查询签到信息失败"
            print(f"⚠️ [签到] {result['signInfo']}")

        sign_resp = gateway_request(SIGN_API, {}, uid, token, proxies, server)
        if sign_resp.get("result") == "1":
            result["signMsg"] = sign_resp.get("msg") or "签到成功"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            result["signMsg"] = sign_resp.get("msg") or "签到失败"
            print(f"⚠️ [签到] {result['signMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🚗 东风奕派小程序签到任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}：{res.get("accountLabel") or res["server"]}
🌍 来源：{res["server"]}
🌐 代理：{res["proxyStatus"]}
📡 出口IP：{res["proxyIp"]}
🔐 Token：{res["token"]}
📅 签到信息：{res["signInfo"]}
📝 签到：{res["signMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    global CURRENT_ACCOUNTS
    try:
        CURRENT_ACCOUNTS = load_account_targets()
        assign_account_phones(CURRENT_ACCOUNTS)
    except Exception as exc:
        print(f"❌ [配置] {exc}")
        return
    log_title()

    results: List[Dict[str, Any]] = []

    for index, account in enumerate(CURRENT_ACCOUNTS, 1):
        try:
            result = run_account(index, len(CURRENT_ACCOUNTS), account)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {account.label} 执行异常: {exc}")
            results.append({
                "server": account.server,
                "accountLabel": account.label,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signInfo": "-",
                "signMsg": "-",
                "error": str(exc),
            })

    notify_content = build_notify(results)
    print()
    print(notify_content)
    send_pushplus(f"东风奕派签到 {now_text()}", notify_content)


if __name__ == "__main__":
    main()
