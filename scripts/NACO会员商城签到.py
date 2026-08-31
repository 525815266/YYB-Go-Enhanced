#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
NACO会员商城小程序签到动态 code 版

功能：
  1. 通过 YYB Go 按账号获取微信 code
  2. https://uic.youzan.com/passport/general/auth.json 使用 code 换 token
  3. 每日签到（已签到自动识别）
  4. 查询会员等级 / 积分 / 当月签到天数
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER        YYB Go 服务地址，格式：地址@微信账号标识，多账号换行分隔
  CODE_SERVER       旧版单账号 code 服务地址（兼容项）
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
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

APP_NAME = "NACO会员商城签到"
APPID = "wxf7eb51e2639162f9"
KDT_ID = "41131244"
CHECKIN_ID = os.getenv("NACO_CHECKIN_ID", "8231")
APP_EMOJI = "🛍️"

# 青龙环境变量：YYB_SERVER=YYB地址@账号ID或OpenID，多账号每行一个。
# 保留 CODE_SERVER 作为旧版单账号配置的兼容入口。
_YYB_SERVER_RAW = os.getenv("YYB_SERVER", "")
SERVERS = [line.strip() for line in _YYB_SERVER_RAW.splitlines() if line.strip()]
if not SERVERS and os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER", "").strip()]
if not SERVERS:
    print("❌ 未配置 YYB_SERVER（格式：地址@微信账号标识，多账号换行分隔）")
    raise SystemExit(1)

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://h5.youzan.com"
LOGIN_URL = f"https://uic.youzan.com/passport/general/auth.json?kdt_id={KDT_ID}&app_id={APPID}"
SIGN_URL = f"{BASE_URL}/wscump/checkin/checkinV2.json"
CHECK_URL = f"{BASE_URL}/wscaccount/api/authorize/data.json"
ACTIVITY_URL = f"{BASE_URL}/wscump/checkin/get_activity_by_yzuid_v2.json"
MONTH_SIGN_URL = f"{BASE_URL}/wscump/checkin/find_checkin_info_by_month.json"
INIT_DATA_URL = f"{BASE_URL}/wscuser/membercenter/init-data.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541c37) XWEB/25364"
)
USER_VERSION = "2.247.4"
USER_UUID = "D1SnUph076t2k3f1787749992996"
USER_FTIME = 1787749992995

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nacocookie.json")


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
    print(f"║ {APP_EMOJI} {APP_NAME:<44}║")
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
    return {"http": proxy_url, "https": proxy_url}


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


def parse_yyb_entry(raw: str) -> Tuple[str, str]:
    """解析 YYB_SERVER：地址@微信账号标识。"""
    raw = str(raw or "").strip()
    if "@" not in raw:
        # 兼容旧版 CODE_SERVER=地址（旧服务的 /login 接口不需要 ref）。
        server = raw.rstrip("/")
        if "://" in server:
            server = server.split("://", 1)[1]
        return server, ""
    server, ref = raw.split("@", 1)
    server = server.strip().rstrip("/")
    if "://" in server:
        server = server.split("://", 1)[1]
    ref = ref.strip()
    if not server or not ref:
        print(f"❌ YYB_SERVER 地址或账号标识为空：{raw}")
        return "", ""
    return server, ref


