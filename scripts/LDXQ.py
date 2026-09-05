#!/usr/bin/env python3
# name: 绿动新球
# cron: 0 12 * * *
# -*- coding: utf-8 -*-
 
"""
绿动新球小程序签到 code 版
 
功能：
  1. 四端口本地服务获取微信 code
  2. 使用 code 换取 token
  3. 每日签到
  4. PushPlus 推送
  5. 品赞代理，业务请求优先代理，失败直连兜底
 
环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  LVDONG_TOKEN      绿动token（可选，支持直接使用token模式）
 
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
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote
 
import requests
from yyb_account_guard import filter_accounts, update_from_result
 
# 禁用SSL警告
import warnings
from urllib3.exceptions import InsecureRequestWarning
warnings.simplefilter('ignore', InsecureRequestWarning)
 
 
APP_NAME = "绿动新球小程序签到"
APPID = "wxa61a45f180dec800"
 
# 从环境变量 YYB_SERVER 读取内网 IP，多个 IP 用换行分隔
SERVERS = [
    s.strip()
    for s in os.getenv("YYB_SERVER", "").splitlines()
    if s.strip()
]

if not SERVERS:
    print("❌ 未配置环境变量 YYB_SERVER")
    print("格式：地址@微信账号标识，多账号换行分隔")
    print("  或")
    print("  YYB_SERVER=127.0.0.1:8088\\n192.168.31.36:8088\\n192.168.31.88:8088")
    exit(1)

SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)
 
PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
LVDONG_TOKEN = os.getenv("LVDONG_TOKEN", "")
 
PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30
 
API_HOST = "lvdong.fzjingzhou.com"
LOGIN_URL = f"https://{API_HOST}/api/login/getWxMiniProgramSessionKey"
SIGN_URL = f"https://{API_HOST}/api/Person/sign"
USER_INFO_URL = f"https://{API_HOST}/api/Person/index"
 
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
 
 
def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🌿 绿动新球小程序签到 code 版                   ║")
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
        from urllib.parse import quote
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
    kwargs.setdefault("verify", False)
 
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
        "Content-Type": "application/x-www-form-urlencoded",
        "Platform": "MP-WEIXIN",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/4/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Accept-Encoding": "gzip, deflate, br",
    }
 
 
def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换取 token")
        post_data = {
            "code": code,
            "gdtVid": "",
            "token": ""
        }
 
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            data=post_data,
            proxies=proxies,
            server=server,
        )
 
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}
 
        if data.get("code") == 1000 and data.get("data", {}).get("token"):
            token = data["data"]["token"]
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data
 
        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None
 
 
def get_user_info(server: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        USER_INFO_URL,
        headers=common_headers(),
        data={"token": token},
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
 
 
def daily_sign(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    try:
        print("📝 [签到] 执行签到操作")
        response = request_with_proxy(
            "POST",
            SIGN_URL,
            headers=common_headers(),
            data={"token": token},
            proxies=proxies,
            server=server,
        )
 
        try:
            data = response.json()
        except Exception:
            return False, f"响应解析错误: {response.text[:300]}"
 
        code_status = data.get("code")
        msg = data.get("msg", "未知错误")
 
        if code_status == 1000:
            if "签到" in msg and "成功" in msg:
                msg = f"签到成功！{msg}"
                print(f"✅ [签到] {msg}")
                return True, msg
            elif "已签到" in msg or "今天已签到" in msg:
                print(f"⚠️ [签到] {msg}")
                return True, msg
            else:
                print(f"✅ [签到] {msg}")
                return True, msg
        elif code_status == 1001:
            print(f"⚠️ [签到] {msg}")
            return True, msg
        else:
            print(f"❌ [签到] {msg} (code:{code_status})")
            return False, msg
    except Exception as exc:
        msg = f"请求异常: {exc}"
        print(f"❌ [签到] {msg}")
        return False, msg
 
 
def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "nickname": "-",
        "score": "-",
        "signMsg": "-",
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
 
    token = None
 
    if LVDONG_TOKEN:
        print("🔐 [登录] 使用环境变量中的 LVDONG_TOKEN")
        token = LVDONG_TOKEN
    else:
        code = get_code(server)
        if not code:
            result["error"] = "获取 code 失败"
            return result
 
        token, raw_login = login_by_code(server, code, proxies)
        if not token:
            result["error"] = f"登录失败: {json_preview(raw_login)}"
            return result
 
    result["token"] = mask(token)
 
    user_info = get_user_info(server, token, proxies)
    if user_info.get("code") == 1000:
        result["nickname"] = user_info.get("data", {}).get("nickname", "-")
        result["score"] = user_info.get("data", {}).get("score", 0)
        print(f"👤 [用户] 昵称: {result['nickname']}, 积分: {result['score']}")
 
    sign_success, sign_msg = daily_sign(server, token, proxies)
    result["signMsg"] = sign_msg
    result["success"] = sign_success
 
    return result
 
 
def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count
 
    content = f"""🌿 绿动新球四账号签到结果
 
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
👤 昵称：{res["nickname"]}
🔐 Token：{res["token"]}
📝 签到：{res["signMsg"]}
💰 积分：{res["score"]}
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
                "token": "-",
                "nickname": "-",
                "score": "-",
                "signMsg": "-",
                "error": traceback.format_exc().strip(),
            })
 
        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)
 
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count
 
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 绿动新球任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")
 
    send_pushplus("🌿 绿动新球四账号签到完成", build_notify(results))
 
 
if __name__ == "__main__":
    main()
 
