#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换登录信息（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → FansInfoByOpenid 换 openid/fansid → 缓存到本地 JSON；
#       下次运行先读取缓存登录信息，并调用积分接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
察理王子 chictea 动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. FansInfoByOpenid 使用 code 换登录信息
  3. 每日签到（查询签到数据 + EnsureSign 签到）
  4. 查询积分余额
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN     PushPlus token，可选
  PROXY_API          品赞代理提取 API，可选
  PROXY_TYPE         http / socks5，默认 http
  CHICTEA_ACT_ID     签到活动 ID，默认 26

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


APP_NAME = "察理王子 chictea 小程序"
APPID = "wxb6bd6fed8bff6e3c"
WEID = "373"
ACT_ID = os.getenv("CHICTEA_ACT_ID", "26")

_SERVER_ENV = os.getenv("YYB_SERVER", "").strip()
SERVERS = [item.strip() for item in _SERVER_ENV.replace(",", "\n").splitlines() if item.strip()]
YYB_API_KEY = os.getenv("YYB_API_KEY", "").strip()

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://minigx.wxdw.top"
API_URL = f"{BASE_URL}/mobile.php"

LOGIN_URL = f"{API_URL}?act=module&name=mpapimember&do=FansInfoByOpenid&weid={WEID}"
SIGNIN_DATA_URL = f"{API_URL}?act=module&name=mpapisignin&do=GetSigninData"
SIGN_JOIN_URL = f"{API_URL}?act=module&name=mpapisignin&do=EnsureSign"
POINTS_URL = f"{API_URL}?act=module&name=mpapishare&do=GetFansPointsAmount"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chicteacookie.json")

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
    """Safely extract 'message' from an API response, handling null/missing."""
    return resp.get("message") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🧋 察理王子 chictea 动态 code 版             ║")
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

def get_code(server: str) -> str | None:
    try:
        endpoint, ref = parse_yyb_entry(server)
        url = f"{endpoint}/wxapp/getCode"
        headers = {"X-API-Key": YYB_API_KEY} if YYB_API_KEY else {}
        print(f"🔐 [授权] YYB 获取 code（账号 {mask(ref)}）")
        response = direct_session().post(
            url,
            json={"ref": ref, "app_id": APPID},
            headers=headers,
            timeout=20,
        )
        data = response.json()

        result = data.get("data") or {}
        result = result.get("result") if isinstance(result, dict) else {}
        code = result.get("code") if isinstance(result, dict) else None
        if data.get("code") != 0 or not code:
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        print("✅ [授权] code 获取成功")
        return str(code)
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def parse_yyb_entry(raw: str) -> Tuple[str, str]:
    value = str(raw or "").strip()
    if "@" not in value:
        raise ValueError("YYB_SERVER 格式应为 地址@账号ID或OpenID")
    endpoint, ref = value.split("@", 1)
    endpoint = endpoint.strip().rstrip("/")
    ref = ref.strip()
    if not endpoint or not ref:
        raise ValueError("YYB_SERVER 缺少地址或账号标识")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"http://{endpoint}"
    return endpoint, ref


def common_headers() -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/93/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def build_form(session_info: Dict[str, Any] | None, extra: Dict[str, Any] | None = None) -> Dict[str, str]:
    data = {
        "noTip": "false",
        "weid": WEID,
        "mp_openid": (session_info or {}).get("openid", ""),
        "phoneNumber": (session_info or {}).get("phone", ""),
        "mp_type": "1",
        "fansid": (session_info or {}).get("fansid", ""),
        "appId": APPID,
        "platformAppId": "",
        "version": "4.3.71",
        "platformType": "0",
        "onlyThePhoneNumber": "1",
        "apiDebug": "0",
        "sxfPayType": "0",
        "scene_code": "0",
    }
    if extra:
        data.update({str(k): str(v) for k, v in extra.items()})
    return data


