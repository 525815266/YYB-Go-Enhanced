#!/usr/bin/env python3
# name: 奈雪的茶
# cron: 2 10 * * *
# -*- coding: utf-8 -*-
"""
脚本名称：奈雪点单动态code签到
说明：
  通过本地登录服务获取Code，再调用奈雪登录接口换 token 后执行签到。

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取链接，可选
  PROXY_TYPE        代理类型：http / socks5，默认 http
  YYB_SERVER             内网wxcode服务地址，多个换行分隔，格式：192.168.1.21:8088
"""

import base64
import hashlib
import hmac
import json
import os
import random
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
from yyb_account_guard import filter_accounts, update_from_result


# ===================== 配置项 =====================

APPID = "wxab7430e6e8b9a4ab"
# 从环境变量 YYB_SERVER 读取内网服务地址，多条换行分隔
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

print(f"✅ 读取到 {len(SERVERS)} 个 YYB Go 账号")
SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)
print("-" * 60 + "\n")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")

PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
ENABLE_PER_ACCOUNT_PROXY = True
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True

OPEN_ID = "QL6ZOftGzbziPlZwfiXM"
SIGN_SECRET = "sArMTldQ9tqU19XIRDMWz7BO5WaeBnrezA"

LOGIN_URL = "https://tm-api.pin-dao.cn/passport/authenticate/wxapp/verify/grc"

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541923) XWEB/19823",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781 NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF XWEB/19725",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781 NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF XWEB/19613",
]


# ===================== 工具函数 =====================

def sleep(seconds: float) -> None:
    time.sleep(seconds)


def rand_sleep(min_s: int = 2, max_s: int = 5) -> None:
    sleep(random.randint(min_s, max_s))


def get_ua() -> str:
    return random.choice(UA_LIST)


def random_int_string(length: int) -> str:
    return "".join(random.choice("123456789") for _ in range(length))


