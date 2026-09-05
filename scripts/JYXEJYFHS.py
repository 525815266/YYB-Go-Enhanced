#!/usr/bin/env python3
# name: 旧衣小二旧衣服回收
# cron: 48 12 * * *
# -*- coding: utf-8 -*-
 
"""
旧衣小二旧衣服回收小程序签到脚本（code 版）
 
功能：
  1. 四端口本地服务获取微信 code
  2. 使用 code 换取 token
  3. 每日签到
  4. 查询积分余额
  5. 查询积分明细
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底
 
环境变量：
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
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote
 
import requests
from yyb_account_guard import filter_accounts, update_from_result
 
 
APP_NAME = "旧衣小二旧衣服回收小程序"
APPID = "wx426d52c8130b8559"
 
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
 
PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30
 
BASE_URL = "https://jiuyixiaoer.fzjingzhou.com"
LOGIN_URL = f"{BASE_URL}/api/login/getWxMiniProgramSessionKey"
SIGN_URL = f"{BASE_URL}/api/Person/sign"
PERSON_INFO_URL = f"{BASE_URL}/api/Person/index"
SCORE_LIST_URL = f"{BASE_URL}/api/Cash/scoreList"
 
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541938) XWEB/19823"
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
    print("║ ♻️ 旧衣小二旧衣服回收小程序签到                 ║")
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
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "platform": "MP-WEIXIN",
        "Referer": f"https://servicewechat.com/{APPID}/5/page-frame.html",
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
 
        person = inner.get("personInfo")
        if isinstance(person, dict):
            candidates.extend([
                person.get("token"),
                person.get("accessToken"),
                person.get("access_token"),
                person.get("jwt"),
            ])
 
    for item in candidates:
        if item and item != "null":
            return str(item)
 
    return None
 
 
def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            data={
                "code": code,
                "gdtVid": "",
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
 
 
def api_post_form(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token),
        data=payload,
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
        "nickname": "-",
        "scoreBefore": "-",
        "scoreAfter": "-",
        "signMsg": "-",
        "scoreChange": 0,
        "recentScores": "-",
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
        person_resp = api_post_form(server, PERSON_INFO_URL, token, proxies, {"token": token})
        if person_resp.get("code") == 1000:
            person_data = person_resp.get("data", {})
            nickname = person_data.get("nickname", "-")
            score_before = person_data.get("score", 0)
            sign_in_num = person_data.get("sign_in_num", 0)
            result["nickname"] = nickname
            result["scoreBefore"] = str(score_before)
            print(f"👤 [信息] 昵称: {nickname}")
            print(f"💰 [积分] 当前积分: {score_before}")
            print(f"✅ [签到] 今日已签到: {'是' if sign_in_num > 0 else '否'}")
        else:
            result["error"] = f"获取个人信息失败: {person_resp.get('msg', '未知错误')}"
            return result
 
        sign_resp = api_post_form(server, SIGN_URL, token, proxies, {"token": token})
        sign_code = sign_resp.get("code", 0)
        sign_msg = sign_resp.get("msg", "")
        result["signMsg"] = sign_msg
 
        if sign_code == 1000:
            print(f"✅ [签到] 签到成功: {sign_msg}")
        elif sign_code == 1001:
            print(f"⚠️ [签到] 今日已签到")
        else:
            print(f"❌ [签到] 签到失败: {sign_msg}")
 
        person_after = api_post_form(server, PERSON_INFO_URL, token, proxies, {"token": token})
        if person_after.get("code") == 1000:
            person_data_after = person_after.get("data", {})
            score_after = person_data_after.get("score", 0)
            result["scoreAfter"] = str(score_after)
            result["scoreChange"] = int(score_after) - int(score_before)
 
            if result["scoreChange"] > 0:
                print(f"📈 [积分] 获得 {result['scoreChange']} 积分，当前: {score_after}")
            elif result["scoreChange"] < 0:
                print(f"📉 [积分] 减少 {abs(result['scoreChange'])} 积分，当前: {score_after}")
            else:
                print(f"📊 [积分] 积分未变化: {score_after}")
 
        score_list_resp = api_post_form(server, SCORE_LIST_URL, token, proxies, {
            "page": 1,
            "limit": 5,
            "token": token
        })
        if score_list_resp.get("code") == 1000:
            score_data = score_list_resp.get("data", {})
            scores = score_data.get("data", [])[:3]
            if scores:
                score_records = []
                for s in scores:
                    memo = s.get("memo", "-")
                    score = s.get("score", 0)
                    createtime = s.get("createtime", 0)
                    if createtime:
                        from datetime import datetime
                        time_str = datetime.fromtimestamp(createtime).strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        time_str = "-"
                    score_records.append(f"{time_str} {memo} {'+' if score > 0 else ''}{score}")
                result["recentScores"] = "\\n".join(score_records)
                print(f"📋 [积分] 最近记录:")
                for record in score_records:
                    print(f"    {record}")
            else:
                print(f"📋 [积分] 暂无积分记录")
 
        result["success"] = True
        return result
 
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result
 
 
def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count
 
    content = f"""♻️ 旧衣小二旧衣服回收签到任务结果
 
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
💰 签到前积分：{res["scoreBefore"]}
💰 签到后积分：{res["scoreAfter"]}
📈 积分变化：{'+' if res["scoreChange"] > 0 else ''}{res["scoreChange"]}
📝 签到：{res["signMsg"]}
📋 最近积分记录：
{res["recentScores"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""
 
        if not res["success"]:
            content += f"❌ 原因：{res['error']}\\n"
 
        content += "━━━━━━━━━━━━━━━━━━━━\\n"
 
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
                "scoreBefore": "-",
                "scoreAfter": "-",
                "signMsg": "-",
                "scoreChange": 0,
                "recentScores": "-",
                "error": traceback.format_exc().strip(),
            })
 
        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)
 
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count
 
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 旧衣小二签到任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")
 
    send_pushplus("♻️ 旧衣小二签到任务完成", build_notify(results))
 
 
if __name__ == "__main__":
    main()
 