def extract_session(data: Any) -> Dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    msg = data.get("message")
    if not isinstance(msg, dict):
        return None

    fans_info = msg.get("fansInfo")
    if not isinstance(fans_info, dict):
        return None

    fansid = fans_info.get("id")
    openid = msg.get("openid") or fans_info.get("openid")
    if not fansid or not openid:
        return None

    return {
        "openid": str(openid),
        "fansid": str(fansid),
        "phone": str(fans_info.get("mobile") or msg.get("mobile") or ""),
        "sessionKey": str(msg.get("session_key") or ""),
        "unionid": str(msg.get("unionid") or ""),
        "cardsn": str(fans_info.get("cardsn") or ""),
    }


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换登录信息")
        response = request_with_proxy(
            "GET",
            LOGIN_URL,
            headers=common_headers(),
            params={
                "code": code,
                "appId": APPID,
                "mp_type": "1",
                "onlyThePhoneNumber": "1",
                "logOut": "0",
                "bind_commission": "2",
                "loginTimeType": "1",
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        session_info = extract_session(data)
        if session_info:
            print(f"✅ [登录] openid 获取成功: {mask(session_info['openid'])}")
            print(f"✅ [登录] fansid 获取成功: {session_info['fansid']}")
            return session_info, data

        print(f"❌ [登录] 未识别登录信息: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_post(server: str, url: str, session_info: Dict[str, Any], proxies: Dict[str, str] | None, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(),
        data=build_form(session_info, extra),
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


# ====================== 登录信息缓存管理 ======================
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
        print("✅ [缓存] 登录信息保存成功")
    except Exception as exc:
        print(f"❌ [缓存] 保存失败: {exc}")


def get_cached_session(server: str) -> Dict[str, Any] | None:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("openid") and data.get("fansid") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} 登录信息")
                return data
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_session(server: str, session_info: Dict[str, Any], expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {
        **session_info,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    """优先使用缓存登录信息（积分接口验证），失效自动 code 刷新"""
    cached = get_cached_session(server)
    if cached:
        print("🔍 [缓存] 验证登录信息")
        try:
            points_resp = api_post(server, POINTS_URL, cached, proxies)
            if safe_data(points_resp).get("points") is not None:
                print("✅ [缓存] 登录信息有效")
                return cached, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] 登录信息已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None

    session_info, raw_login = login_by_code(server, code, proxies)
    if not session_info:
        return None, raw_login

    set_cached_session(server, session_info, datetime.fromtimestamp(time.time() + 24 * 3600).isoformat())
    return session_info, raw_login

def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "pointsMsg": "-",
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

    session_info, raw_login = login_with_cache(server, proxies)
    if not session_info:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(session_info["openid"])

    try:
        signin_data = api_post(server, SIGNIN_DATA_URL, session_info, proxies, {"act_id": ACT_ID})
        signin_msg = safe_data(signin_data)
        signed_today = False
        sign_dates = signin_msg.get("signDates") or []
        sign_record_date = signin_msg.get("signRecordDate") or ""
        if isinstance(sign_dates, list):
            day = datetime.now().day
            day_markers = {f"this{day}", f"this{day:02d}", str(day), f"{day:02d}"}
            signed_today = bool(set(sign_dates) & day_markers) or sign_record_date in day_markers

        if signed_today:
            result["signMsg"] = f"今日已签到（连续 {signin_msg.get('signCount', 0)} 天）"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            sign_resp = api_post(server, SIGN_JOIN_URL, session_info, proxies, {"act_id": ACT_ID})
            sign_result = safe_data(sign_resp).get("signinResult") or {}
            if sign_resp.get("message", {}).get("code") == 1:
                if isinstance(sign_result, dict) and sign_result.get("type") == "coupon":
                    coupon_title = sign_result.get("title") or sign_result.get("couponType") or "优惠券"
                    result["signMsg"] = f"签到成功，获得: {coupon_title}"
                elif isinstance(sign_result, dict) and sign_result.get("msg"):
                    result["signMsg"] = sign_result["msg"]
                else:
                    result["signMsg"] = "签到成功"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                result["signMsg"] = sign_resp.get("message", {}).get("msg") or sign_resp.get("msg") or "签到失败"
                print(f"⚠️ [签到] {result['signMsg']}")

        points_resp = api_post(server, POINTS_URL, session_info, proxies)
        points_msg = safe_data(points_resp)
        points = points_msg.get("points") or "0"
        amount = points_msg.get("amount") or "0"
        result["pointsMsg"] = f"当前积分 {points}，余额 {amount} 元"
        print(f"💰 [积分] {result['pointsMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🧋 察理王子 chictea 签到任务结果

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
🔐 Openid：{res["token"]}
📝 签到：{res["signMsg"]}
💰 积分：{res["pointsMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    if not SERVERS:
        print("❌ 未配置 YYB_SERVER，格式：地址@账号ID或OpenID，每行一个")
        return

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
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
                "pointsMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 察理王子签到任务执行完成                  ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🧋 察理王子签到任务完成", build_notify(results))


if __name__ == "__main__":
    main()
