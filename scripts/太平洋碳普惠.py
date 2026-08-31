#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用积分接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
太平洋财产保险碳普惠动态 code 版

功能：
  1. 通过 YYB Go 按账号获取微信 code
  2. /api/auth/register/userLogin 使用 code 换 token（SM2 加密通信）
  3. 每日签到（saveSignLog）
  4. 签到日历/签到状态查询（querySignLog / signFlag）
  5. 关注公众号积分（queryUserIsAttention / queryAttentionIntegral）
  6. 点赞排行榜积分（topDistributePoints）
  7. 看视频得积分（视频系列 → 视频列表 → 上报观看）
  8. 生日积分（getUserBirthDay / saveUserBirthDayIntegral）
  9. 抽奖次数查询（queryDrawNum）
  10. 积分汇总查询（待领取/即将失效积分）
  11. PushPlus 推送
  12. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER        YYB Go 服务地址，格式：地址@微信账号标识，多账号换行分隔
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests gmssl
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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from gmssl.sm2 import CryptSM2
except ImportError:
    CryptSM2 = None


APP_NAME = "太平洋财产保险碳普惠小程序"
APPID = "wxc62da17526f8b4d0"

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

BASE_URL = "https://cfp.cpic.com.cn"
LOGIN_URL = f"{BASE_URL}/api/auth/register/userLogin"
TRACE_URL = f"{BASE_URL}/api/http/trace/getHttpTraceId"

SIGN_SAVE_URL = f"{BASE_URL}/api/saveSignLog"
SIGN_LOG_URL = f"{BASE_URL}/api/querySignLog"
SIGN_FLAG_URL = f"{BASE_URL}/api/signFlag"
ATTENTION_IS_URL = f"{BASE_URL}/api/auth/register/queryUserIsAttention"
ATTENTION_INTEGRAL_URL = f"{BASE_URL}/api/auth/register/queryAttentionIntegral"
TOP_POINTS_URL = f"{BASE_URL}/api/topDistributePoints"
VIDEO_SERIES_URL = f"{BASE_URL}/api/getVideoSeriesDataNew"
VIDEO_LIST_URL = f"{BASE_URL}/api/getVideoDataNew"
VIDEO_SAVE_URL = f"{BASE_URL}/api/saveVideoIntegralNew"
BIRTHDAY_URL = f"{BASE_URL}/api/getUserBirthDay"
BIRTHDAY_SAVE_URL = f"{BASE_URL}/api/saveUserBirthDayIntegral"
DRAW_NUM_URL = f"{BASE_URL}/api/active/prize/queryDrawNum"
PENDING_INTEGRAL_URL = f"{BASE_URL}/api/getTphPendingIntegralDetail"
COLLECT_INTEGRAL_URL = f"{BASE_URL}/api/updateTphIntegral"
FAIL_INTEGRAL_URL = f"{BASE_URL}/api/getAboutFailIntegralDetail"
TASK_LIST_URL = f"{BASE_URL}/api/carbon/scenario/search"

ACTIVE_CODE = "16298e9663dc4a8b"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tpcphcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat"
)
REFERER = f"https://servicewechat.com/{APPID}/156/page-frame.html"


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


def is_hex(text: Any) -> bool:
    if not isinstance(text, str) or len(text) < 192 or len(text) % 2:
        return False
    try:
        int(text[:32], 16)
        int(text[-32:], 16)
    except ValueError:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in text[:64])


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🌱 太平洋碳普惠动态 code 版                    ║")
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


def parse_proxy_response(text: Any) -> Optional[Dict[str, Any]]:
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


