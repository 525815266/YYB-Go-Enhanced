#!/usr/bin/env python3
# name: 铛铛一下
# cron: 32 16 * * *
# -*- coding: utf-8 -*-

"""
铛铛一下旧衣服回收动态 code 版

功能：
  1. 多地址本地服务获取微信 code（从环境变量 YYB_SERVER 读取，换行分隔）
  2. /wechat/login 使用 code 换 token
  3. 每日签到
  4. 抽奖
  5. 查询余额
  6. 满 0.3 自动提现
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER             必填：wxcode服务地址，多地址换行填写
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  DD1X_CHANNEL_ID   渠道 ID，默认 154

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
from yyb_account_guard import filter_accounts, update_from_result


APP_NAME = "铛铛一下旧衣服回收小程序"
APPID = "wxe378d2d7636c180e"

# ========== 修改点：从环境变量 YYB_SERVER 读取服务地址，换行分割 ==========
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

SERVERS = filter_accounts(SERVERS, app_id="wxe378d2d7636c180e", log=print)

print(f"✅ 读取到 {len(SERVERS)} 个 YYB Go 账号")
print("-" * 60 + "\n")
# =================================================================

CHANNEL_ID = os.getenv("DD1X_CHANNEL_ID", "154")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://vues.dd1x.cn"
LOGIN_URL = f"{BASE_URL}/wechat/login"

SIGN_JOIN_URL = f"{BASE_URL}/api/v2/sign_join"
LOTTERY_INFO_URL = f"{BASE_URL}/front/activity/get_lottery_info?id=13&channelId={CHANNEL_ID}"
LOTTERY_RESULT_URL = f"{BASE_URL}/front/activity/get_lottery_result?id=13"
LOTTERY_UPDATE_URL = f"{BASE_URL}/front/activity/update_lottery_result"
ACCOUNT_DETAIL_URL = f"{BASE_URL}/api/h/get_account_detailed"
WITHDRAWAL_TRADE_LIST_URL = f"{BASE_URL}/api/h/get_withdrawal_trade_list"
WITHDRAWAL_URL = f"{BASE_URL}/api/h/withdrawal"

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


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ ♻️ 铛铛一下旧衣服回收动态 code 版             ║")
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

def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/824/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["token"] = token
    return headers


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

        user = inner.get("user")
        if isinstance(user, dict):
            candidates.extend([
                user.get("token"),
                user.get("accessToken"),
                user.get("access_token"),
                user.get("jwt"),
            ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "GET",
            LOGIN_URL,
            headers=common_headers(),
            params={
                "code": code,
                "channelId": CHANNEL_ID,
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


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


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token),
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


def normalize_trade_list(resp: Any) -> List[Dict[str, Any]]:
    if isinstance(resp, list):
        return [item for item in resp if isinstance(item, dict)]

    if not isinstance(resp, dict):
        return []

    data = resp.get("data")

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        for key in ("list", "records", "tradeList", "withdrawalDetailPojoList"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


def build_withdraw_payload(trade_resp: Any) -> Tuple[Dict[str, Any] | None, str]:
    trade_list = normalize_trade_list(trade_resp)

    if not trade_list:
        return None, "没有可提现订单数据"

    available = []
    for item in trade_list:
        if item.get("disabled") is True:
            continue

        money = to_float(item.get("money"))
        if money <= 0:
            continue

        available.append(item)

    if not available:
        return None, "没有可提现订单"

    total_money = round(sum(to_float(item.get("money")) for item in available), 2)

    if total_money < 0.3:
        return None, f"可提现金额 {total_money:.2f} 元，未满 0.3 元"

    return {
        "totalMoney": f"{total_money:.2f}",
        "type": 1,
        "withdrawalDetailPojoList": available,
    }, f"可提现订单 {len(available)} 个，合计 {total_money:.2f} 元"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "lotteryMsg": "-",
        "balance": "-",
        "withdrawMsg": "-",
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

    code = get_code(server)
    if not code:
        result["error"] = "获取 code 失败"
        return result

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        sign_resp = api_get(server, SIGN_JOIN_URL, token, proxies)
        if sign_resp.get("code") == 0:
            sign_name = sign_resp.get("data", {}).get("name", "签到成功")
            result["signMsg"] = f"每日签到: {sign_name}"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            result["signMsg"] = sign_resp.get("msg") or sign_resp.get("message") or "签到失败"
            print(f"⚠️ [签到] {result['signMsg']}")

        lottery_info = api_get(server, LOTTERY_INFO_URL, token, proxies)
        member_count = int(lottery_info.get("data", {}).get("member_count", 0) or 0)
        print(f"🎰 [抽奖] 当前可抽奖 {member_count} 次")

        prize_list: List[str] = []
        for draw_index in range(1, member_count + 1):
            wait_time = random.randint(2, 5)
            print(f"⏳ [抽奖] 第 {draw_index} 次抽奖前等待 {wait_time}s")
            sleep(wait_time)

            draw_resp = api_get(server, LOTTERY_RESULT_URL, token, proxies)
            if draw_resp.get("code") != 0:
                msg = draw_resp.get("msg") or draw_resp.get("message") or "抽奖失败"
                prize_list.append(f"第{draw_index}次失败: {msg}")
                print(f"❌ [抽奖] {msg}")
                continue

            data = draw_resp.get("data") or {}
            prize_name = data.get("prizeName") or data.get("goodName") or "未知奖品"
            record_id = data.get("record_id") or data.get("recordId") or data.get("id")
            prize_list.append(prize_name)
            print(f"✅ [抽奖] 第 {draw_index} 次获得: {prize_name}")

            if record_id:
                update_url = f"{LOTTERY_UPDATE_URL}?id={quote(str(record_id))}"
                update_resp = api_get(server, update_url, token, proxies)
                if update_resp.get("code") == 0:
                    print("✅ [抽奖] 奖品结果确认成功")
                else:
                    print(f"⚠️ [抽奖] 奖品结果确认失败: {json_preview(update_resp, 300)}")

        result["lotteryMsg"] = "、".join(prize_list) if prize_list else f"{member_count} 次机会"

        account_resp = api_get(server, ACCOUNT_DETAIL_URL, token, proxies)
        total_raw = account_resp.get("data", {}).get("total", 0)
        total = to_float(total_raw)

        result["balance"] = str(total_raw)
        print(f"💰 [余额] 当前总金额: {total_raw} 元")

        if total < 0.3:
            result["withdrawMsg"] = "余额不足 0.3 元，跳过提现"
            print(f"⚠️ [提现] {result['withdrawMsg']}")
            result["success"] = True
            return result

        trade_resp = api_get(server, WITHDRAWAL_TRADE_LIST_URL, token, proxies)
        payload, withdraw_prepare_msg = build_withdraw_payload(trade_resp)
        print(f"💸 [提现] {withdraw_prepare_msg}")

        if not payload:
            result["withdrawMsg"] = withdraw_prepare_msg
            result["success"] = True
            return result

        withdraw_resp = api_post(server, WITHDRAWAL_URL, token, proxies, payload)
        result["withdrawMsg"] = (
            withdraw_resp.get("msg")
            or withdraw_resp.get("message")
            or json_preview(withdraw_resp)
        )
        print(f"💸 [提现] {result['withdrawMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""♻️ 铛铛一下旧衣服回收多账号任务结果

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
📝 签到：{res["signMsg"]}
🎰 抽奖：{res["lotteryMsg"]}
💰 余额：{res["balance"]} 元
💸 提现：{res["withdrawMsg"]}
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
            update_from_result(server, result, app_id="wxe378d2d7636c180e")
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
                "lotteryMsg": "-",
                "balance": "-",
                "withdrawMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 铛铛一下任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("♻️ 铛铛一下多账号任务完成", build_notify(results))


if __name__ == "__main__":
    main()
