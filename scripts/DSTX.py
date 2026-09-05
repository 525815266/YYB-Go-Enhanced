#!/usr/bin/env python3
# name: 都市甜心
# cron: 0 16 * * *
# -*- coding: utf-8 -*-

"""
🍰 都市甜心动态 code 签到美化版

流程：
  1. 本地服务获取微信 code（从环境变量 YYB_SERVER 读取，换行分隔多地址）
  2. Auth 不传 PSPLVISITORID
  3. 提取 reloginToken
  4. reloginToken 作为 PSPLVISITORID
  5. FindLoginInfo 获取会员信息
  6. 查询签到状态 / 签到 / web_report 上报
  7. PushPlus 推送汇总

环境变量：
  YYB_SERVER             必填：wxcode服务地址，多地址换行填写
  PLUSPLUS_TOKEN    PushPlus token，可选
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
from datetime import datetime
from urllib.parse import quote

import requests

from yyb_account_guard import filter_accounts, mark_from_error, mark_ready


APPID = "wx46abbbcfa7cf571a"
# ========== 修改：从环境变量 YYB_SERVER 读取服务地址，换行分割 ==========
SERVERS = []
env_YYB_SERVER = os.getenv("YYB_SERVER", "")
if env_YYB_SERVER:
    raw_lines = env_YYB_SERVER.splitlines()
    # 去除每行首尾空格、过滤空行
    SERVERS = [line.strip() for line in raw_lines if line.strip()]

# 无有效地址直接退出
if len(SERVERS) == 0:
    print("❌ 未配置环境变量 YYB_SERVER")
    print("格式：地址@微信账号标识，多账号换行分隔")
    print("192.168.31.36:8088")
    print("192.168.31.88:8088")
    print("192.168.31.62:8088")
    exit(1)

SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)
if not SERVERS:
    print("⏭️ 公共缓存中没有可执行账号")
    raise SystemExit(0)

print(f"✅ 读取到 {len(SERVERS)} 个 YYB Go 账号")
print("-" * 60 + "\n")
# =================================================================

STORE_IDS = ["5545556", "4815863"]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True

BASE_URL = "https://wxservice-stg62.pospal.cn"
WEB_REPORT_URL = "https://webreport.pospal.cn/datareport/simple/web_report"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541923) XWEB/19823"


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def mask_value(value: str) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def json_preview(data, limit: int = 800) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    return text[:limit]


def line(char: str = "━", length: int = 48) -> str:
    return char * length


def log_title() -> None:
    print()
    print("╔" + "═" * 48 + "╗")
    print("║ 🍰 都市甜心动态 code 签到美化版              ║")
    print(f"║ 🕒 启动时间: {now_text():<31}║")
    print(f"║ 🔢 账号数量: {len(SERVERS):<33}║")
    print("╚" + "═" * 48 + "╝")


def log_account_header(index: int, total: int, server: str) -> None:
    print()
    print("┌" + "─" * 48 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<35}│")
    print(f"│ 🌍 来源 {server:<38}│")
    print("└" + "─" * 48 + "┘")


def log_step(icon: str, tag: str, message: str) -> None:
    print(f"{icon} [{tag}] {message}")


def log_success(tag: str, message: str) -> None:
    log_step("✅", tag, message)


def log_error(tag: str, message: str) -> None:
    log_step("❌", tag, message)


def log_warn(tag: str, message: str) -> None:
    log_step("⚠️", tag, message)


def parse_proxy_response(text) -> dict | None:
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


def build_proxy_dict(proxy_info: dict | None) -> dict | None:
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

    log_step("🛠️", "代理", f"生成 {scheme.upper()} 代理 {host}:{port}")

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def validate_proxy(proxies: dict | None) -> tuple[bool, str]:
    if not proxies:
        return False, ""

    try:
        res = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if res.status_code == 200:
            try:
                ip = res.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            log_success("代理", f"验证通过，出口 IP: {ip}")
            return True, ip
    except Exception as exc:
        log_warn("代理", f"验证失败：{exc}")

    return False, ""


def get_valid_proxy(account_name: str) -> tuple[dict | None, str]:
    if not PROXY_API:
        log_warn("代理", f"{account_name} 未配置 PROXY_API，使用直连")
        return None, ""

    log_step("🌐", "代理", f"{account_name} 正在获取品赞代理...")

    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            res = direct_session().get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(res.text)

            if not proxy_info:
                log_warn("代理", f"第 {index} 次解析失败")
                continue

            log_success("代理", f"提取到 {proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)

            ok, ip = validate_proxy(proxies)
            if ok:
                return proxies, ip

            log_warn("代理", f"第 {index} 次代理不可用")
        except Exception as exc:
            log_warn("代理", f"第 {index} 次获取异常：{exc}")

        if index < PROXY_RETRY_TIMES:
            sleep(2)

    log_warn("代理", "连续获取失败，使用直连")
    return None, ""


def request_with_proxy(method: str, url: str, *, proxies: dict | None = None, server: str = "", **kwargs):
    kwargs.setdefault("timeout", 30)

    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            log_warn("代理", f"{server} 代理请求失败：{exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            log_step("🔁", "兜底", "切换直连重试")

    session = direct_session()
    return session.request(method, url, **kwargs)


def send_pushplus(title: str, content: str) -> None:
    if not PLUSPLUS_TOKEN:
        log_warn("PushPlus", "未配置 PLUSPLUS_TOKEN，跳过推送")
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
        log_success("PushPlus", "推送成功")
    except Exception as exc:
        log_error("PushPlus", f"推送失败：{exc}")


def parse_yyb_go_entry(raw_value):
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None, None

    if "@" not in raw_value:
        print(f"❌ 配置错误：YYB_SERVER 格式应为 地址@微信账号标识，当前值：{raw_value}")
        return None, None

    server, ref = raw_value.split("@", 1)
    server = server.strip()
    ref = ref.strip()

    if server.startswith("http://"):
        server = server[7:]
    elif server.startswith("https://"):
        server = server[8:]

    server = server.rstrip("/")

    if not server or not ref:
        print(f"❌ 配置错误：YYB_SERVER 缺少地址或微信账号标识，当前值：{raw_value}")
        return None, None

    return server, ref

def get_code(server: str) -> str | None:
    parsed_server, ref = parse_yyb_go_entry(server)
    if not parsed_server or not ref:
        return None

    url = f"http://{parsed_server}/wxapp/getCode"
    print(f"[{parsed_server}] 请求YYB Go获取code：{url}")

    try:
        res = requests.post(
            url,
            json={"ref": ref, "app_id": APPID},
            timeout=20,
            proxies={"http": None, "https": None},
        )
        data = res.json()
        code = (((data.get("data") or {}).get("result") or {}).get("code"))

        if data.get("code") != 0 or not code:
            print(f"[{parsed_server}] 获取code失败：{data}")
            return None

        print(f"[{parsed_server}] 获取code成功")
        return code
    except Exception as exc:
        print(f"[{parsed_server}] 获取code异常：{exc}")
        return None

def build_headers(
    store_id: str,
    visitor_uid: str | None = None,
    mode: str = "RegularOrder|package",
    include_visitor: bool = True,
) -> dict:
    headers = {
        "Host": "wxservice-stg62.pospal.cn",
        "Connection": "keep-alive",
        "PSPLVISITORAUTO": "API",
        "VERSIONINFO": "NC|2026.4.9",
        "STOREID": str(store_id),
        "xweb_xhr": "1",
        "APPTYPE": "1",
        "POSPALSTOREMODE": mode,
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": f"https://servicewechat.com/{APPID}/46/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    if include_visitor:
        headers["PSPLVISITORID"] = visitor_uid or ""

    return headers


def find_values_by_key(data, keys: set[str]) -> list:
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in keys and value not in (None, "", 0, "0", -1, "-1"):
                    found.append(value)
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return found


def extract_customer_uid(data) -> str | None:
    values = find_values_by_key(
        data,
        {"customerUid", "customer_uid", "customerUID", "customerId", "customer_id", "uid"},
    )
    for value in values:
        return str(value)
    return None


def extract_relogin_token(data) -> str | None:
    values = find_values_by_key(data, {"reloginToken", "ReloginToken", "reLoginToken"})
    for value in values:
        return str(value)
    return None


def extract_member_info(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}

    return {
        "nickname": data.get("nickName") or data.get("name") or "-",
        "phone": data.get("phone") or "-",
        "point": data.get("point", data.get("totalPoint", "-")),
        "category": data.get("categoryName") or "-",
    }


def auth_without_visitor(server: str, store_id: str, proxies: dict | None) -> tuple[str | None, dict | None]:
    code = get_code(server)
    if not code:
        return None, {"error": "获取 code 失败"}

    url = f"{BASE_URL}/wxapi/customeraccount/Auth"
    headers = build_headers(store_id, None, mode="RegularOrder|takeout", include_visitor=False)
    body = {
        "storeId": int(store_id),
        "signInMode": 1,
        "code": code,
    }

    log_step("🔐", "授权", f"Auth 不传 PSPLVISITORID，STOREID={store_id}")

    try:
        res = request_with_proxy(
            "POST",
            url,
            headers=headers,
            json=body,
            proxies=proxies,
            server=server,
            timeout=30,
        )

        try:
            data = res.json()
        except Exception:
            data = {"raw": res.text[:1000]}

        relogin_token = extract_relogin_token(data)

        if relogin_token:
            log_success("授权", f"reloginToken 获取成功 {mask_value(relogin_token)}")
            return relogin_token, data

        log_warn("授权", f"未获取到 reloginToken：{json_preview(data)}")
        return None, data
    except Exception as exc:
        return None, {"exception": str(exc)}


def call_customer_api(
    server: str,
    store_id: str,
    visitor_uid: str,
    path: str,
    body: dict,
    proxies: dict | None,
) -> dict:
    url = f"{BASE_URL}{path}"
    headers = build_headers(store_id, visitor_uid)

    try:
        res = request_with_proxy(
            "POST",
            url,
            headers=headers,
            json=body,
            proxies=proxies,
            server=server,
            timeout=30,
        )
        try:
            return res.json()
        except Exception:
            return {"successed": False, "messages": f"JSON解析失败: {res.text[:300]}"}
    except Exception as exc:
        return {"successed": False, "messages": str(exc)}


def find_login_info(
    server: str,
    store_id: str,
    visitor_uid: str,
    proxies: dict | None,
) -> tuple[str | None, dict, dict]:
    data = call_customer_api(
        server,
        store_id,
        visitor_uid,
        "/wxapi/customeraccount/FindLoginInfo",
        {
            "storeId": int(store_id),
            "isRefresh": True,
            "showCardAge": True,
            "isMemCenter": True,
            "incShoppingCard": False,
            "agreementVersion": "20220523",
            "incBirthdayChangeCount": False,
        },
        proxies,
    )

    customer_uid = extract_customer_uid(data)
    member_info = extract_member_info(data)

    if customer_uid:
        log_success(
            "会员",
            f"识别成功 昵称={member_info['nickname']} | 积分={member_info['point']} | UID={mask_value(customer_uid)}",
        )
    else:
        log_warn("会员", f"未识别会员：{json_preview(data)}")

    return customer_uid, data, member_info


def login_by_code(server: str, proxies: dict | None) -> tuple[str | None, str | None, str | None, dict | None, dict]:
    last_data = None

    for store_id in STORE_IDS:
        log_step("📍", "门店", f"尝试 STOREID={store_id}")

        relogin_token, auth_data = auth_without_visitor(server, store_id, proxies)
        last_data = auth_data

        if not relogin_token:
            continue

        customer_uid, login_info, member_info = find_login_info(server, store_id, relogin_token, proxies)
        last_data = login_info

        if customer_uid:
            log_success("登录", f"登录识别成功 STOREID={store_id}")
            return store_id, customer_uid, relogin_token, login_info, member_info

    return None, None, None, last_data, {}


def query_checkin_points(
    server: str,
    store_id: str,
    visitor_uid: str,
    proxies: dict | None,
) -> tuple[bool, dict | None, bool]:
    data = call_customer_api(
        server,
        store_id,
        visitor_uid,
        "/wxapi/customeraccount/FindCheckinPointsNew",
        {"range": 1},
        proxies,
    )

    if data.get("successed") is False and data.get("messages"):
        log_warn("签到", f"查询失败：{data.get('messages')}")
        return False, data, False

    points = data.get("result")
    if isinstance(points, list) and points:
        today_checked = any(bool(point.get("todayChecked")) for point in points)

        for index, point in enumerate(points, 1):
            log_step(
                "🎯",
                "签到",
                f"记录{index} customerUid={point.get('customerUid')} | 积分={point.get('gaintPoint', 0)} | 今日={'已签' if point.get('todayChecked') else '未签'}",
            )

        return True, data, today_checked

    log_step("🎯", "签到", "暂无签到记录，视为未签到")
    return True, data, False


def real_sign_in(
    server: str,
    store_id: str,
    visitor_uid: str,
    proxies: dict | None,
) -> tuple[bool, str]:
    data = call_customer_api(
        server,
        store_id,
        visitor_uid,
        "/wxapi/customeraccount/Checkin",
        {
            "longitude": 0,
            "latitude": 0,
            "address": "",
            "isMemberCard": False,
            "memberCardNo": "",
        },
        proxies,
    )

    if data.get("successed"):
        log_success("真实签到", "签到接口返回成功")
        return True, "真实签到成功"

    msg = data.get("messages") or data.get("message") or data.get("msg") or "未知错误"
    if isinstance(msg, list):
        msg = "；".join(str(item) for item in msg)

    log_warn("真实签到", str(msg))
    return False, f"真实签到失败：{msg}"


def web_report_sign_in(
    server: str,
    store_id: str,
    customer_uid: str,
    visitor_uid: str,
    proxies: dict | None,
) -> tuple[bool, str]:
    params = {
        "reportKey": "2000113",
        "value": "https://imgw.pospal.cn/wkbprod/images/4758527/5deaf14c-0c3c-4076-a3db-76dc66848836.png",
        "version": "2026.4.9",
        "channel": "WX_1027",
        "traceId": str(int(time.time() * 1000)),
        "userId": store_id,
        "visitorUid": visitor_uid,
        "customerUid": customer_uid,
    }

    headers = {
        "Host": "webreport.pospal.cn",
        "Connection": "keep-alive",
        "User-Agent": UA,
        "xweb_xhr": "1",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": f"https://servicewechat.com/{APPID}/46/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    try:
        res = request_with_proxy(
            "GET",
            WEB_REPORT_URL,
            headers=headers,
            params=params,
            proxies=proxies,
            server=server,
            timeout=30,
        )
        try:
            data = res.json()
        except Exception:
            data = {"raw": res.text[:300]}

        if data.get("status") == "success":
            log_success("网页上报", "web_report 上报成功")
            return True, "网页报告上报成功"

        log_warn("网页上报", json_preview(data, 300))
        return False, f"网页报告上报失败：{json_preview(data, 300)}"
    except Exception as exc:
        log_warn("网页上报", f"请求异常：{exc}")
        return False, f"网页报告请求异常：{exc}"


def run_account(index: int, total: int, server: str) -> dict:
    result = {
        "server": server,
        "success": False,
        "proxy_status": "未使用代理",
        "proxy_ip": "-",
        "store_id": "-",
        "visitor_uid": "-",
        "customer_uid": "-",
        "nickname": "-",
        "phone": "-",
        "point": "-",
        "category": "-",
        "before_status": "-",
        "real_sign": "-",
        "web_report": "-",
        "after_status": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxy_status"] = "使用专属代理" if proxies else "使用直连"
    result["proxy_ip"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    log_step("⏳", "延迟", f"启动延迟 {delay}s")
    sleep(delay)

    store_id, customer_uid, visitor_uid, raw, member_info = login_by_code(server, proxies)

    if not store_id or not customer_uid or not visitor_uid:
        result["error"] = f"登录识别失败，最后响应：{json_preview(raw, 800)}"
        log_error("账号", result["error"])
        return result

    result["store_id"] = store_id
    result["customer_uid"] = mask_value(customer_uid)
    result["visitor_uid"] = mask_value(visitor_uid)
    result["nickname"] = member_info.get("nickname", "-")
    result["phone"] = member_info.get("phone", "-")
    result["point"] = member_info.get("point", "-")
    result["category"] = member_info.get("category", "-")

    log_step("🪪", "会员", f"昵称={result['nickname']} | 等级={result['category']} | 积分={result['point']}")

    _, _, today_checked_before = query_checkin_points(server, store_id, visitor_uid, proxies)
    result["before_status"] = "已签到" if today_checked_before else "未签到"

    if today_checked_before:
        result["success"] = True
        result["real_sign"] = "今日已签到，跳过"
        result["web_report"] = "今日已签到，跳过"
        result["after_status"] = "已签到"
        log_success("账号", "今日已签到，账号处理完成")
        return result

    real_ok, real_msg = real_sign_in(server, store_id, visitor_uid, proxies)
    result["real_sign"] = real_msg

    sleep(2)

    web_ok, web_msg = web_report_sign_in(server, store_id, customer_uid, visitor_uid, proxies)
    result["web_report"] = web_msg

    sleep(5)

    _, _, today_checked_after = query_checkin_points(server, store_id, visitor_uid, proxies)
    result["after_status"] = "已签到" if today_checked_after else "未签到"

    result["success"] = bool(today_checked_after or real_ok or web_ok)
    if result["success"]:
        log_success("账号", "账号处理完成")
    else:
        result["error"] = "签到后状态未更新，且签到接口未成功"
        log_error("账号", result["error"])

    return result


def build_notify(results: list[dict]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍰 都市甜心多账号签到结果

{line()}
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
{line()}
"""

    for index, res in enumerate(results, 1):
        status_icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {index}