def hmac_sha1_base64(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def build_request_data(extra_params: dict | None = None) -> dict:
    nonce = random_int_string(6)
    timestamp = int(time.time())
    url_path = f"nonce={nonce}&openId={OPEN_ID}&timestamp={timestamp}"
    signature = hmac_sha1_base64(SIGN_SECRET, url_path)

    common = {
        "platform": "wxapp",
        "version": "6.0.42",
        "imei": "",
        "osn": "microsoft",
        "sv": "Windows 10 x64",
        "lat": "",
        "lng": "",
        "lang": "zh_CN",
        "currency": "CNY",
        "timeZone": "",
        "nonce": int(nonce),
        "openId": OPEN_ID,
        "timestamp": timestamp,
        "signature": signature,
    }

    params = {
        "businessType": 1,
        "brand": 26000252,
        "tenantId": 1,
        "channel": 2,
        "stallType": None,
        "storeId": "",
        "storeType": "",
        "cityId": "",
    }

    if extra_params:
        params.update(extra_params)

    return {
        "common": common,
        "params": params,
    }


def china_date_parts() -> tuple[int, int, int]:
    now = datetime.now(timezone(timedelta(hours=8)))
    return now.year, now.month, now.day


def mask_phone(phone: str) -> str:
    phone = str(phone or "")
    if len(phone) >= 11:
        return f"{phone[:3]}****{phone[7:]}"
    return phone or "未知"


# ===================== 品赞代理 =====================

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

    if PROXY_TYPE == "socks5":
        proxy_url = f"socks5://{auth}{host}:{port}"
    else:
        proxy_url = f"http://{auth}{host}:{port}"

    print(f"生成代理：{proxy_url}")
    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def validate_proxy(proxies: dict | None) -> bool:
    if not proxies:
        return False

    try:
        res = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if res.status_code == 200:
            try:
                ip = res.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            print(f"代理验证通过，出口IP：{ip}")
            return True
    except Exception as exc:
        print(f"代理验证失败：{exc}")

    return False


def get_valid_proxy(account_name: str) -> dict | None:
    if not PROXY_API:
        print(f"[{account_name}] 未配置 PROXY_API，使用直连")
        return None

    print(f"[{account_name}] 正在获取品赞代理...")

    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            res = requests.get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(res.text)

            if not proxy_info:
                print(f"[{account_name}] 第 {index} 次代理解析失败")
                continue

            print(f"[{account_name}] 提取到代理：{proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)

            if validate_proxy(proxies):
                return proxies

            print(f"[{account_name}] 第 {index} 次代理不可用")
        except Exception as exc:
            print(f"[{account_name}] 第 {index} 次获取代理异常：{exc}")

        if index < PROXY_RETRY_TIMES:
            sleep(2)

    print(f"[{account_name}] 获取代理失败，使用直连")
    return None


# ===================== PushPlus =====================

def send_pushplus(title: str, content: str) -> None:
    if not PLUSPLUS_TOKEN:
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
        print("PushPlus 推送成功")
    except Exception as exc:
        print(f"PushPlus 推送失败：{exc}")


# ===================== 请求封装 =====================

def request_with_proxy(method: str, url: str, *, proxies: dict | None = None, server: str = "", **kwargs):
    kwargs.setdefault("timeout", 30)

    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            print(f"[{server}] 代理请求失败：{exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            print(f"[{server}] 切换直连重试")

    return requests.request(method, url, **kwargs)


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

def extract_token(data) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
        data.get("authToken"),
        data.get("memberToken"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("authToken"),
            inner.get("memberToken"),
            inner.get("access_token_value"),
        ])

        token_info = inner.get("tokenInfo")
        if isinstance(token_info, dict):
            candidates.extend([
                token_info.get("token"),
                token_info.get("accessToken"),
                token_info.get("access_token"),
            ])

        user_token = inner.get("userToken")
        if isinstance(user_token, dict):
            candidates.extend([
                user_token.get("token"),
                user_token.get("accessToken"),
                user_token.get("access_token"),
            ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(code: str, ua: str, proxies: dict | None, server: str) -> tuple[str | None, dict | None]:
    headers = {
        "Host": "tm-api.pin-dao.cn",
        "Connection": "keep-alive",
        "Authorization": "Bearer null",
        "User-Agent": ua,
        "xweb_xhr": "1",
        "storeId": "",
        "Content-Type": "application/json",
        "iv": random_int_string(16),
        "Accept": "*/*",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": f"https://servicewechat.com/{APPID}/819/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    body = build_request_data({
        "appId": APPID,
        "dAId": "",
        "type": 3,
        "wxappCode": code,
        "regChannelCode": "|1027",
    })

    try:
        res = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=headers,
            data=json.dumps(body, separators=(",", ":"), ensure_ascii=False),
            proxies=proxies,
            server=server,
        )

        try:
            data = res.json()
        except Exception:
            data = {"raw": res.text[:500]}

        token = extract_token(data)
        if token:
            print(f"[{server}] 登录成功，已获取 token")
            return token, data

        print(f"[{server}] 登录成功但未识别 token 字段：{json.dumps(data, ensure_ascii=False)[:800]}")
        return None, data
    except Exception as exc:
        print(f"[{server}] 登录异常：{exc}")
        return None, None


def call_api(url: str, token: str, ua: str, proxies: dict | None, server: str, body: dict | None = None) -> dict:
    headers = {
        "User-Agent": ua,
        "Authorization": f"Bearer {token}",
        "Referer": "https://tm-web.pin-dao.cn/",
        "Origin": "https://tm-web.pin-dao.cn",
        "Content-Type": "application/json",
    }

    payload = build_request_data(body or {})

    try:
        res = request_with_proxy(
            "POST",
            url,
            headers=headers,
            data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            proxies=proxies,
            server=server,
        )
        return res.json()
    except Exception as exc:
        return {
            "code": -1,
            "message": str(exc),
        }


# ===================== 业务逻辑 =====================

def run_account(server: str, global_proxy: dict | None = None) -> dict:
    result = {
        "server": server,
        "success": False,
        "proxy_status": "未使用代理",
        "login_msg": "",
        "sign_msg": "",
        "coin": "-",
        "error": "",
    }

    print(f"\n===== 奈雪点单 - {server} 账号 =====")
    ua = get_ua()

    proxies = global_proxy
    if ENABLE_PER_ACCOUNT_PROXY:
        proxies = get_valid_proxy(server)
        result["proxy_status"] = "使用专属代理" if proxies else "使用直连"
        sleep(PROXY_FETCH_INTERVAL)

    try:
        delay = random.randint(2, 6)
        print(f"[{server}] 启动延迟 {delay}s")
        sleep(delay)

        code = get_code(server)
        if not code:
            result["error"] = "获取 code 失败"
            return result

        token, login_raw = login_by_code(code, ua, proxies, server)
        if not token:
            result["error"] = "登录失败或未识别 token 字段"
            return result

        result["login_msg"] = "登录成功"
        rand_sleep(2, 5)

        userinfo = call_api(
            "https://tm-web.pin-dao.cn/user/base-userinfo",
            token,
            ua,
            proxies,
            server,
            {},
        )

        if userinfo.get("code") != 0:
            result["error"] = f"查询用户信息失败：{userinfo.get('message') or '未知错误'}"
            return result

        phone = userinfo.get("data", {}).get("phone", "")
        print(f"[{server}] 登录账号：{mask_phone(phone)}")

        year, month, day = china_date_parts()
        sign_date = f"{year}-{month:02d}-01"
        today = f"{year}-{month:02d}-{day:02d}"

        sign_records = call_api(
            "https://tm-web.pin-dao.cn/user/sign/records",
            token,
            ua,
            proxies,
            server,
            {
                "signDate": sign_date,
                "startDate": today,
            },
        )

        if sign_records.get("code") != 0:
            result["sign_msg"] = f"查询签到失败：{sign_records.get('message') or '未知错误'}"
            print(f"[{server}] {result['sign_msg']}")
        else:
            status = bool(sign_records.get("data", {}).get("status"))
            count = sign_records.get("data", {}).get("signCount", "-")
            print(f"[{server}] 今天{'已' if status else '未'}签到，已签到 {count} 天")

            if status:
                result["sign_msg"] = f"今日已签到，累计 {count} 天"
            else:
                sign_save = call_api(
                    "https://tm-web.pin-dao.cn/user/sign/save",
                    token,
                    ua,
                    proxies,
                    server,
                    {
                        "signDate": today,
                    },
                )

                if sign_save.get("code") == 0 and sign_save.get("data", {}).get("flag"):
                    result["sign_msg"] = "签到成功"
                    print(f"[{server}] 签到成功")
                else:
                    result["sign_msg"] = f"签到失败：{sign_save.get('message') or '未知错误'}"
                    print(f"[{server}] {result['sign_msg']}")

        rand_sleep(2, 5)

        account = call_api(
            "https://tm-web.pin-dao.cn/user/account/user-account",
            token,
            ua,
            proxies,
            server,
            {},
        )

        if account.get("code") == 0:
            result["coin"] = account.get("data", {}).get("coin", "-")
            print(f"[{server}] 当前奈雪币：{result['coin']}")
        else:
            print(f"[{server}] 查询奈雪币失败：{account.get('message') or '未知错误'}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = str(exc)
        print(f"[{server}] 执行异常：{exc}")
        return result


def main() -> None:
    print("===== 奈雪点单动态code签到（YYB_SERVER多内网+品赞代理+PushPlus）=====\n")

    global_proxy = None
    if not ENABLE_PER_ACCOUNT_PROXY:
        global_proxy = get_valid_proxy("全局共用")

    results = []

    for index, server in enumerate(SERVERS, 1):
        res = run_account(server, global_proxy)
        update_from_result(server, res, app_id=APPID)
        results.append(res)

        if index < len(SERVERS):
            sleep(2)

    notify = "### 奈雪点单多账号任务执行结果\n"

    for res in results:
        notify += f"""
#### {res["server"]}
- 代理状态：{res["proxy_status"]}
- 执行状态：{"成功" if res["success"] else "失败"}
"""

        if res["success"]:
            notify += f"""- 登录结果：{res["login_msg"]}
- 签到结果：{res["sign_msg"]}
- 当前奈雪币：{res["coin"]}
"""
        else:
            notify += f"""- 失败原因：{res["error"]}
"""

    send_pushplus("奈雪点单多账号任务完成", notify)

    print("\n===== 所有账号执行完成 =====")


if __name__ == "__main__":
    main()
