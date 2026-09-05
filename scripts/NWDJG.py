#!/usr/bin/env python3
# name: 浓五的酒馆
# cron: 8 10 * * *
# -*- coding: utf-8 -*-

"""
浓五的酒馆小程序动态 code 版

功能：
  1. 多端口本地服务获取微信 code
  2. 使用 code 换 token
  3. 每日签到
  4. 查询用户信息和积分
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  YYB_SERVER             内网wxcode服务地址，多个换行分隔，格式：192.168.1.21:8088

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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from yyb_account_guard import filter_accounts, update_from_result


APP_NAME = "浓五的酒馆小程序"
APPID = "wxed3cf95a14b58a26"
PROMOTION_ID = "PI6a41ee59886bd1000a158d9b"

# 从环境变量 YYB_SERVER 读取内网服务，多条换行分隔
SERVERS = []
env_YYB_SERVER = os.getenv("YYB_SERVER", "")
if env_YYB_SERVER:
    raw_lines = env_YYB_SERVER.splitlines()
    SERVERS = [line.strip() for line in raw_lines if line.strip()]

# 校验无有效地址直接退出
if len(SERVERS) == 0:
    print("❌ 未配置环境变量 YYB_SERVER")
    print("格式：地址@微信账号标识，多账号换行分隔")
    print("192.168.1.21:8088")
    exit(1)

SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)

print(f"✅ 读取到 {len(SERVERS)} 个 YYB Go 账号")
print("-" * 60 + "\n")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://stdcrm.dtmiller.com"
LOGIN_URL = f"{BASE_URL}/std-weixin-mp-service/miniApp/custom/login"
USER_INFO_URL = f"{BASE_URL}/scrm-promotion-service/mini/wly/user/info"
SIGN_INFO_URL = f"{BASE_URL}/scrm-promotion-service/promotion/sign/userinfo"
SIGN_TODAY_URL = f"{BASE_URL}/scrm-promotion-service/promotion/sign/today"
POINTS_RECORD_URL = f"{BASE_URL}/scrm-promotion-service/mini/point/wly/balance/detail"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541a1d) XWEB/19899"
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


def to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🍺 浓五的酒馆小程序动态 code 版                ║")
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

def common_headers() -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json={
                "code": code,
                "appId": APPID,
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        print(f"🔍 [登录] 响应数据: {json_preview(data, 300)}")

        if data.get("code") == 0 and data.get("data"):
            token = data["data"]
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 登录失败: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    headers = common_headers()
    headers["Authorization"] = f"Bearer {token}"
    
    response = request_with_proxy(
        "GET",
        url,
        headers=headers,
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


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "userInfo": "-",
        "initialScore": 0,
        "finalScore": 0,
        "signMsg": "-",
        "signDetails": [],
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
        print(f"🔍 [用户] 开始查询用户信息...")
        user_info_resp = api_get(
            server,
            USER_INFO_URL,
            token,
            proxies
        )

        print(f"🔍 [用户] 响应数据: {json_preview(user_info_resp, 200)}")

        if user_info_resp.get("code") == 0 and user_info_resp.get("data"):
            member_data = user_info_resp["data"].get("member", {})
            grade_data = user_info_resp["data"].get("grade", {})
            points_balance = to_int(member_data.get("points", 0))
            member_name = member_data.get("nick_name", "未知")
            member_level = grade_data.get("level_name", "普通会员")

            result["initialScore"] = points_balance
            result["userInfo"] = f"{member_name} {member_level} 当前积分{points_balance}"

            print(f"✅ [用户] {result['userInfo']}")
        else:
            error_msg = user_info_resp.get("msg") or "获取用户信息失败"
            result["userInfo"] = error_msg
            print(f"⚠️ [用户] {result['userInfo']}")
            print(f"⚠️ [用户] 完整响应: {json_preview(user_info_resp, 500)}")

        sleep(2)

        # 获取签到信息
        sign_info_resp = api_get(
            server,
            f"{SIGN_INFO_URL}?promotionId={PROMOTION_ID}",
            token,
            proxies
        )

        print(f"🔍 [签到信息] 响应数据: {json_preview(sign_info_resp, 300)}")

        if sign_info_resp.get("code") == 0 and sign_info_resp.get("data"):
            sign_data = sign_info_resp["data"]
            sign_days = to_int(sign_data.get("signDays", 0))
            today_sign = sign_data.get("today", False)
            next_continuous_day = to_int(sign_data.get("nextContinuousDay", 0))
            sign_day_prize_name = sign_data.get("signDayPrizeName", "未知")

            print(f"📊 [签到] 已签到{sign_days}天，今日{'已' if today_sign else '未'}签到")
            print(f"📊 [签到] 下次连续签到: {next_continuous_day}天")
            
            # 执行签到
            sign_today_resp = api_get(
                server,
                f"{SIGN_TODAY_URL}?promotionId={PROMOTION_ID}",
                token,
                proxies
            )

            print(f"🔍 [签到] 响应数据: {json_preview(sign_today_resp, 300)}")

            if sign_today_resp.get("code") == 0 and sign_today_resp.get("data"):
                today_data = sign_today_resp["data"]
                prize = today_data.get("prize", {})
                goods_name = prize.get("goodsName", "无奖励")
                
                result["signMsg"] = f"签到成功 获得{goods_name} 连续{sign_days + 1}天"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                error_msg = sign_today_resp.get("msg") or "签到失败"
                result["signMsg"] = error_msg
                print(f"⚠️ [签到] {result['signMsg']}")
        else:
            error_msg = sign_info_resp.get("msg") or "获取签到信息失败"
            result["signMsg"] = error_msg
            print(f"⚠️ [签到] {result['signMsg']}")

        sleep(2)

        # 获取最终用户信息
        final_user_info_resp = api_get(
            server,
            USER_INFO_URL,
            token,
            proxies
        )

        if final_user_info_resp.get("code") == 0 and final_user_info_resp.get("data"):
            member_data = final_user_info_resp["data"].get("member", {})
            points_balance = to_int(member_data.get("points", 0))

            result["finalScore"] = points_balance
            score_change = points_balance - result["initialScore"]

            if score_change > 0:
                print(f"✅ [最终] 积分{points_balance} (本次+{score_change})")
            else:
                print(f"✅ [最终] 积分{points_balance}")
        else:
            print(f"⚠️ [最终] 获取最终用户信息失败")

        sleep(2)

        # 获取积分记录
        points_records_resp = api_get(
            server,
            f"{POINTS_RECORD_URL}?type=0&pageNo=1&pageSize=10",
            token,
            proxies
        )

        if points_records_resp.get("code") == 0 and points_records_resp.get("data"):
            records_data = points_records_resp["data"]
            records_list = records_data.get("list", [])

            if records_list:
                result["signDetails"] = []
                print(f"📋 [明细] 最近{len(records_list)}条积分记录：")
                for item in records_list[:5]:
                    source_remark = item.get("sourceRemark", "")
                    number = to_int(item.get("number", 0))
                    created_time = item.get("createdTime", "")

                    result["signDetails"].append({
                        "type": source_remark,
                        "points": number,
                        "time": created_time,
                    })

                    print(f"  {created_time} {source_remark} {number}积分")
            else:
                print("ℹ️ [明细] 暂无积分记录")
        else:
            print(f"⚠️ [明细] 获取积分记录失败：{points_records_resp.get('msg')}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    total_score = sum(item.get("finalScore", 0) for item in results)

    content = f"""🍺 浓五的酒馆多账号任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
💎 总积分：{total_score}
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
👤 用户：{res["userInfo"]}
📝 签到：{res["signMsg"]}
"""

        score_change = res["finalScore"] - res["initialScore"]
        if score_change > 0:
            content += f"📊 积分变化：{res['initialScore']} -> {res['finalScore']} (+{score_change})\n"
        else:
            content += f"📊 当前积分：{res['finalScore']}\n"

        if res.get("signDetails"):
            content += "📋 积分记录：\n"
            for detail in res["signDetails"][:3]:
                content += f"   {detail['time']} {detail['type']} {detail['points']}积分\n"

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
            update_from_result(server, result, app_id=APPID)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "userInfo": "-",
                "initialScore": 0,
                "finalScore": 0,
                "signMsg": "-",
                "signDetails": [],
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    total_score = sum(item.get("finalScore", 0) for item in results)

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 浓五的酒馆任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 💎 总积分: {total_score:<38}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🍺 浓五的酒馆多账号任务完成", build_notify(results))


if __name__ == "__main__":
    main()
