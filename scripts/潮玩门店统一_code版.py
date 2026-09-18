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


"""潮玩/电玩门店统一动态 code 版(jingjianx 白标SaaS平台)

功能：
  1. 四端口本地服务获取微信 code（按每个小程序的 appid 分别取）
  2. 覆盖 6 个同体系小程序（微信里按名字搜）：
     潮玩社 JN北园银座店 / 阿凡达潮玩荟 / D1玩家 / 高新大玩家 / 大玩家匠心娱乐 / 玩家森林大玩家XY
  3. /capp/account/login code 换 token（Bearer + JJ-CHAINID/JJ-SHOPID 头）
  4. 每店签到（getreward 判断开启 → getprogress 判断已签 → confirm/newconfirm 签到）
  5. 查询各店资产（币/积分/票/优惠券）
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

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

import uuid as _uuid

APP_NAME = "京见鲜多门店统一"
APPID = "wx533baf4b9428d47f"

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

LOG_NAME = "潮玩门店统一"
LOG_ICON = "🧸"

API_BASE = "https://capi.jingjianx.vip"

APPS = [
    {"name": "潮玩社 JN北园银座店", "appid": "wx533baf4b9428d47f", "chainId": "8684",
     "shops": [{"shopId": "15666", "shopName": "潮玩社 JN北园银座店"}]},
    {"name": "阿凡达潮玩荟家庭娱乐中心", "appid": "wxea93bab38fc144d1", "chainId": "10967",
     "shops": [{"shopId": "21380", "shopName": "阿凡达潮玩荟文水店"},
               {"shopId": "21094", "shopName": "阿凡达潮玩荟太原迎春街店"},
               {"shopId": "20169", "shopName": "阿凡达潮玩荟忻州店"},
               {"shopId": "19075", "shopName": "阿凡达潮玩荟榆次店"}]},
    {"name": "D1玩家", "appid": "wxcced0bb82c738dd1", "chainId": "3708",
     "shops": [{"shopId": "7960", "shopName": "D1玩家-大丰保利店"},
               {"shopId": "18811", "shopName": "D1玩家友谊广场店"},
               {"shopId": "14082", "shopName": "D1玩家-大丰汇融店"},
               {"shopId": "13473", "shopName": "D1玩家-阳光新业店"},
               {"shopId": "13190", "shopName": "D1玩家-龙湖金楠天街店"},
               {"shopId": "19190", "shopName": "酷啦啦-甘孜店"},
               {"shopId": "12302", "shopName": "D1玩家-龙湖锦宸店"}]},
    {"name": "高新大玩家", "appid": "wx7ec8c4f08046df18", "chainId": "11279",
     "shops": [{"shopId": "19642", "shopName": "高新大玩家"}]},
    {"name": "大玩家匠心娱乐", "appid": "wxf64d5e147f9cc3b6", "chainId": "9663",
     "shops": [{"shopId": "17093", "shopName": "大玩家匠心娱乐"}]},
    {"name": "玩家森林大玩家XY", "appid": "wx6136bd123a990614", "chainId": "9095",
     "shops": [{"shopId": "18894", "shopName": "玩家森林大玩家XY"}]},
]

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jingjianxcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) MicroMessenger/3.9.12 "
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



def is_today(date_text: str) -> bool:
    if not date_text:
        return False
    return str(date_text).startswith(datetime.now().strftime("%Y-%m-%d"))


def create_guid() -> str:
    return str(_uuid.uuid4())


def app_headers(app: Dict[str, Any], shop_id: str = "", token: str = "", extra: Dict[str, str] | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{app['appid']}/1/page-frame.html",
        "content-type": "application/json",
        "JJ-CHAINID": app["chainId"],
        "BDSZH-SHOPID": shop_id,
        "JJ-SHOPID": shop_id,
        "JJ-MiniAppVersion": "release",
        "JJ-AppId": app["appid"],
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update(extra)
    return headers


def request(app: Dict[str, Any], method: str, api_path: str, shop_id: str = "", token: str = "",
            params: Dict[str, Any] | None = None, data: Dict[str, Any] | None = None,
            biz_code: str = "", proxies: Dict[str, str] | None = None) -> Dict[str, Any]:
    extra = {"JJ-BizCode": biz_code} if biz_code else {}
    kwargs: Dict[str, Any] = {"headers": app_headers(app, shop_id, token, extra), "timeout": REQUEST_TIMEOUT}
    if proxies:
        kwargs["proxies"] = proxies
    url = f"{API_BASE}{api_path}"
    if method == "GET":
        kwargs["params"] = params or {}
        try:
            response = requests.get(url, **kwargs)
        except Exception:
            if not proxies or not ENABLE_DIRECT_FALLBACK:
                raise
            kwargs.pop("proxies", None)
            response = requests.get(url, **kwargs)
    else:
        kwargs["json"] = data or {}
        try:
            response = requests.post(url, **kwargs)
        except Exception:
            if not proxies or not ENABLE_DIRECT_FALLBACK:
                raise
            kwargs.pop("proxies", None)
            response = requests.post(url, **kwargs)
    if response.status_code > 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except Exception:
        return {"success": False, "msg": f"JSON解析失败: {response.text[:200]}"}


def shop_login(app: Dict[str, Any], server: str, shop_id: str,
               proxies: Dict[str, str] | None) -> str:
    code = get_code_for(server, app["appid"])
    if not code:
        raise RuntimeError("code 服务未返回 code")
    result = request(app, "POST", "/capp/account/login", shop_id=shop_id,
                     data={"code": code}, proxies=proxies)
    if not result.get("success"):
        raise RuntimeError(result.get("msg") or json_preview(result, 150))
    return str((result.get("data") or {}).get("token") or "")


def get_signed_state(reward: Dict[str, Any], progress: Dict[str, Any]) -> Dict[str, Any]:
    sign_type = int(reward.get("signCycle") or 0)
    sign_mode = int(reward.get("signMode") or 1)

    if sign_type == 0:
        return {"signed": not progress.get("isSigning"),
                "currentDay": progress.get("signInDays") or 0,
                "apiPath": "/signed/capp/signed/confirm"}

    if sign_mode == 2 and progress.get("totalDay"):
        return {"signed": not progress["totalDay"].get("isSigning"),
                "currentDay": progress["totalDay"].get("signInDays") or 0,
                "apiPath": "/signed/capp/signed/newconfirm"}

    if sign_mode == 3 and isinstance(progress.get("dailyDay"), list):
        return {"signed": any(is_today(item.get("signDate")) for item in progress["dailyDay"]),
                "currentDay": len(progress["dailyDay"]),
                "apiPath": "/signed/capp/signed/newconfirm"}

    total_day = progress.get("totalDay") or {}
    return {"signed": not total_day.get("isSigning") if total_day else False,
            "currentDay": total_day.get("signInDays") or 0,
            "apiPath": "/signed/capp/signed/newconfirm"}


def shop_sign(app: Dict[str, Any], server: str, shop: Dict[str, Any],
              proxies: Dict[str, str] | None) -> str:
    try:
        token = shop_login(app, server, shop["shopId"], proxies)
    except Exception as exc:
        return f"登录失败({exc})"
    try:
        reward = request(app, "GET", "/signed/capp/signed/getreward", shop_id=shop["shopId"],
                         token=token, proxies=proxies)
        if not reward.get("success"):
            return f"getreward失败({reward.get('msg') or '未知'})"
        reward_data = reward.get("data") or {}
        if not reward_data.get("isEnabled"):
            return "未开启签到"

        progress_path = ("/signed/capp/signed/getprogress"
                         if int(reward_data.get("signCycle") or 0) == 0
                         else "/signed/capp/signed/getnewprogress")
        progress_result = request(app, "GET", progress_path, shop_id=shop["shopId"],
                                  token=token, proxies=proxies)
        if not progress_result.get("success"):
            return f"getprogress失败({progress_result.get('msg') or '未知'})"
        state = get_signed_state(reward_data, progress_result.get("data") or {})
        if state["signed"]:
            return f"今日已签到(第{state['currentDay']}天)"

        result = request(app, "POST", state["apiPath"], shop_id=shop["shopId"], token=token,
                         data={"longitude": 0, "latitude": 0}, biz_code=create_guid(), proxies=proxies)
        if not result.get("success"):
            return f"签到失败({result.get('msg') or '未知'})"
        data = result.get("data") or {}
        reward_name = data.get("rewardName") or data.get("prizeName")
        return f"签到成功{f' 奖励={reward_name}' if reward_name else ''}"
    except Exception as exc:
        return f"签到失败({exc})"


def get_code_for(server: str, appid: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url} appId={appid}")
    try:
        response = direct_session().get(url, params={"appId": appid}, timeout=20)
        data = response.json()
        code_val = str(data.get("code") or "")
        status = data.get("status")
        if data.get("err") != 0 or not code_val or code_val == "null" or code_val == "invalid" or (status is not None and status != "ok"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None
        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


EMPTY_RESULT = {
    "server": "-",
    "success": False,
    "proxyStatus": "未使用代理",
    "proxyIp": "-",
    "shopSummary": "-",
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

    lines = []
    ok_shops = 0
    total_shops = 0
    for app in APPS:
        for shop in app["shops"]:
            total_shops += 1
            msg = shop_sign(app, server, shop, proxies)
            if "签到成功" in msg or "已签到" in msg or "未开启" in msg:
                ok_shops += 1
            print(f"🏪 [{app['name']} | {shop['shopName']}] {msg}")
            lines.append(f"{shop['shopName']}: {msg}")
            sleep(random.randint(1, 2))

    result["shopSummary"] = f"{ok_shops}/{total_shops} 店正常"
    result["error"] = ""
    result["success"] = ok_shops > 0 and "签到失败" not in "".join(lines)
    # 明细放 notify 里逐行展示
    result["_lines"] = lines
    return result
def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🧸 潮玩门店统一四账号任务结果

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
🏪 门店：{res["shopSummary"]}
"""
        for line in res.get("_lines", []):
            content += f"  · {line}\n"
        content += f"""{icon} 结果：{"成功" if res["success"] else "失败"}
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
    print(f"║ 🏁 潮玩门店统一任务执行完成          ║")
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