def build_proxy_dict(proxy_info: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
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


def validate_proxy(proxies: Optional[Dict[str, str]]) -> Tuple[bool, str]:
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


def get_valid_proxy(account_name: str) -> Tuple[Optional[Dict[str, str]], str]:
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
    proxies: Optional[Dict[str, str]] = None,
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


# ====================== SM2 国密加解密 ======================
def check_gmssl() -> bool:
    if CryptSM2 is None:
        print("❌ [依赖] 缺少 gmssl，请先执行: pip install gmssl")
        return False
    return True


def sm2_encrypt(public_key: str, plain_text: str) -> str:
    """SM2 加密（C1C3C2，无 04 前缀），与小程序 miniprogram-sm-crypto 一致。"""
    public_key = public_key.strip()
    if public_key.startswith("04") and len(public_key) == 130:
        public_key = public_key[2:]
    crypt = CryptSM2(private_key="", public_key=public_key, mode=1)
    return crypt.encrypt(plain_text.encode("utf-8")).hex()


def sm2_decrypt(private_key: str, cipher_hex: str) -> Optional[str]:
    """SM2 解密（C1C3C2）。失败返回 None。"""
    try:
        private_key = private_key.strip()
        if len(private_key) > 64:
            private_key = private_key[-64:]
        crypt = CryptSM2(private_key=private_key, public_key="", mode=1)
        plain = crypt.decrypt(bytes.fromhex(cipher_hex))
        if plain is None:
            return None
        return plain.decode("utf-8")
    except Exception:
        return None


def gen_local_token() -> str:
    """客户端会话 token：32 位 hex + 6 位随机小写字母（与小程序一致）。"""
    import uuid
    import string

    return uuid.uuid4().hex + "".join(random.choices(string.ascii_lowercase, k=6))


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


def get_code(entry: str) -> Optional[str]:
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
        code = result.get("code") if isinstance(result, dict) else None
        if not code and isinstance(data.get("result"), dict):
            code = data["result"].get("code")
        if not code and isinstance(data.get("code"), str):
            code = data.get("code")
        if data.get("code") not in (None, 0, "0") and not code:
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None
        if not code:
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None
        print("✅ [授权] code 获取成功")
        return str(code)
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def common_headers(token: str = "", openid: str = "", trace_id: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "xweb_xhr": "1",
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "*/*",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": REFERER,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if trace_id:
        headers["httpTraceId"] = trace_id
    if openid:
        headers["openid"] = openid
    if token:
        headers["token"] = token
    return headers


class Account:
    """单个账号的会话信息（来自 userLogin 解密结果）。"""

    def __init__(self, server: str):
        self.server = server
        self.token = ""
        self.openid = ""
        self.unionid = ""
        self.user_code = ""
        self.branch_code = ""
        self.public_key = ""
        self.private_key = ""
        self.proxies: Optional[Dict[str, str]] = None

    # ---------- 基础请求 ----------
    def get_trace_id(self) -> str:
        try:
            response = request_with_proxy(
                "POST",
                TRACE_URL,
                headers=common_headers(self.token, self.openid),
                json={"source": "tph"},
                proxies=self.proxies,
                server=self.server,
            )
            data = response.json()
            return (data.get("data") or {}).get("httpTraceId", "")
        except Exception:
            return ""

    def api_post(self, url: str, payload: Dict[str, Any], need_encrypt: bool = True) -> Dict[str, Any]:
        """发送业务请求：payload 经 SM2 加密放入 param，响应 data 若为密文则解密。"""
        trace_id = self.get_trace_id()
        headers = common_headers(self.token, self.openid, trace_id)

        if need_encrypt:
            if not self.public_key:
                return {"code": -1, "msg": "缺少加密公钥"}
            body = {"param": sm2_encrypt(self.public_key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))}
        else:
            body = payload

        response = request_with_proxy(
            "POST",
            url,
            headers=headers,
            json=body,
            proxies=self.proxies,
            server=self.server,
        )
        try:
            result = response.json()
        except Exception:
            return {"code": -1, "msg": f"JSON解析失败: {response.text[:300]}"}

        data = result.get("data")
        if isinstance(data, str) and is_hex(data) and self.private_key:
            plain = sm2_decrypt(self.private_key, data)
            if plain is not None:
                try:
                    result["data"] = json.loads(plain)
                except Exception:
                    result["data"] = plain
        return result


def login_by_code(server: str, code: str, proxies: Optional[Dict[str, str]]) -> Tuple[Optional[Account], Dict[str, Any]]:
    account = Account(server)
    account.proxies = proxies

    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(gen_local_token(), ""),
            json={
                "wechatCode": code,
                "sourceType": "share_point",
                "shareType": "point",
                "shareUserUserCode": "",
                "shareUserUnionId": "",
            },
            proxies=proxies,
            server=server,
        )

        try:
            result = response.json()
        except Exception:
            result = {"raw": response.text[:800]}

        if result.get("code") != 200:
            print(f"❌ [登录] 登录失败: {json_preview(result)}")
            return None, result

        data = result.get("data") or {}
        account.public_key = data.get("appFirstMake", "")
        account.private_key = data.get("rearEndUse", "")

        if not account.private_key or not isinstance(data.get("response"), str):
            print(f"❌ [登录] 未返回密钥或加密响应: {json_preview(result)}")
            return None, result

        plain = sm2_decrypt(account.private_key, data["response"])
        if plain is None:
            print("❌ [登录] 响应解密失败")
            return None, result

        try:
            info = json.loads(plain)
        except Exception:
            print(f"❌ [登录] 解密结果非 JSON: {plain[:200]}")
            return None, result

        account.token = info.get("token", "")
        account.openid = info.get("openid", "")
        account.unionid = info.get("unionid", "")
        account.user_code = info.get("userCode", "")
        account.branch_code = (info.get("thcUserInfo") or {}).get("branchCode", "")

        if not account.token:
            print(f"❌ [登录] 未识别 token 字段: {json_preview(info)}")
            return None, result

        print(f"✅ [登录] token 获取成功: {mask(account.token)}")
        return account, result
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, {}


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