def get_code(entry: str) -> str | None:
    server, ref = parse_yyb_entry(entry)
    if not server:
        return None
    if not ref:
        # 仅兼容旧版本地 code 服务；YYB_SERVER 应始终使用 地址@账号标识。
        url = f"http://{server}/login"
        print(f"🔐 [授权] 请求旧版 code 服务: {url}")
        try:
            response = direct_session().get(url, params={"appId": APPID}, timeout=20)
            data = response.json()
            code = data.get("code")
            if data.get("err") == 0 and code:
                print("✅ [授权] code 获取成功")
                return str(code)
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
        except Exception as exc:
            print(f"❌ [授权] code 获取异常: {exc}")
        return None
    url = f"http://{server}/wxapp/getCode"
    print(f"🔐 [授权] 请求 YYB code 服务: {url}（账号 {ref}）")
    try:
        response = direct_session().post(
            url,
            json={"ref": ref, "app_id": APPID},
            timeout=20,
        )
        data = response.json()
        result = data.get("data") or {}
        if isinstance(result, dict):
            result = result.get("result") or result
        code_value = result.get("code") if isinstance(result, dict) else None
        if not code_value and isinstance(data.get("result"), dict):
            code_value = data["result"].get("code")
        if not code_value and isinstance(data.get("code"), str):
            code_value = data.get("code")
        if not code_value or str(code_value).lower() in ("null", "none"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None
        print("✅ [授权] code 获取成功")
        return code_value
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def common_headers(token: str | None = None, session_id: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/147/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    sid = session_id or token or ""
    if sid:
        headers["Cookie"] = f"KDTSESSIONID={sid}; yz_log_seqb=1"
        headers["Extra-Data"] = json.dumps(
            {
                "is_weapp": 1,
                "sid": sid,
                "version": USER_VERSION,
                "clientType": "weapp-miniprogram",
                "client": "weapp",
                "bizEnv": "wsc",
                "uuid": USER_UUID,
                "ftime": USER_FTIME,
            },
            separators=(",", ":"),
        )
    return headers


def api_request(
    method: str,
    url: str,
    token: str = "",
    session_id: str = "",
    params: Dict[str, Any] | None = None,
    body: Dict[str, Any] | None = None,
    proxies: Dict[str, str] | None = None,
    server: str = "",
) -> Dict[str, Any]:
    try:
        params = dict(params or {})
        if token:
            params.setdefault("app_id", APPID)
            params.setdefault("kdt_id", KDT_ID)
            params.setdefault("access_token", token)
        kwargs: Dict[str, Any] = {
            "headers": common_headers(token, session_id),
        }
        if params:
            kwargs["params"] = params
        if method.upper() == "GET":
            response = request_with_proxy(method.upper(), url, proxies=proxies, server=server, **kwargs)
        else:
            kwargs["json"] = body or {}
            response = request_with_proxy(method.upper(), url, proxies=proxies, server=server, **kwargs)
        return response.json()
    except Exception as exc:
        return {"code": -1, "msg": f"请求异常: {exc}"}


def login_by_code(
    server: str,
    code: str,
    proxies: Dict[str, str] | None,
) -> Tuple[str | None, str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        body = {
            "appId": APPID,
            "code": code,
            "platformName": "weapp",
            "signature": "windows",
            "clientBiz": "weapp_wsc",
            "inWsc": True,
            "kdtId": KDT_ID,
            "extraBizData": {
                "enterOptions": {
                    "extKdtId": int(KDT_ID),
                    "path": "pages/home/dashboard/index",
                    "query": {},
                    "scene": 1005,
                    "referrerInfo": {},
                    "hostExtraData": {},
                    "apiCategory": "default",
                },
                "guideBizDataMap": {"from_params": ""},
                "sceneData": {},
            },
        }
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json=body,
            proxies=proxies,
            server=server,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        inner = data.get("data") or {}
        token = (
            inner.get("accessToken")
            or inner.get("access_token")
            or inner.get("token")
            or inner.get("sessionId")
        )
        session_id = inner.get("sessionId") or inner.get("session_id") or token
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, session_id, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None, None


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


def set_cached_token(
    server: str,
    token: str,
    session_id: str = "",
    nickname: str = "",
    user_id: str = "",
) -> None:
    cache = load_token_cache()
    cache[server] = {
        "token": token,
        "sessionId": session_id,
        "nickname": nickname,
        "userId": user_id,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def remove_cached_token(server: str) -> None:
    cache = load_token_cache()
    if server in cache:
        del cache[server]
        save_token_cache(cache)


def check_cached_session(
    server: str,
    token: str,
    session_id: str,
    proxies: Dict[str, str] | None,
) -> Tuple[bool, Dict[str, Any]]:
    print("🔍 [缓存] 验证 token")
    check = api_request(
        "GET",
        CHECK_URL,
        token=token,
        session_id=session_id,
        params={"appId": APPID, "kdtId": KDT_ID, "secure": 1},
        proxies=proxies,
        server=server,
    )
    user_info = (check.get("data") or {}).get("userInfo") or {}
    if check.get("code") == 0 and user_info.get("hasLogin"):
        print("✅ [缓存] token 有效")
        return True, check
    print("⚠️ [缓存] token 已失效，重新登录")
    return False, check


def login_with_cache(
    server: str,
    proxies: Dict[str, str] | None,
) -> Tuple[str | None, str | None, Dict[str, Any] | None]:
    cache = load_token_cache().get(server) or {}
    cache_token = cache.get("token")
    cache_session = str(cache.get("sessionId") or "")
    if cache_token:
        ok, raw = check_cached_session(server, cache_token, cache_session, proxies)
        if ok:
            return cache_token, cache_session, raw
        remove_cached_token(server)

    code = get_code(server)
    if not code:
        print("⚠️ [授权] code 无效，等待后重试")
        sleep(2)
        code = get_code(server)
    if not code:
        return None, None, None

    token, session_id, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, None, raw_login

    inner = raw_login.get("data") or {}
    nickname = inner.get("nickname") or inner.get("nickName") or ""
    user_id = inner.get("userId") or inner.get("buyerId") or ""
    set_cached_token(server, token, session_id or "", nickname, user_id)
    return token, session_id, raw_login


def pick_msg(data: Any) -> str:
    if not isinstance(data, dict):
        return json_preview(data, 200)
    for key in ("msg", "message", "Message", "errMsg", "retInfo", "desc", "info"):
        if data.get(key):
            return str(data[key])
    return json_preview(data, 200)


def is_already_done(data: Any) -> bool:
    if isinstance(data, dict) and data.get("code") in (1000030071, "1000030071"):
        return True
    text = pick_msg(data)
    return any(k in text for k in ("已签", "已经签", "签到过", "重复", "已完成", "今日已", "already"))


def is_auth_error(data: Any) -> bool:
    text = pick_msg(data)
    return any(k in text for k in ("token", "登录", "未授权", "未登录", "失效", "过期", "401", "403"))


def get_points(server: str, token: str, session_id: str, proxies: Dict[str, str] | None) -> Tuple[str, str]:
    try:
        resp = api_request(
            "GET",
            INIT_DATA_URL,
            token=token,
            session_id=session_id,
            params={
                "kdtId": KDT_ID,
                "version": USER_VERSION,
                "onlineKdtId": KDT_ID,
                "currentKdtId": KDT_ID,
                "needConsumptionAboveCoupon": 1,
            },
            proxies=proxies,
            server=server,
        )
        data = resp.get("data") or {}
        level = (data.get("level") or {}).get("levelName") or "未知等级"
        points = (data.get("member") or {}).get("stats") or {}
        points_value = points.get("points") or 0
        return str(level), str(points_value)
    except Exception as exc:
        print(f"⚠️ [积分] 查询异常: {exc}")
        return "未知等级", "-"


def get_sign_days(server: str, token: str, session_id: str, proxies: Dict[str, str] | None) -> str:
    try:
        now = datetime.now()
        resp = api_request(
            "GET",
            MONTH_SIGN_URL,
            token=token,
            session_id=session_id,
            params={
                "checkin_id": CHECKIN_ID,
                "year": now.year,
                "month": now.month,
            },
            proxies=proxies,
            server=server,
        )
        dates = (resp.get("data") or {}).get("checkin_date") or []
        return f"{len(dates)} 天"
    except Exception as exc:
        print(f"⚠️ [签到天数] 查询异常: {exc}")
        return "-"


def do_sign(server: str, token: str, session_id: str, proxies: Dict[str, str] | None) -> Dict[str, str]:
    result = {
        "signMsg": "-",
        "continuesDay": "-",
    }
    try:
        activity = api_request(
            "GET",
            ACTIVITY_URL,
            token=token,
            session_id=session_id,
            params={"checkinId": CHECKIN_ID},
            proxies=proxies,
            server=server,
        )
        if activity.get("code") == 0:
            activity_data = activity.get("data") or {}
            result["continuesDay"] = str(activity_data.get("continuesDay", "-"))
            if activity_data.get("isCheckin"):
                result["signMsg"] = f"今日已签到，连续 {activity_data.get('continuesDay', 0)} 天"
                print(f"✅ [签到] {result['signMsg']}")
                return result
        elif is_auth_error(activity):
            result["signMsg"] = f"签到失败: {pick_msg(activity)}"
            return result

        sign = api_request(
            "GET",
            SIGN_URL,
            token=token,
            session_id=session_id,
            params={"checkinId": CHECKIN_ID},
            proxies=proxies,
            server=server,
        )
        if sign.get("code") == 0:
            sign_data = sign.get("data") or {}
            reward_list = sign_data.get("list") or []
            reward = ""
            if reward_list:
                reward = (reward_list[0].get("infos") or {}).get("title") or ""
            result["signMsg"] = f"{sign_data.get('desc') or '签到成功'}: {reward}" if reward else (sign_data.get("desc") or "签到成功")
            print(f"✅ [签到] {result['signMsg']}")
        elif is_already_done(sign):
            result["signMsg"] = f"今日已签到: {pick_msg(sign)}"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            result["signMsg"] = f"签到失败: {pick_msg(sign)}"
            print(f"⚠️ [签到] {result['signMsg']}")

        after = api_request(
            "GET",
            ACTIVITY_URL,
            token=token,
            session_id=session_id,
            params={"checkinId": CHECKIN_ID},
            proxies=proxies,
            server=server,
        )
        if after.get("code") == 0:
            result["continuesDay"] = str((after.get("data") or {}).get("continuesDay", result["continuesDay"]))
        return result
    except Exception as exc:
        result["signMsg"] = f"签到请求异常: {exc}"
        print(f"❌ [签到] {result['signMsg']}")
        return result


def extract_user(raw_login: Dict[str, Any] | None) -> Dict[str, Any]:
    if not raw_login:
        return {}
    inner = raw_login.get("data") or {}
    if isinstance(inner.get("userInfo"), dict):
        return inner["userInfo"]
    return inner


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "nickname": "-",
        "userId": "-",
        "level": "-",
        "points": "-",
        "signMsg": "-",
        "signDays": "-",
        "continuesDay": "-",
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

    token, session_id, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)
    user = extract_user(raw_login)
    result["nickname"] = user.get("nickname") or user.get("nickName") or "未知用户"
    result["userId"] = str(user.get("userId") or user.get("buyerId") or "-")
    print(f"👤 [用户] 昵称: {result['nickname']}, ID: {result['userId']}")

    try:
        level, points = get_points(server, token, session_id, proxies)
        result["level"] = level
        result["points"] = points
        print(f"⭐ [等级] {level}, 积分: {points}")

        result["signDays"] = get_sign_days(server, token, session_id, proxies)
        print(f"📅 [签到] 当月签到: {result['signDays']}")

        sign_result = do_sign(server, token, session_id, proxies)
        result["signMsg"] = sign_result["signMsg"]
        result["continuesDay"] = sign_result["continuesDay"]

        if is_auth_error({"msg": result["signMsg"]}):
            print("🔁 [刷新] token 失效，重新获取 code 后重试")
            remove_cached_token(server)
            token, session_id, raw_login = login_with_cache(server, proxies)
            if not token:
                result["error"] = f"登录失败: {json_preview(raw_login)}"
                return result
            result["token"] = mask(token)
            sign_result = do_sign(server, token, session_id, proxies)
            result["signMsg"] = sign_result["signMsg"]
            result["continuesDay"] = sign_result["continuesDay"]

        result["success"] = True
        return result
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🛍️ NACO会员商城任务结果

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
🆔 用户ID：{res["userId"]}
🔐 Token：{res["token"]}
⭐ 等级：{res["level"]}
💰 积分：{res["points"]}
📝 签到：{res["signMsg"]}
📅 当月签到：{res["signDays"]}
🔁 连续签到：{res["continuesDay"]}
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
                "userId": "-",
                "level": "-",
                "points": "-",
                "signMsg": "-",
                "signDays": "-",
                "continuesDay": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 NACO会员商城任务执行完成                ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🛍️ NACO会员商城签到任务完成", build_notify(results))


if __name__ == "__main__":
    main()
