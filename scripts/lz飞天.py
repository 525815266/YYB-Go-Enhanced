#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → getOpenId 绑定 → 抓包 xcxData decode 换 token → 缓存；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
lz飞天 登录/签到小程序动态 code 版

功能：
  1. 通过 YYB 获取微信 code
  2. https://gszy.baijqr.cn/api/cloud2.member.api/wx/getOpenId 使用 code 绑定 openid
  3. https://gszy.baijqr.cn/api/cloud2.member.api/member/userInfo/decode 使用抓包 xcxData 换 token
  4. 每日签到
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  YYB_SERVER        YYB 地址@账号 ID/OpenID，每行一个账号
  YYB_API_KEY       YYB API Key，可选
  LZ_CODE_FALLBACK  仅联调时使用抓包 code 兜底，默认 0

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
import sys
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass



APP_NAME = 'lz飞天 登录/签到'
APPID = 'wxda7924fb8811699c'
APP_EMOJI = '✈️'

_SERVER_ENV = os.getenv("YYB_SERVER", "").strip()
SERVERS = [item.strip() for item in _SERVER_ENV.replace(",", "\n").splitlines() if item.strip()]
SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)
YYB_API_KEY = os.getenv("YYB_API_KEY", "").strip()

# 8088 当前没有小程序会话时返回 code=null；抓包中的 code 仍可用于联调。
HAR_FALLBACK_CODE = os.getenv("LZ_FALLBACK_CODE", "0b1F2H0w3zUEE732Et1w36FgPz1F2H0R")
CODE_FALLBACK_ENABLED = os.getenv("LZ_CODE_FALLBACK", "0") != "0"

# H5 页面从微信小程序带过来的 xcxData，decode 后可解出 userToken。
XCX_DATA = "OTd6SVFIT0JteHErdHhaMzVNcUJOQzZwVEJzTGJ1d1pRVCs1dlYvR0VmUlZVa3diSDRYR0hwdE9neHhaejVGYkVtTFhqa0xGaHBJekpFd29UOHhNK3VNOE9yNEVwWUlITzZiZUtLdHYvaE5nQjRJR0dOUkZoTnF2QXRUMk1iSzFuRGRmdFUvelVCa3kwQk84Qnl4eDk1OVZtanBid3Z1Sk1YUlRoekQ3akdNQk1yYXYzUStIcU5rM21jWkhWRTJIdnFBUnNyV3FhRG5rdVhUSkpJTEh0L24rMkdvd1VMUHd1cGVyNlpYdytqMkZBUjh5ZjNnaFIyUjg3bGpwUE4zd0wxTUhlWXdjc21pelJOYzJ6Wng5ZmZuMnJtai8wTnREd3Iwb0Yzenc4dkpXYTk1UWNRMC80Zk9UejE0MEs4VzlHNmxFaGFiWHpuRjFCcHhlZ0VPMEpsRmtmN0V0NGZFVXpNMmtRRzBqSWU3eXRPQ3c5aVdLY2JveDA4dEJ6TnFUSTAwMmRkby95YXdETmZsZUQrN3k4UEVwT0l2L0FqVmVsZkFvUlBnMDNTST0="

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

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
            json={"token": PLUSPLUS_TOKEN, "title": title, "content": content, "template": "txt"},
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
        code_value = result.get("code") if isinstance(result, dict) else None
        if data.get("code") != 0 or not code_value:
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
        else:
            if not code_value or str(code_value).lower() in ("null", "none"):
                print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            else:
                print("✅ [授权] code 获取成功")
                return str(code_value)
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")

    if CODE_FALLBACK_ENABLED and HAR_FALLBACK_CODE:
        print("🔁 [授权] 8088 暂无新 code，使用抓包 code 兜底继续测试")
        return HAR_FALLBACK_CODE
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


def get_cached_token(server: str) -> str | None:
    cache = load_token_cache()
    data = cache.get(server)
    if data and isinstance(data, dict) and data.get("token"):
        print(f"✅ [缓存] 使用 {server} token")
        return data["token"]
    return None