def get_cached_token(server: str) -> Optional[Dict[str, Any]]:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} token")
                return data
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, account: Account, expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {
        "token": account.token,
        "openid": account.openid,
        "unionid": account.unionid,
        "userCode": account.user_code,
        "branchCode": account.branch_code,
        "appFirstMake": account.public_key,
        "rearEndUse": account.private_key,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def account_from_cache(server: str, cached: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> Account:
    account = Account(server)
    account.proxies = proxies
    account.token = cached.get("token", "")
    account.openid = cached.get("openid", "")
    account.unionid = cached.get("unionid", "")
    account.user_code = cached.get("userCode", "")
    account.branch_code = cached.get("branchCode", "")
    account.public_key = cached.get("appFirstMake", "")
    account.private_key = cached.get("rearEndUse", "")
    return account


def login_with_cache(server: str, proxies: Optional[Dict[str, str]]) -> Tuple[Optional[Account], Dict[str, Any]]:
    """优先使用缓存 token（积分接口验证），失效自动 code 刷新"""
    cached = get_cached_token(server)
    if cached:
        print("🔍 [缓存] 验证 token")
        try:
            account = account_from_cache(server, cached, proxies)
            check_resp = account.api_post(PENDING_INTEGRAL_URL, {
                "userCode": account.user_code,
                "unionId": account.unionid,
            })
            if check_resp.get("code") == 200:
                print("✅ [缓存] token 有效")
                return account, {}
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, {}

    account, raw_login = login_by_code(server, code, proxies)
    if not account:
        return None, raw_login

    expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    set_cached_token(server, account, expire_time)
    return account, raw_login


# ====================== 任务 ======================
def task_sign(account: Account) -> str:
    """每日签到"""
    resp = account.api_post(SIGN_SAVE_URL, {
        "unionId": account.unionid,
        "userCode": account.user_code,
    })
    code = resp.get("code")
    msg = resp.get("msg") or ""
    if code == 200:
        print(f"✅ [签到] 签到成功: {msg or '获得积分'}")
        return f"签到成功 {msg}".strip()
    if "重复签到" in msg or "已签" in msg:
        print(f"⚠️ [签到] {msg}")
        return msg
    print(f"⚠️ [签到] 签到失败: {msg or json_preview(resp, 200)}")
    return msg or "签到失败"


def task_sign_status(account: Account) -> str:
    """签到状态/日历查询"""
    try:
        resp = account.api_post(SIGN_LOG_URL, {
            "userCode": account.user_code,
            "unionId": account.unionid,
            "branchCode": account.branch_code,
        })
        if resp.get("code") != 200:
            return "签到日历查询失败"
        data = resp.get("data")
        if isinstance(data, list):
            signed = sum(
                1 for item in data
                if isinstance(item, dict) and str(item.get("signFlag", "")) in ("1", "true", "True")
            )
            today = next((item for item in data if isinstance(item, dict) and item.get("date") == "今天"), None)
            today_text = f"今日可得 {today.get('integral')} 积分" if today else "日历获取成功"
            return f"{today_text}，已签到 {signed} 天"
        if isinstance(data, dict):
            return f"签到记录 {len(data)} 项"
        return "签到日历获取成功"
    except Exception as exc:
        print(f"⚠️ [签到] 日历查询异常: {exc}")
        return "签到日历查询异常"


def task_attention(account: Account) -> str:
    """关注公众号积分"""
    try:
        is_resp = account.api_post(ATTENTION_IS_URL, {
            "unionId": account.unionid,
            "userCode": account.user_code,
        })
        integral_resp = account.api_post(ATTENTION_INTEGRAL_URL, {
            "unionId": account.unionid,
            "userCode": account.user_code,
        })
        is_attention = is_resp.get("data") if is_resp.get("code") == 200 else None
        integral = integral_resp.get("data") if integral_resp.get("code") == 200 else None
        print(f"📮 [关注] 关注状态: {is_attention}，可得积分: {integral}")
        return f"关注状态 {is_attention} / 积分 {integral}"
    except Exception as exc:
        print(f"⚠️ [关注] 异常: {exc}")
        return "关注任务异常"


def task_top_points(account: Account) -> str:
    """点赞排行榜领积分：先查状态，再领取"""
    time_type = current_time_type()
    texts = []
    for save_flag in (False, True):
        resp = account.api_post(TOP_POINTS_URL, {
            "userCode": account.user_code,
            "unionId": account.unionid,
            "savePoinsFlag": save_flag,
            "sendTimeType": time_type,
        })
        code = resp.get("code")
        msg = resp.get("msg") or ""
        stage = "查询" if not save_flag else "领取"
        if code == 200:
            print(f"👍 [点赞] {stage}: {msg} data={resp.get('data')}")
            texts.append(f"{msg}".strip())
        else:
            print(f"⚠️ [点赞] {stage}失败: {msg or json_preview(resp, 200)}")
            texts.append(msg or "失败")
        sleep(1)
    return " / ".join(texts) if texts else "点赞任务失败"


def current_time_type() -> int:
    """点赞积分时段凌晨/上午/下午/晚上。"""
    hour = datetime.now().hour
    if hour < 6:
        return 1
    if hour < 12:
        return 2
    if hour < 18:
        return 3
    return 4


def task_video(account: Account) -> str:
    """看视频得积分：视频列表 → 逐个上报观看时长"""
    try:
        series_resp = account.api_post(VIDEO_SERIES_URL, {"branchCode": account.branch_code}, need_encrypt=False)
        series_list = series_resp.get("data") if series_resp.get("code") == 200 else None
        if isinstance(series_list, dict):
            series_list = series_list.get("list") or series_list.get("seriesList") or []
        if not isinstance(series_list, list) or not series_list:
            print(f"⚠️ [视频] 系列获取失败: {series_resp.get('msg') or '无系列'}")
            return "视频系列获取失败"

        watched = 0
        for series in series_list[:4]:
            if not isinstance(series, dict):
                continue
            series_name = series.get("seriesName") or series.get("name") or ""
            if not series_name:
                continue
            list_resp = account.api_post(VIDEO_LIST_URL, {
                "branchCode": account.branch_code,
                "userCode": account.user_code,
                "unionId": account.unionid,
                "seriesName": series_name,
            })
            if list_resp.get("code") != 200:
                print(f"⚠️ [视频] {series_name}: {list_resp.get('msg') or '列表获取失败'}")
                continue
            videos = list_resp.get("data") or []
            if isinstance(videos, dict):
                videos = videos.get("list") or videos.get("videoList") or []
            if not isinstance(videos, list):
                continue

            for video in videos[:8]:
                if not isinstance(video, dict):
                    continue
                if str(video.get("integralStatus", "")) == "1" or str(video.get("isGetIntegral", "")) == "1":
                    continue
                watch_length = parse_video_length(video.get("videoLength"))
                save_resp = account.api_post(VIDEO_SAVE_URL, {
                    "userCode": account.user_code,
                    "unionId": account.unionid,
                    "videoConfigId": video.get("videoConfigId") or video.get("id"),
                    "videoSubCategory": video.get("videoSubCategory", ""),
                    "watchLength": watch_length,
                    "isAlert": "0",
                    "isAgree": "",
                    "branchCode": account.branch_code,
                    "seriesName": series_name,
                    "flag": video.get("flag", ""),
                })
                if save_resp.get("code") == 200:
                    watched += 1
                    print(f"🎬 [视频] 观看上报成功: {video.get('videoName') or video.get('videoConfigId')} +{video.get('integral') or ''}")
                else:
                    msg = save_resp.get("msg") or ""
                    if msg and "重复" not in msg and "已" not in msg:
                        print(f"⚠️ [视频] {video.get('videoName') or ''}: {msg}")
                sleep(random.randint(1, 2))

        print(f"🎬 [视频] 本次观看上报 {watched} 个")
        return f"观看上报 {watched} 个"
    except Exception as exc:
        print(f"⚠️ [视频] 异常: {exc}")
        return "视频任务异常"


def parse_video_length(value: Any) -> int:
    """视频时长解析为秒，支持 分:秒 / 纯数字。"""
    if isinstance(value, (int, float)):
        return int(value) or 30
    text = str(value or "").strip()
    if ":" in text:
        try:
            parts = [int(p) for p in text.split(":")]
            total = 0
            for p in parts:
                total = total * 60 + p
            return total or 30
        except ValueError:
            return 30
    try:
        return int(float(text)) or 30
    except ValueError:
        return 30


def task_birthday(account: Account) -> str:
    """生日积分"""
    try:
        resp = account.api_post(BIRTHDAY_URL, {
            "userCode": account.user_code,
            "unionId": account.unionid,
        })
        if resp.get("code") != 200:
            return "生日信息获取失败"
        data = resp.get("data") or {}
        if not isinstance(data, dict):
            return "无生日信息"

        is_get = data.get("isGet")
        birthday = data.get("birthday") or data.get("birthDay") or ""
        if is_get in (1, "1", True):
            return "生日积分已领取"

        month_day = datetime.now().strftime("%m-%d")
        if birthday and month_day in str(birthday):
            save_resp = account.api_post(BIRTHDAY_SAVE_URL, {
                "userCode": account.user_code,
                "unionId": account.unionid,
            })
            if save_resp.get("code") == 200:
                print("🎂 [生日] 生日积分领取成功")
                return "生日积分领取成功"
            return save_resp.get("msg") or "生日积分领取失败"
        return f"非生日月({birthday})"
    except Exception as exc:
        print(f"⚠️ [生日] 异常: {exc}")
        return "生日任务异常"


def task_draw_num(account: Account) -> str:
    """抽奖次数查询"""
    resp = account.api_post(DRAW_NUM_URL, {
        "active_code": ACTIVE_CODE,
        "user_code": account.user_code,
        "unionId": account.unionid,
    })
    if resp.get("code") == 200:
        num = resp.get("data")
        if isinstance(num, bool):
            num = 1 if num else 0
        print(f"🎰 [抽奖] 当前可抽奖次数: {num}")
        return f"可抽奖 {num} 次"
    msg = resp.get("msg") or "抽奖次数查询失败"
    print(f"⚠️ [抽奖] {msg}")
    return msg


def task_collect(account: Account) -> str:
    """收取待领取的碳能量：待领取列表 → 逐条收取"""
    try:
        pending_resp = account.api_post(PENDING_INTEGRAL_URL, {
            "userCode": account.user_code,
            "unionId": account.unionid,
        })
        if pending_resp.get("code") != 200:
            return pending_resp.get("msg") or "待领取查询失败"

        data = pending_resp.get("data")
        items = data.get("tphPendingIntegralDetail") if isinstance(data, dict) else None
        if isinstance(data, list):
            items = data
        items = [item for item in (items or []) if isinstance(item, dict) and str(item.get("inFlag", "0")) == "0" and item.get("id")]
        if not items:
            print("🧺 [收取] 暂无可收取能量")
            return "无可收取能量"

        collected = 0
        for item in items:
            resp = account.api_post(COLLECT_INTEGRAL_URL, {
                "userCode": account.user_code,
                "unionId": account.unionid,
                "id": item.get("id"),
            })
            if resp.get("code") == 200:
                collected += 1
                print(f"🧺 [收取] +{item.get('integral')} {item.get('sysSourceSubcategory') or '能量'}")
            else:
                print(f"⚠️ [收取] {resp.get('msg') or '收取失败'}")
            sleep(1)

        print(f"🧺 [收取] 共收取 {collected} 笔")
        return f"收取 {collected} 笔"
    except Exception as exc:
        print(f"⚠️ [收取] 异常: {exc}")
        return "收取异常"


def task_integral(account: Account) -> Tuple[str, str]:
    """积分汇总：待领取 / 即将失效"""
    pending_text = "-"
    fail_text = "-"
    try:
        pending_resp = account.api_post(PENDING_INTEGRAL_URL, {
            "userCode": account.user_code,
            "unionId": account.unionid,
        })
        if pending_resp.get("code") == 200:
            data = pending_resp.get("data")
            if isinstance(data, dict):
                pending_text = str(data.get("tphPendingIntegralDetail") or data.get("integral") or data.get("pendingIntegral") or data)
            else:
                pending_text = str(data)
            print(f"💰 [积分] 待领取积分: {pending_text}")
    except Exception as exc:
        print(f"⚠️ [积分] 待领取查询异常: {exc}")

    try:
        fail_resp = account.api_post(FAIL_INTEGRAL_URL, {
            "userCode": account.user_code,
            "unionId": account.unionid,
        })
        if fail_resp.get("code") == 200:
            data = fail_resp.get("data")
            if isinstance(data, dict):
                fail_text = str(data.get("aboutFailIntegral", 0))
            else:
                fail_text = str(data)
            print(f"⏳ [积分] 即将失效积分: {fail_text}")
    except Exception as exc:
        print(f"⚠️ [积分] 失效查询异常: {exc}")

    return pending_text, fail_text


def task_task_list(account: Account) -> str:
    """低碳任务列表"""
    try:
        resp = account.api_post(TASK_LIST_URL, {
            "typeValue": "TAN_CATEGORY",
            "branchCode": account.branch_code,
            "unionId": account.unionid,
            "userCode": account.user_code,
        })
        if resp.get("code") != 200:
            return resp.get("msg") or "任务列表获取失败"
        data = resp.get("data") or []
        if isinstance(data, dict):
            data = data.get("list") or data.get("taskList") or []
        if not isinstance(data, list):
            return "任务列表为空"
        print(f"📋 [任务] 获取到 {len(data)} 个低碳任务场景")
        return f"任务场景 {len(data)} 个"
    except Exception as exc:
        print(f"⚠️ [任务] 异常: {exc}")
        return "任务列表异常"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "signStatus": "-",
        "attentionMsg": "-",
        "topMsg": "-",
        "videoMsg": "-",
        "collectMsg": "-",
        "birthdayMsg": "-",
        "drawMsg": "-",
        "taskMsg": "-",
        "pendingIntegral": "-",
        "failIntegral": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    if not check_gmssl():
        result["error"] = "缺少 gmssl 依赖"
        return result

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    account, raw_login = login_with_cache(server, proxies)
    if not account:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(account.token)

    try:
        result["signMsg"] = task_sign(account)
        sleep(random.randint(1, 3))

        result["signStatus"] = task_sign_status(account)
        sleep(random.randint(1, 2))

        result["attentionMsg"] = task_attention(account)
        sleep(random.randint(1, 2))

        result["topMsg"] = task_top_points(account)
        sleep(random.randint(1, 2))

        result["videoMsg"] = task_video(account)
        sleep(random.randint(1, 2))

        result["collectMsg"] = task_collect(account)
        sleep(random.randint(1, 2))

        result["birthdayMsg"] = task_birthday(account)
        sleep(random.randint(1, 2))

        result["drawMsg"] = task_draw_num(account)
        sleep(random.randint(1, 2))

        result["taskMsg"] = task_task_list(account)
        sleep(random.randint(1, 2))

        result["pendingIntegral"], result["failIntegral"] = task_integral(account)

        result["success"] = True
        return result

    except Exception:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {result['error'].splitlines()[-1]}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🌱 太平洋碳普惠多账号任务结果

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
📅 签到状态：{res["signStatus"]}
📮 关注：{res["attentionMsg"]}
👍 点赞：{res["topMsg"]}
🎬 视频：{res["videoMsg"]}
🧺 收取：{res["collectMsg"]}
🎂 生日：{res["birthdayMsg"]}
🎰 抽奖：{res["drawMsg"]}
📋 任务：{res["taskMsg"]}
💰 待领取积分：{res["pendingIntegral"]}
⏳ 即将失效：{res["failIntegral"]}
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
        except Exception:
            print(f"❌ [主程序] {server} 执行异常: {traceback.format_exc().splitlines()[-1]}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "signStatus": "-",
                "attentionMsg": "-",
                "topMsg": "-",
                "videoMsg": "-",
                "collectMsg": "-",
                "birthdayMsg": "-",
                "drawMsg": "-",
                "taskMsg": "-",
                "pendingIntegral": "-",
                "failIntegral": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 太平洋碳普惠任务执行完成                    ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🌱 太平洋碳普惠多账号任务完成", build_notify(results))


if __name__ == "__main__":
    main()
