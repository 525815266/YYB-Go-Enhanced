#!/usr/bin/env python3
# name: 途虎养车
# cron: 24 8 * * *
# -*- coding: utf-8 -*-

"""
途虎养车动态 code 签到版

功能：
  1. 多端口本地服务获取微信 code
  2. 使用 code 登录换取 userSession
  3. 查询签到状态和当前积分
  4. 未签到则提交签到
  5. 统计签到前积分、签到后积分、获得积分
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

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
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from yyb_account_guard import filter_accounts, update_from_result


APP_NAME = "途虎养车小程序"
APPID = "wx27d20205249c56a3"

# 从环境变量 YYB_SERVER 读取内网服务，多条换行分隔
SERVERS = []
env_YYB_SERVER = os.getenv("YYB_SERVER", "")
if env_YYB_SERVER:
    raw_lines = env_YYB_SERVER.splitlines()
    SERVERS = [line.strip() for line in raw_lines if line.strip()]

# 无有效地址直接退出并提示
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

BASE_URL = "https://cl-gateway.tuhu.cn"
LOGIN_URL = f"{BASE_URL}/cl-user-auth-login/login/authSilentSign"
SIGN_INFO_URL = f"{BASE_URL}/cl-common-api/api/member/getSignInInfo"
SIGN_SUBMIT_URL = f"{BASE_URL}/cl-common-api/api/dailyCheckIn/userCheckIn"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541938) XWEB/19823"
)

REFERER = f"https://servicewechat.com/{APPID}/1319/page-frame.html"


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
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🚗 途虎养车动态 code 签到版                  ║")
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

def common_headers(user_session: str | None = None) -> Dict[str, str]:
    headers = {
        "Host": "cl-gateway.tuhu.cn",
        "Connection": "keep-alive",
        "orion_biz_gps_latitude": "22.787150540279182",
        "orion_biz_gps_province": "%E5%B9%BF%E8%A5%BF%E5%A3%AE%E6%97%8F%E8%87%AA%E6%B2%BB%E5%8C%BA",
        "xweb_xhr": "1",
        "distinct_id": "6a68cbca-ce9a-4b0e-8092-cc5a85cf9a85",
        "currentPage": "memberMallPackage/pages/pointCenter/pointCenter",
        "orion_biz_gps_city": "%E5%8D%97%E5%AE%81%E5%B8%82",
        "deviceId": f"{int(time.time() * 1000)}-{random.randint(1000000, 9999999)}-0f6cb850fc64da-24853921",
        "authType": "oauth",
        "api_level": "2",
        "vehicleClass": "CAR",
        "channel": "wechat-miniprogram",
        "Content-Type": "application/json",
        "fingerprint": f"sMPVY{int(time.time())}QPV2wLVhl8f",
        "orion_biz_gps_longitude": "108.27980328217664",
        "User-Agent": USER_AGENT,
        "version": "7.62.8",
        "Accept": "*/*",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": REFERER,
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    if user_session:
        headers["Authorization"] = f"Bearer {user_session}"

    return headers


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str, Dict[str, Any] | None]:
    print("🔐 [登录] 使用 code 换 userSession")

    payload = {
        "channel": "WXAPP",
        "code": code,
    }

    try:
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json=payload,
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        if data.get("code") == 10000:
            user_data = data.get("data") or {}
            user_session = user_data.get("userSession")
            nick_name = user_data.get("nickName") or "微信用户"

            if user_session:
                print(f"✅ [登录] {nick_name} 登录成功: {mask(user_session)}")
                return user_session, nick_name, data

        print(f"❌ [登录] 登录失败: {json_preview(data)}")
        return None, "-", data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, "-", None


def api_post(server: str, url: str, user_session: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    payload = {
        "channel": "WXAPP",
    }

    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(user_session),
        json=payload,
        proxies=proxies,
        server=server,
    )

    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "message": f"JSON解析失败: {response.text[:300]}",
        }


def parse_sign_info(data: Dict[str, Any]) -> Tuple[bool | None, int]:
    if data.get("code") != 10000:
        return None, 0

    info = data.get("data") or {}
    sign_status = bool(info.get("signInStatus", False))
    user_integral = to_int(info.get("userIntegral", 0))

    return sign_status, user_integral


def get_sign_info(server: str, user_session: str, proxies: Dict[str, str] | None) -> Tuple[bool | None, int, Dict[str, Any]]:
    data = api_post(server, SIGN_INFO_URL, user_session, proxies)
    sign_status, user_integral = parse_sign_info(data)

    if sign_status is None:
        print(f"⚠️ [积分] 查询失败: {json_preview(data)}")
    else:
        print(f"📊 [积分] 当前积分: {user_integral}")

    return sign_status, user_integral, data


def submit_signin(server: str, user_session: str, proxies: Dict[str, str] | None) -> Tuple[bool, str, int, int]:
    data = api_post(server, SIGN_SUBMIT_URL, user_session, proxies)

    if data.get("code") == 10000:
        result = data.get("data") or {}
        reward_integral = to_int(result.get("rewardIntegral", 0))
        continuous_days = to_int(result.get("continuousDays", 0))
        msg = f"签到成功 +{reward_integral}积分，连续签到{continuous_days}天"
        return True, msg, reward_integral, continuous_days

    message = data.get("message") or data.get("msg") or "签到失败"
    if "已签到" in message or "重复" in message:
        return True, "今日已签到", 0, 0

    return False, message, 0, 0


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "nickname": "-",
        "session": "-",
        "signMsg": "-",
        "beforeIntegral": "0",
        "afterIntegral": "0",
        "earnedIntegral": "0",
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

    user_session, nick_name, raw_login = login_by_code(server, code, proxies)
    if not user_session:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["nickname"] = nick_name
    result["session"] = mask(user_session)

    try:
        print("📊 [积分] 查询签到前积分和状态...")
        sign_status, before_integral, raw_info = get_sign_info(server, user_session, proxies)

        if sign_status is None:
            result["error"] = f"查询签到状态失败: {json_preview(raw_info)}"
            return result

        result["beforeIntegral"] = str(before_integral)

        if sign_status:
            result["signMsg"] = "今日已签到"
            result["afterIntegral"] = str(before_integral)
            result["earnedIntegral"] = "0"
            print("✅ [签到] 今日已签到")
        else:
            print("📝 [签到] 未签到，开始签到...")
            sign_ok, sign_msg, reward_integral, _ = submit_signin(server, user_session, proxies)
            result["signMsg"] = sign_msg
            result["earnedIntegral"] = str(reward_integral)

            if sign_ok:
                print(f"✅ [签到] {sign_msg}")
            else:
                print(f"⚠️ [签到] {sign_msg}")
                result["error"] = sign_msg
                return result

            sleep(random.randint(1, 2))
            print("📊 [积分] 查询签到后积分...")
            _, after_integral, raw_after = get_sign_info(server, user_session, proxies)
            result["afterIntegral"] = str(after_integral)

            if reward_integral == 0:
                result["earnedIntegral"] = str(after_integral - before_integral)

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count
    total_earned = sum(to_int(item.get("earnedIntegral", 0)) for item in results if item.get("success"))

    content = f"""🚗 途虎养车多账号任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
💰 总获得积分：{total_earned}
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
👤 昵称：{res["nickname"]}
🔐 Session：{res["session"]}
📝 签到：{res["signMsg"]}
💰 签到前：{res["beforeIntegral"]} 积分
💰 签到后：{res["afterIntegral"]} 积分
💰 获得：{res["earnedIntegral"]} 积分
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
            update_from_result(server, result, app_id=APPID)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "nickname": "-",
                "session": "-",
                "signMsg": "-",
                "beforeIntegral": "0",
                "afterIntegral": "0",
                "earnedIntegral": "0",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 途虎养车任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🚗 途虎养车多账号任务完成", build_notify(results))


if __name__ == "__main__":
    main()