🌍 来源：{res["server"]}
🌐 代理：{res["proxy_status"]}
📡 出口IP：{res["proxy_ip"]}
📍 STOREID：{res["store_id"]}
👤 昵称：{res["nickname"]}
🎖️ 等级：{res["category"]}
💰 积分：{res["point"]}
🆔 customerUid：{res["customer_uid"]}
🎯 签到前：{res["before_status"]}
📝 真实签到：{res["real_sign"]}
📡 网页上报：{res["web_report"]}
🎯 签到后：{res["after_status"]}
{status_icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += line() + "\n"

    return content


def main() -> None:
    log_title()

    results = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
            results.append(result)
            ref = server.rsplit("@", 1)[-1].strip()
            if result.get("success"):
                mark_ready(ref, app_id=APPID)
            else:
                mark_from_error(ref, result.get("error", ""), app_id=APPID)
        except Exception as exc:
            mark_from_error(server.rsplit("@", 1)[-1].strip(), str(exc), app_id=APPID)
            log_error("主程序", f"{server} 执行异常：{exc}")
            results.append({
                "server": server,
                "success": False,
                "proxy_status": "-",
                "proxy_ip": "-",
                "store_id": "-",
                "visitor_uid": "-",
                "customer_uid": "-",
                "nickname": "-",
                "phone": "-",
                "point": "-",
                "category": "-",
                "before_status": "-",
                "real_sign": "-",
                "web_report": "-",
                "after_status": "-",
                "error": str(exc),
            })

        if index < len(SERVERS):
            log_step("⏳", "间隔", "等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 48 + "╗")
    print("║ 🏁 都市甜心任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<37}║")
    print(f"║ ❌ 失败: {fail_count:<37}║")
    print(f"║ 🕒 结束时间: {now_text():<31}║")
    print("╚" + "═" * 48 + "╝")

    notify_content = build_notify(results)
    send_pushplus("🍰 都市甜心多账号签到完成", notify_content)


if __name__ == "__main__":
    main()