def set_cached_token(server: str, token: str) -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def remove_cached_token(server: str) -> None:
    cache = load_token_cache()
    if server in cache:
        del cache[server]
        save_token_cache(cache)

BASE_URL = 'https://gszy.baijqr.cn'

AI_PERSONALITY_CONTENT = (
    "题1：春日闲暇时光，你更偏爱哪种场景？，答案：A. 书房品茗，静享独处；"
    "题2：你更欣赏哪种艺术形式？，答案：A. 书法绘画，水墨丹青；"
    "题3：你更欣赏哪种历史人物？，答案：A. 文人墨客，诗词传世；"
    "题4：你更偏爱哪种季节的氛围？，答案：A. 春暖花开，生机盎然；"
    "题5：面对新事物，你的态度是？，答案：A. 偏爱经典，坚守本心；"
    "题6：你更愿意为哪种事情投入时间？，答案：A. 学习一门手艺；"
    "题7：你更愿意如何度过周末午后？，答案：A. 煮茶听琴，静享时光；"
    "题8：如果有一天完全自由，你会？，答案：A. 读书习字，修身养性；"
    "题9：你更倾向于哪种运动方式？，答案：A. 太极瑜伽，修身养性；"
    "题10：你更愿意尝试哪种新体验？，答案：A. 学习传统技艺；"
    "题11：你更偏爱哪种色调的家居？，答案：A. 素雅原木，自然温润；"
    "题12：你最喜欢的室内装饰风格？，答案：A. 中式雅致，书画琴韵；"
    "题13：闲暇时你更愿与谁共度？，答案：A. 独处，与自己对话；"
    "题14：你更愿意在哪种环境中工作？，答案：A. 安静独立，专注高效；"
    "题15：面对压力时，你更倾向？，答案：A. 静坐冥想，内省自修"
)

ACTIVITIES = [
    {"id": "509", "name": "AI时光剧场", "source": "20260721", "mode": "story_theater",
     "image": "https://gszy-oss.baijqr.cn/image/orn/company/picture/20260825221536383-11.jpeg", "content": "兰州，你好"},
    {"id": "502", "name": "AI烟韵人格测试", "source": "20260417", "mode": "ai_personality",
     "image": "https://gszy-oss.baijqr.cn/image/orn/company/picture/20260825222450529-18.jpeg", "content": AI_PERSONALITY_CONTENT},
    {"id": "506", "name": "敦煌藏雅韵", "source": "20260622", "mode": "dunhuang",
     "image": "https://gszy-oss.baijqr.cn/image/orn/company/picture/20260825221724669-54.jpeg", "content": "你好你好"},
    {"id": "498", "name": "春日绘梦", "source": "20260309", "mode": "spring_draw",
     "image": "https://gszy-oss.baijqr.cn/image/orn/company/picture/20260825222617417-11.jpeg", "input": "你好小猫", "style": "动漫"},
    {"id": "475", "name": "悠悠好礼燃力全开", "source": "20250523", "mode": "parkour"},
    {"id": "471", "name": "潮黑礼遇拼图", "source": "20221019", "mode": "puzzle"},
    {"id": "505", "name": "悠享丝路AR驿站", "source": "20260608", "mode": "ar_collect"},
    {"id": "508", "name": "口令寻礼兰州图鉴", "source": "20260714", "mode": "lanzhou_collect"},
]
if os.getenv("LZ_ACTIVITIES"):
    _ids = {x.strip() for x in os.getenv("LZ_ACTIVITIES").split(",") if x.strip()}
    ACTIVITIES = [a for a in ACTIVITIES if a["id"] in _ids]
LNG = 108.37379996134864
LAT = 22.81894986486499
OPENID_URL = f'{BASE_URL}/api/cloud2.member.api/wx/getOpenId'
DECODE_URL = f'{BASE_URL}/api/cloud2.member.api/member/userInfo/decode'
CHECK_URL = f'{BASE_URL}/api/cloud2.member.api/member/userInfo/getCurLoginUser'
SIGN_URL = f'{BASE_URL}/api/cloud2.member.api/userSignInPointMall/signIn'
RECORD_URL = f'{BASE_URL}/api/cloud2.member.api/userSignInPointMall/getSignInRecord'
# ACTIVITY CONSTANTS

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541c37) XWEB/25364"
)

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lzft.js_token_cache.json")

TOKEN_HEADER = 'x-auth-jwt'
SIGN_METHOD = 'GET'

def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "isOpenSecret": "1",
        "Referer": f"https://servicewechat.com/{APPID}/88/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers[TOKEN_HEADER] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    def search(obj: Any) -> str | None:
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except Exception:
                return obj if obj and obj != "null" else None
        if not isinstance(obj, dict):
            return None
        for key in ("userToken", "token", "accessToken", "access_token", "sessionId", "session_id"):
            value = obj.get(key)
            if value and str(value).lower() not in ("null", "none"):
                return str(value)
        nested = obj.get("data")
        if nested is not None:
            return search(nested)
        nested = obj.get("result")
        if nested is not None:
            return search(nested)
        return None

    return search(data)


def api_request(
    method: str,
    url: str,
    token: str = "",
    params: Dict[str, Any] | None = None,
    body: Dict[str, Any] | None = None,
    proxies: Dict[str, str] | None = None,
    server: str = "",
) -> Dict[str, Any]:
    try:
        kwargs: Dict[str, Any] = {"headers": common_headers(token)}
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


def bind_openid(server: str, code: str, auth_token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    params = {"code": code, "t": int(time.time() * 1000)}
    data = api_request("GET", OPENID_URL, token=auth_token, params=params, proxies=proxies, server=server)
    if is_success(data):
        openid = ((data.get("data") or {}).get("openid") or "")
        print(f"✅ [登录] code 绑定 openid 成功: {mask(openid)}")
    else:
        print(f"⚠️ [登录] getOpenId 未成功: {pick_msg(data)}")
    return data


def decode_user_token(server: str, code: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    headers = common_headers()
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["User-Agent"] = USER_AGENT + " miniProgram/wxda7924fb8811699c"
    headers["Origin"] = BASE_URL
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Referer"] = f"{BASE_URL}/gansu/gansu-shop/sign.html?&isUser=true&xcxData={XCX_DATA}&c={code}"
    try:
        response = request_with_proxy(
            "POST",
            DECODE_URL,
            headers=headers,
            data={"context": XCX_DATA},
            proxies=proxies,
            server=server,
        )
        return response.json()
    except Exception as exc:
        return {"code": -1, "msg": f"请求异常: {exc}"}


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None, auth_token: str = "") -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 绑定 openid")
        bind_openid(server, code, auth_token, proxies)
        print("🔐 [登录] 使用抓包 xcxData 换取 userToken")
        data = decode_user_token(server, code, proxies)
        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data
        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def is_success(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("success", "Success", "ok", "status"):
        if data.get(key) is True:
            return True
    for key in ("code", "status", "ret", "errcode", "error"):
        value = data.get(key)
        if value in (0, 200, "0", "200", "0000", 1, "1", 10000, 100, "100"):
            return True
    return False


def pick_msg(data: Any) -> str:
    if not isinstance(data, dict):
        return json_preview(data, 200)
    for key in ("msg", "message", "Message", "errMsg", "retInfo", "desc", "info"):
        if data.get(key):
            return str(data[key])
    return json_preview(data, 200)


def is_already_done(data: Any) -> bool:
    if isinstance(data, dict) and data.get("code") in (203, "203"):
        return True
    text = pick_msg(data)
    return any(k in text for k in ("已签", "已经签", "签到过", "重复", "已完成", "今日已", "一天只能签到一次", "只能签到一次", "already"))


def is_auth_error(data: Any) -> bool:
    text = pick_msg(data)
    return any(k in text for k in ("token", "登录", "未授权", "未登录", "失效", "过期", "401", "403"))


def do_sign(token: str, code: str, server: str, proxies: Dict[str, str] | None) -> str:
    data = api_request(
        "GET",
        SIGN_URL,
        token=token,
        params={"code": code, "userToken": token},
        proxies=proxies,
        server=server,
    )
    if is_success(data):
        msg = "签到成功"
        print(f"✅ [签到] {msg}")
    elif is_already_done(data):
        msg = pick_msg(data) or "今日已签到"
        print(f"✅ [签到] {msg}")
    else:
        msg = f"签到失败: {pick_msg(data)}"
        print(f"⚠️ [签到] {msg}")

    record = api_request(
        "GET",
        RECORD_URL,
        token=token,
        params={"time": int(time.time() * 1000), "userToken": token},
        proxies=proxies,
        server=server,
    )
    if is_success(record):
        record_data = record.get("data") or {}
        days = record_data.get("keepSignInDays")
        if days is not None:
            msg = f"{msg}，连续签到 {days} 天"
            print(f"📅 [签到] 连续签到 {days} 天")
    return msg


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None, str]:
    cached_token = get_cached_token(server)
    if cached_token:
        check = api_request(
            "GET",
            CHECK_URL,
            token=cached_token,
            params={"userToken": cached_token, "t": int(time.time() * 1000)},
            proxies=proxies,
            server=server,
        )
        if is_success(check):
            print("✅ [缓存] token 有效")
            code = get_code(server)
            if code:
                bind_openid(server, code, cached_token, proxies)
            return cached_token, None, code or ""
        print("⚠️ [缓存] token 已失效，重新登录")
        remove_cached_token(server)

    code = get_code(server)
    if not code:
        print("⚠️ [授权] code 无效，等待后重试")
        sleep(2)
        code = get_code(server)
    if not code:
        return None, None, ""

    token, raw_login = login_by_code(server, code, proxies, auth_token=cached_token or "")
    if not token and code != HAR_FALLBACK_CODE and CODE_FALLBACK_ENABLED and HAR_FALLBACK_CODE:
        print("🔁 [登录] 当前 code 换 token 失败，尝试抓包 fallback code")
        code = HAR_FALLBACK_CODE
        token, raw_login = login_by_code(server, code, proxies, auth_token=cached_token or "")
    if token:
        set_cached_token(server, token)
    return token, raw_login, code

def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "activityMsg": "-",
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

    token, raw_login, used_code = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        result["signMsg"] = do_sign(token, used_code, server, proxies)
        if any(k in result["signMsg"] for k in ("token", "登录", "未授权", "未登录", "失效", "过期", "401", "403")):
            print("🔁 [刷新] token 失效，重新获取 code 后重试")
            remove_cached_token(server)
            token, raw_login, used_code = login_with_cache(server, proxies)
            if not token:
                result["error"] = f"登录失败: {json_preview(raw_login)}"
                return result
            result["token"] = mask(token)
            result["signMsg"] = do_sign(token, used_code, server, proxies)
        result["activityMsg"] = run_activities(server, token, proxies)
        result["success"] = True
        return result
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def run_one_activity(server: str, token: str, act: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
    print(f"🎯 [活动] {act['name']} ({act['id']})")
    detail = try_share_free(server, token, act, proxies)
    if not _is_ok(detail):
        return f"查询失败: {pick_msg(detail)}"
    d = _get_data(detail) or {}
    chances = free_chances(d)
    if chances <= 0:
        return "无分享免费机会，跳过（不兑换币）"
    mode = act["mode"]
    if mode in ("story_theater", "ai_personality", "dunhuang", "spring_draw"):
        return complete_ai_game(server, token, act, proxies)
    if mode in ("parkour", "puzzle"):
        return complete_simple_game(server, token, act, proxies)
    if mode == "ar_collect":
        return run_ar_collect(server, token, act, proxies, chances)
    if mode == "lanzhou_collect":
        return run_lanzhou_collect(server, token, act, proxies, chances)
    return f"未知活动模式: {mode}"

def run_activities(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    lines: List[str] = []
    for index, act in enumerate(ACTIVITIES, 1):
        try:
            msg = run_one_activity(server, token, act, proxies)
        except Exception as exc:
            msg = f"执行异常: {exc}"
        print(f"✅ [活动] {act['name']}: {msg}")
        lines.append(f"{act['name']}: {msg}")
        if index < len(ACTIVITIES):
            sleep(1)
    return "；".join(lines)
def run_lanzhou_collect(server: str, token: str, act: Dict[str, Any], proxies: Dict[str, str] | None, chances: int) -> str:
    messages: List[str] = []
    for _ in range(min(chances, 2)):
        q = _activity_request(
            "POST", "/api/cloud2.activity.api/market/postStation/lanZhouQuery", token,
            params={"userToken": token, "actId": act["id"]}, proxies=proxies, server=server,
        )
        if not _is_ok(q):
            break
        qd = _get_data(q) or {}
        cards = qd.get("lanzhou") or []
        missing = [x for x in cards if not x.get("has")]
        if not missing:
            break
        post_id = missing[0]["id"]
        save = _activity_request(
            "POST", "/api/cloud2.activity.api/market/postStation/lanZhouSave", token,
            params={"userToken": token, "actId": act["id"], "postId": post_id}, proxies=proxies, server=server,
        )
        if _is_ok(save):
            messages.append(f"翻牌获得图鉴 {post_id}")
        else:
            messages.append(f"翻牌 {post_id} 失败: {pick_msg(save)}")
            break
    q = _activity_request(
        "POST", "/api/cloud2.activity.api/market/postStation/lanZhouQuery", token,
        params={"userToken": token, "actId": act["id"]}, proxies=proxies, server=server,
    )
    qd = _get_data(q) or {}
    cards = qd.get("lanzhou") or []
    owned = sum(1 for x in cards if x.get("has"))
    if owned >= len(cards) and cards:
        synth = _activity_request(
            "POST", "/api/cloud2.activity.api/market/postStation/lanZhouSynthesis", token,
            params={"userToken": token, "actId": act["id"]}, proxies=proxies, server=server,
        )
        lz = extract_lz(synth)
        if lz:
            open_data = open_activity_game(server, token, act, extra={"source": act["source"], "lz": lz}, proxies=proxies)
            od = _get_data(open_data) or {}
            end = end_activity_game(server, token, act, od.get("gameId"), od.get("maxScoreLimit") or 800, proxies=proxies)
            return "；".join(messages + [prize_text(end)])
        return "；".join(messages + [f"合成结果: {json_preview(synth, 200)}"])
    return "；".join(messages) + f"，图鉴进度 {owned}/{len(cards)}"
def run_ar_collect(server: str, token: str, act: Dict[str, Any], proxies: Dict[str, str] | None, chances: int) -> str:
    _activity_request(
        "POST", "/api/cloud2.activity.api/market/postStation/redisVideo", token,
        params={"userToken": token, "actId": act["id"], "type": "0", "init": "true"}, proxies=proxies, server=server,
    )
    messages: List[str] = []
    for _ in range(min(chances, 5)):
        q = _activity_request(
            "POST", "/api/cloud2.activity.api/market/postStation/query", token,
            params={"userToken": token, "actId": act["id"]}, proxies=proxies, server=server,
        )
        if not _is_ok(q):
            break
        qd = _get_data(q) or {}
        juan = qd.get("juanArray") or []
        missing = [x for x in juan if not x.get("has")]
        if not missing:
            break
        post_id = missing[0]["id"]
        save = _activity_request(
            "POST", "/api/cloud2.activity.api/market/postStation/save", token,
            params={"userToken": token, "actId": act["id"], "postId": post_id, "type": "1"}, proxies=proxies, server=server,
        )
        if _is_ok(save):
            messages.append(f"收集券 {post_id}")
        else:
            messages.append(f"收集券 {post_id} 失败: {pick_msg(save)}")
            break
    q = _activity_request(
        "POST", "/api/cloud2.activity.api/market/postStation/query", token,
        params={"userToken": token, "actId": act["id"]}, proxies=proxies, server=server,
    )
    qd = _get_data(q) or {}
    medals = qd.get("xzArray") or []
    target = next((x for x in medals if x.get("canSynthesize")), None)
    if target:
        synth = _activity_request(
            "POST", "/api/cloud2.activity.api/market/postStation/synthesis", token,
            params={"userToken": token, "actId": act["id"], "postId": target["id"]}, proxies=proxies, server=server,
        )
        open_data = open_activity_game(server, token, act, extra={"special": target["id"]}, proxies=proxies)
        od = _get_data(open_data) or {}
        end = end_activity_game(server, token, act, od.get("gameId"), od.get("maxScoreLimit") or 800, proxies=proxies)
        return "；".join(messages + [prize_text(end)])
    return "；".join(messages) + ("，未满足勋章合成条件" if messages else "无免费券机会")
def complete_simple_game(server: str, token: str, act: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
    open_data = open_activity_game(server, token, act, proxies=proxies)
    if not _is_ok(open_data):
        return f"开局失败: {pick_msg(open_data)}"
    od = _get_data(open_data) or {}
    extra = {"challengeResult": 0} if act["mode"] == "parkour" else None
    end = end_activity_game(server, token, act, od.get("gameId"), od.get("maxScoreLimit") or od.get("pointConfig") or 800, extra=extra, proxies=proxies)
    return prize_text(end)

def extract_lz(data: Dict[str, Any]) -> str:
    d = _get_data(data)
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        for key in ("lz", "data", "result"):
            value = d.get(key)
            if value:
                return str(value)
    return ""
def complete_ai_game(server: str, token: str, act: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
    open_data = open_activity_game(server, token, act, proxies=proxies)
    if not _is_ok(open_data):
        return f"开局失败: {pick_msg(open_data)}"
    od = _get_data(open_data) or {}
    mode = act["mode"]
    if mode == "story_theater":
        print(f"🎬 [活动] {act['name']} 生成剧情...")
        _activity_request(
            "POST", "/api/cloud2.activity.api/market/treeHole/theaterReply", token,
            data={"userToken": token, "actId": act["id"], "contentValue": act["content"]}, proxies=proxies, server=server,
        )
    elif mode == "ai_personality":
        print(f"🎨 [活动] {act['name']} 生成人格...")
        _activity_request(
            "POST", "/cloud2.activity.api/market/treeHole/flowerReply", token,
            params={"userToken": token, "contentValue": act["content"]}, proxies=proxies, server=server,
        )
    elif mode == "dunhuang":
        print(f"🪔 [活动] {act['name']} 生成敦煌寄语...")
        _activity_request(
            "POST", "/api/cloud2.activity.api/market/treeHole/dunHuangReply", token,
            params={"userToken": token, "contentValue": act["content"]}, proxies=proxies, server=server,
        )
    elif mode == "spring_draw":
        print(f"🌸 [活动] {act['name']} 生成春日画卷...")
        reply = _activity_request(
            "POST", "/cloud2.activity.api/market/treeHole/springReply", token,
            params={"input": act["input"], "type": act["style"], "userToken": token}, proxies=proxies, server=server,
        )
        if _is_ok(reply):
            act["image"] = (_get_data(reply) or {}).get("url") or act.get("image", "")
    save_tree_hole(server, token, act, proxies)
    end = end_activity_game(server, token, act, od.get("gameId"), od.get("maxScoreLimit") or od.get("pointConfig") or 800, proxies=proxies)
    return prize_text(end)
def _is_ok(data: Any) -> bool:
    return isinstance(data, dict) and data.get("code") in (200, "200")

def _get_data(data: Any) -> Any:
    return (data or {}).get("data") if isinstance(data, dict) else None

def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

def _activity_request(
    method: str, path: str, token: str,
    params: Dict[str, Any] | None = None, data: Dict[str, Any] | None = None,
    proxies: Dict[str, str] | None = None, server: str = "",
) -> Dict[str, Any]:
    headers = common_headers(token)
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    kwargs: Dict[str, Any] = {"headers": headers}
    if params:
        kwargs["params"] = params
    if data is not None:
        kwargs["data"] = data
    try:
        response = request_with_proxy(method.upper(), BASE_URL + path, proxies=proxies, server=server, **kwargs)
        return response.json()
    except Exception as exc:
        return {"code": -1, "msg": f"请求异常: {exc}"}

def query_activity_detail(server: str, token: str, activity_id: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    return _activity_request(
        "GET", "/cloud2.activity.api/common/queryActivity/queryActivityUserInfoDetail", token,
        params={"activityId": activity_id, "userToken": token}, proxies=proxies, server=server,
    )

def free_chances(detail_data: Dict[str, Any]) -> int:
    return max(0, _to_int(detail_data.get("surplusFreeChance"))) + max(0, _to_int(detail_data.get("remainChanceTimes")))

def try_share_free(server: str, token: str, act: Dict[str, Any], proxies: Dict[str, str] | None) -> Dict[str, Any]:
    detail = query_activity_detail(server, token, act["id"], proxies)
    if not _is_ok(detail):
        return detail
    d = _get_data(detail) or {}
    share_limit = _to_int(d.get("share"))
    share_used = _to_int(d.get("useShare"))
    if share_limit > 0 and share_used == 0:
        info = _activity_request(
            "GET", "/cloud2.activity.api/common/activity/getShareInfo", token,
            params={"activityId": act["id"], "source": act["source"], "userToken": token}, proxies=proxies, server=server,
        )
        share = _activity_request(
            "GET", "/cloud2.activity.api/common/activity/share", token,
            params={"activityId": act["id"], "source": act["source"], "userToken": token}, proxies=proxies, server=server,
        )
        print(f"✅ [活动] {act['name']} 分享完成: {pick_msg(share)}")
        detail = query_activity_detail(server, token, act["id"], proxies)
    return detail

def open_activity_game(server: str, token: str, act: Dict[str, Any], extra: Dict[str, Any] | None = None, proxies: Dict[str, str] | None = None) -> Dict[str, Any]:
    body = {"activityId": act["id"], "userToken": token}
    if act.get("mode") in ("story_theater", "dunhuang"):
        body["source"] = act["source"]
    if extra:
        body.update(extra)
    return _activity_request("POST", "/cloud2.activity.api/common/activity/openGame", token, data=body, proxies=proxies, server=server)

def end_activity_game(server: str, token: str, act: Dict[str, Any], game_id: Any, max_score: Any, extra: Dict[str, Any] | None = None, proxies: Dict[str, str] | None = None) -> Dict[str, Any]:
    body = {
        "userToken": token, "activityId": act["id"], "source": act["source"],
        "gameId": game_id, "point": max_score or 800, "lng": LNG, "lat": LAT, "chanceSource": "free",
    }
    if extra:
        body.update(extra)
    return _activity_request("POST", "/cloud2.activity.api/common/activity/endGame", token, data=body, proxies=proxies, server=server)

def prize_text(data: Dict[str, Any]) -> str:
    if _is_ok(data):
        d = _get_data(data)
        if isinstance(d, dict):
            win = _to_int(d.get("win")) == 1 and _to_int(d.get("prizeType")) != 4000
            name = d.get("prizeName") or d.get("awardsName") or d.get("prizeValue") or "谢谢参与"
            return f"抽中 {name}" if win else f"未中奖: {name}"
        return f"结束返回: {json_preview(data, 200)}"
    return f"结束失败: {pick_msg(data)}"

def save_tree_hole(server: str, token: str, act: Dict[str, Any], proxies: Dict[str, str] | None) -> Dict[str, Any]:
    return _activity_request(
        "POST", "/cloud2.activity.api/market/treeHole/save", token,
        data={"actId": act["id"], "userToken": token, "sentence": act.get("image", "")}, proxies=proxies, server=server,
    )


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""✈️ lz飞天 登录/签到任务结果

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
🎮 活动：{res["activityMsg"]}
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
                "signMsg": "-",
                "activityMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ 🏁 lz飞天 登录/签到任务执行完成                       ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus(f"✈️ lz飞天 登录/签到任务完成", build_notify(results))


if __name__ == "__main__":
    main()
