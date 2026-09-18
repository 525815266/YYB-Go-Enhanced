#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ========== 企业微信推送配置（可选） ==========
QYWX_TOKEN = __import__("os").getenv("QYWX_TOKEN", "")  # 企业微信机器人 Webhook key（机器人地址 ?key= 后面的值，留空不推送）

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
jingjianx 潮玩/电玩门店统一签到动态 code 版（jingjianx 白标 SaaS 平台）

覆盖 11 个同体系小程序（与「潮玩门店统一」互补）：
  极速玩家巴中经开店 / 嘉鱼县梦幻岛游乐园 / 七彩潮玩城 / 吉合家庭娱乐超乐场 /
  SUPER101潮漫电玩宜都店 / 城市星空颖上恒太 / 维京传奇长春店 / 像素玩家电玩城 /
  星贝乐阜阳商厦中心店 / 中嘉动漫创美店 / 中嘉动漫城樽憬店

功能：
  1. 本地 code 服务按各小程序 appid 分别获取微信 code
  2. /capp/account/login 使用 code 换 token（Bearer + JJ-CHAINID/JJ-SHOPID 头，含缓存与自动刷新）
  3. 查询会员信息与各店资产（代币/积分/彩票/优惠券）
  4. 每店签到（getreward 判断开启 → getprogress 判断已签 → confirm/newconfirm 签到）
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN      PushPlus token，可选
  QYWX_TOKEN          企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API           品赞代理提取 API，可选
  PROXY_TYPE          http / socks5，默认 http
  CODE_SERVER         本地 code 服务地址，默认 127.0.0.1:8088
  {门店缩写}_api_base    覆盖 API 域名（默认 https://capi.jingjianx.vip）
  {门店缩写}_chainid     覆盖 chainId
  {门店缩写}_version     覆盖小程序版本标识（默认 release）
  {门店缩写}_shop_ids    逗号分隔，过滤/追加门店

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import json
import os
import random
import re
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "jingjianx潮玩门店统一"
APPID = [
    "wx174a477493da1aeb",
    "wxb7fcdb0c375bac0c",
    "wx72727cb43e04f838",
    "wxdc43cd8bfcfd00ad",
    "wx1031a0ff78dbb777",
    "wxab97f393b22fc3f0",
    "wxf4a3dae0d0cfd841",
    "wx16ad44c68249d573",
    "wxf0a7f75ab1670953",
    "wx97b6b57988678880",
    "wx334c4ec677c62f96",
]

SERVERS = [
    "127.0.0.1:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

APPS = [
    {"ck": "jswjbzjk", "name": "极速玩家巴中经开店", "appid": "wx174a477493da1aeb", "chainId": "4748",
     "shops": [{"shopId": "19081", "shopName": "极速玩家巴中经开店"}]},
    {"ck": "jyxmhdylly", "name": "嘉鱼县梦幻岛游乐园", "appid": "wxb7fcdb0c375bac0c", "chainId": "10762",
     "shops": [{"shopId": "18752", "shopName": "嘉鱼县梦幻岛游乐园"}]},
    {"ck": "qccwc", "name": "七彩潮玩城", "appid": "wx72727cb43e04f838", "chainId": "7243",
     "shops": [{"shopId": "12860", "shopName": "七彩潮玩城"}]},
    {"ck": "jhjtylclc", "name": "吉合家庭娱乐超乐场", "appid": "wxdc43cd8bfcfd00ad", "chainId": "3580",
     "shops": [{"shopId": "8551", "shopName": "吉合家庭娱乐超乐场"}]},
    {"ck": "super101cmdwyd", "name": "SUPER101潮漫电玩宜都店", "appid": "wx1031a0ff78dbb777", "chainId": "3091",
     "shops": [{"shopId": "13516", "shopName": "SUPER101潮漫电玩宜都店"}]},
    {"ck": "wx_three_sign", "name": "城市星空颖上恒太", "appid": "wxab97f393b22fc3f0", "chainId": "3900",
     "shops": [{"shopId": "7726", "shopName": "城市星空颖上恒太"}]},
    {"ck": "wjcq", "name": "维京传奇长春店", "appid": "wxf4a3dae0d0cfd841", "chainId": "11383",
     "shops": [{"shopId": "19820", "shopName": "维京传奇长春店"}]},
    {"ck": "xswj", "name": "像素玩家电玩城", "appid": "wx16ad44c68249d573", "chainId": "10557",
     "shops": [{"shopId": "18417", "shopName": "像素玩家电玩城"}]},
    {"ck": "xblfysx", "name": "星贝乐阜阳商厦中心店", "appid": "wxf0a7f75ab1670953", "chainId": "3900",
     "shops": [{"shopId": "17457", "shopName": "星贝乐阜阳商厦中心店"}]},
    {"ck": "zjdmc", "name": "中嘉动漫创美店", "appid": "wx97b6b57988678880", "chainId": "8006",
     "shops": [{"shopId": "21310", "shopName": "中嘉动漫创美店"}]},
    {"ck": "zjdmzj", "name": "中嘉动漫城樽憬店", "appid": "wx334c4ec677c62f96", "chainId": "8006",
     "shops": [{"shopId": "16741", "shopName": "中嘉动漫城樽憬店"}]},
]

# 源脚本支持的环境变量覆盖（api_base / chainid / version / shop_ids）
for _app in APPS:
    _app["apiBase"] = os.getenv(f"{_app['ck']}_api_base") or "https://capi.jingjianx.vip"
    _app["chainId"] = os.getenv(f"{_app['ck']}_chainid") or _app["chainId"]
    _app["version"] = os.getenv(f"{_app['ck']}_version") or "release"
    _shop_ids = [item.strip() for item in re.split(r"[,，&;\n]+", os.getenv(f"{_app['ck']}_shop_ids") or "") if item.strip()]
    if _shop_ids:
        _kept = [shop for shop in _app["shops"] if str(shop["shopId"]) in _shop_ids]
        _known = {str(shop["shopId"]) for shop in _app["shops"]}
        _kept += [{"shopId": sid, "shopName": f"门店{sid}"} for sid in _shop_ids if sid not in _known]
        _app["shops"] = _kept

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jjxallcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) MicroMessenger/3.9.12 "
    "MiniProgramEnv/Windows WindowsWechat/WMPF"
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
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🧸 jingjianx潮玩门店统一动态 code 版          ║")
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



def send_qywx(title, content):
    """企业微信机器人推送（Webhook）。未配置 QYWX_TOKEN 时自动跳过。"""
    if not QYWX_TOKEN:
        print("[企业微信] 未配置 QYWX_TOKEN，跳过推送")
        return False
    key = QYWX_TOKEN.split("key=")[-1].strip()
    import json as _qywx_json, urllib.request as _qywx_urllib
    try:
        text = "%s\n%s" % (title, content)
        if len(text.encode("utf-8")) > 2000:
            text = text.encode("utf-8")[:2000].decode("utf-8", "ignore")
        payload = _qywx_json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
        req = _qywx_urllib.Request("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key,
                                   data=payload, headers={"Content-Type": "application/json"})
        res = _qywx_json.loads(_qywx_urllib.urlopen(req, timeout=10).read().decode("utf-8"))
        ok = res.get("errcode") == 0
        print("[企业微信] 推送%s errcode=%s errmsg=%s" % ("成功 ✓" if ok else "失败 ✗", res.get("errcode"), res.get("errmsg", "")))
        return ok
    except Exception as _exc:
        print("[企业微信] 推送异常:", _exc)
        return False
def send_pushplus(title: str, content: str) -> None:
    send_qywx(title, content)  # 企业微信推送（QYWX_TOKEN 未配置时自动跳过）
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


def get_code(server: str, appid: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url} appId={appid}")

    try:
        response = direct_session().get(
            url,
            params={"appId": appid},
            timeout=20,
        )
        data = response.json()

        if data.get("err") != 0 or not data.get("code"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def common_headers(app: Dict[str, Any], shop_id: str = "", token: str = "",
                   extra: Dict[str, str] | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{app['appid']}/1/page-frame.html",
        "content-type": "application/json",
        "JJ-CHAINID": app["chainId"],
        "BDSZH-SHOPID": shop_id,
        "JJ-SHOPID": shop_id,
        "JJ-MiniAppVersion": app["version"],
        "JJ-AppId": app["appid"],
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update(extra)
    return headers


def create_guid() -> str:
    return str(uuid.uuid4())


def is_today(date_text: str) -> bool:
    if not date_text:
        return False
    return str(date_text).startswith(datetime.now().strftime("%Y-%m-%d"))


def mask_phone(phone: str = "") -> str:
    return re.sub(r"^(\d{3})\d{4}(\d{4})$", r"\1****\2", str(phone or ""))


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


def login_by_code(server: str, code: str, app: Dict[str, Any], shop: Dict[str, Any],
                  proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print(f"🔐 [登录] [{app['name']}][{shop['shopName']}] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            f"{app['apiBase']}/capp/account/login",
            headers=common_headers(app, shop["shopId"]),
            json={"code": code},
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"success": False, "msg": f"JSON解析失败: {response.text[:300]}"}

        if not data.get("success"):
            print(f"❌ [登录] [{app['name']}][{shop['shopName']}] 登录失败: {data.get('msg') or json_preview(data, 200)}")
            return None, data

        token = extract_token(data)
        if token:
            print(f"✅ [登录] [{app['name']}][{shop['shopName']}] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None,
            headers: Dict[str, str] | None = None, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=headers or {},
        params=params,
        proxies=proxies,
        server=server,
    )
    if response.status_code > 400:
        return {"success": False, "msg": f"HTTP {response.status_code}: {response.text[:200]}"}
    try:
        return response.json()
    except Exception:
        return {
            "success": False,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any],
             headers: Dict[str, str] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=headers or {},
        json=payload,
        proxies=proxies,
        server=server,
    )
    if response.status_code > 400:
        return {"success": False, "msg": f"HTTP {response.status_code}: {response.text[:200]}"}
    try:
        return response.json()
    except Exception:
        return {
            "success": False,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


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


def get_cached_token(server: str) -> str | None:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} token")
                return data["token"]
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, token: str, expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def save_member_info(cache_key: str, info: Dict[str, Any]) -> None:
    """会员信息与 token 同文件缓存（token 五件套之外的补充存储）"""
    cache = load_token_cache()
    cache[f"{cache_key}|member"] = info
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"⚠️ [缓存] 会员信息保存失败: {exc}")


def load_member_info(cache_key: str) -> Dict[str, Any]:
    cache = load_token_cache()
    info = cache.get(f"{cache_key}|member")
    return info if isinstance(info, dict) else {}


def check_token(app: Dict[str, Any], shop: Dict[str, Any], token: str,
                proxies: Dict[str, str] | None, server: str) -> bool:
    """用签到配置接口验证 token 是否仍有效"""
    try:
        result = api_get(
            server,
            f"{app['apiBase']}/signed/capp/signed/getreward",
            token,
            proxies,
            headers=common_headers(app, shop["shopId"], token),
        )
        return bool(result.get("success"))
    except Exception:
        return False


def login_with_cache(app: Dict[str, Any], shop: Dict[str, Any], server: str, cache_key: str,
                     proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（签到配置接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(cache_key)
    fresh_login: Dict[str, Any] | None = None
    if cache_token:
        print(f"🔍 [缓存] [{app['name']}][{shop['shopName']}] 验证 token")
        if check_token(app, shop, cache_token, proxies, server):
            print(f"✅ [缓存] token 有效")
            return cache_token, fresh_login
        print(f"⚠️ [缓存] [{app['name']}][{shop['shopName']}] token 已失效，重新登录")

    code = get_code(server, app["appid"])
    if not code:
        return None, None

    token, raw_login = login_by_code(server, code, app, shop, proxies)
    if not token:
        return None, raw_login

    expire_time = None
    if raw_login and isinstance(raw_login, dict):
        inner = raw_login.get("data")
        if isinstance(inner, dict):
            expire_time = inner.get("expireTime") or inner.get("expire_time")
            expires_in = inner.get("expiresIn")
            if not expire_time and isinstance(expires_in, (int, float)) and expires_in > 0:
                expire_time = datetime.fromtimestamp(time.time() + expires_in).isoformat()
    if not expire_time:
        expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    elif not isinstance(expire_time, str):
        expire_time = datetime.fromtimestamp(expire_time / 1000).isoformat()
    set_cached_token(cache_key, token, expire_time)
    return token, raw_login


def add_store_asset(assets: Dict[str, float], category: Any, value: Any, name: str = "") -> None:
    amount = to_float(value)
    label = str(name or "")
    if label:
        if re.search(r"积分|Point", label, re.IGNORECASE):
            assets["integral"] += amount
        elif re.search(r"彩票|票", label):
            assets["ticket"] += amount
        elif re.search(r"游戏币|本币|代币", label):
            assets["coin"] += amount
        return
    try:
        cat = int(float(category))
    except (TypeError, ValueError):
        cat = 0
    if cat in (101, 102, 103):
        assets["coin"] += amount
    elif cat == 105:
        assets["integral"] += amount
    elif cat in (104, 106, 1001, 1002, 1003):
        assets["ticket"] += amount


def get_assets(app: Dict[str, Any], shop: Dict[str, Any], token: str, server: str,
               proxies: Dict[str, str] | None) -> Dict[str, float]:
    assets = {"coin": 0.0, "integral": 0.0, "ticket": 0.0, "coupon": 0.0}
    account_names: Dict[int, str] = {}
    try:
        account_resp = api_get(
            server,
            f"{app['apiBase']}/basic/shop/xcx/scene/getaccount",
            token,
            proxies,
            headers=common_headers(app, shop["shopId"], token),
        )
        account_list = account_resp.get("data") if isinstance(account_resp.get("data"), list) else []
        for item in account_list:
            if isinstance(item, dict):
                try:
                    account_names[int(float(item.get("key")))] = str(item.get("value") or "")
                except (TypeError, ValueError):
                    pass
    except Exception as exc:
        print(f"⚠️ [资产] [{app['name']}][{shop['shopName']}] 查询账户类型失败: {exc}")

    try:
        store_resp = api_get(
            server,
            f"{app['apiBase']}/member/capp/member/store/get",
            token,
            proxies,
            headers=common_headers(app, shop["shopId"], token),
        )
        store_list = store_resp.get("data") if isinstance(store_resp.get("data"), list) else []
        for item in store_list:
            if isinstance(item, dict):
                try:
                    cat_key = int(float(item.get("storeCategory")))
                except (TypeError, ValueError):
                    cat_key = 0
                add_store_asset(assets, item.get("storeCategory"), item.get("value"), account_names.get(cat_key, ""))
    except Exception as exc:
        print(f"⚠️ [资产] [{app['name']}][{shop['shopName']}] 查询资产失败: {exc}")

    try:
        coupon_resp = api_get(
            server,
            f"{app['apiBase']}/coupon/capp/membercoupon/count",
            token,
            proxies,
            headers=common_headers(app, shop["shopId"], token),
        )
        if coupon_resp.get("success"):
            assets["coupon"] = to_float(coupon_resp.get("data") or 0)
    except Exception as exc:
        print(f"⚠️ [资产] [{app['name']}][{shop['shopName']}] 查询优惠券失败: {exc}")

    return assets


def get_signed_state(reward: Dict[str, Any], progress: Dict[str, Any]) -> Dict[str, Any]:
    sign_type = int(to_float(reward.get("signCycle")))
    sign_mode = int(to_float(reward.get("signMode") or 1))

    if sign_type == 0:
        return {
            "signType": sign_type,
            "signMode": sign_mode,
            "signed": not progress.get("isSigning"),
            "currentDay": progress.get("signInDays") or 0,
            "apiPath": "/signed/capp/signed/confirm",
        }

    if sign_mode == 2 and progress.get("totalDay"):
        total_day = progress["totalDay"]
        return {
            "signType": sign_type,
            "signMode": sign_mode,
            "signed": not total_day.get("isSigning"),
            "currentDay": total_day.get("signInDays") or 0,
            "apiPath": "/signed/capp/signed/newconfirm",
        }

    if sign_mode == 3 and isinstance(progress.get("dailyDay"), list):
        daily_day = [item for item in progress["dailyDay"] if isinstance(item, dict)]
        return {
            "signType": sign_type,
            "signMode": sign_mode,
            "signed": any(is_today(item.get("signDate")) for item in daily_day),
            "currentDay": len(daily_day),
            "apiPath": "/signed/capp/signed/newconfirm",
        }

    total_day = progress.get("totalDay") or {}
    return {
        "signType": sign_type,
        "signMode": sign_mode,
        "signed": (not total_day.get("isSigning")) if total_day else False,
        "currentDay": total_day.get("signInDays") or 0,
        "apiPath": "/signed/capp/signed/newconfirm",
    }


def shop_sign(app: Dict[str, Any], shop: Dict[str, Any], token: str, server: str,
              proxies: Dict[str, str] | None) -> str:
    try:
        reward_resp = api_get(
            server,
            f"{app['apiBase']}/signed/capp/signed/getreward",
            token,
            proxies,
            headers=common_headers(app, shop["shopId"], token),
        )
        if not reward_resp.get("success"):
            return f"签到失败({reward_resp.get('msg') or '未知'})"
        reward = reward_resp.get("data") or {}
        if not reward.get("isEnabled"):
            return "未开启签到"

        progress_path = ("/signed/capp/signed/getprogress"
                         if int(to_float(reward.get("signCycle"))) == 0
                         else "/signed/capp/signed/getnewprogress")
        progress_resp = api_get(
            server,
            f"{app['apiBase']}{progress_path}",
            token,
            proxies,
            headers=common_headers(app, shop["shopId"], token),
        )
        if not progress_resp.get("success"):
            return f"签到失败({progress_resp.get('msg') or '未知'})"

        state = get_signed_state(reward, progress_resp.get("data") or {})
        print(
            f"ℹ️ [签到] [{app['name']}][{shop['shopName']}] 签到状态: signType={state['signType']} "
            f"signMode={state['signMode']} currentDay={state['currentDay']} signed={state['signed']}"
        )
        if state["signed"]:
            return f"今日已签到(第{state['currentDay']}天)"

        result = api_post(
            server,
            f"{app['apiBase']}{state['apiPath']}",
            token,
            proxies,
            {"longitude": 0, "latitude": 0},
            headers=common_headers(app, shop["shopId"], token, {"JJ-BizCode": create_guid()}),
        )
        if not result.get("success"):
            return f"签到失败({result.get('msg') or '未知'})"
        data = result.get("data") or {}
        reward_name = data.get("rewardName") or data.get("prizeName")
        return f"签到成功{f' 奖励={reward_name}' if reward_name else ''}"
    except Exception as exc:
        return f"签到失败({exc})"


def shop_flow(app: Dict[str, Any], shop: Dict[str, Any], server: str,
              proxies: Dict[str, str] | None) -> Dict[str, Any]:
    cache_key = f"{app['appid']}:{server}:{shop['shopId']}"
    summary = {
        "appName": app["name"],
        "shopName": shop["shopName"],
        "loginShopName": shop["shopName"],
        "memberText": "未知用户",
        "assets": {"coin": 0.0, "integral": 0.0, "ticket": 0.0, "coupon": 0.0},
        "sign": "未执行",
    }

    token, raw_login = login_with_cache(app, shop, server, cache_key, proxies)
    if not token:
        summary["sign"] = "登录失败"
        print(f"❌ [结果] [{app['name']}][{shop['shopName']}] 结果={summary['sign']}")
        return summary

    member_info = load_member_info(cache_key)
    if raw_login and isinstance(raw_login, dict) and isinstance(raw_login.get("data"), dict):
        login_data = raw_login["data"]
        member_info = {
            "isMember": bool(login_data.get("isMember")),
            "member": login_data.get("member") or {},
            "shopName": str(login_data.get("shopName") or shop["shopName"]),
        }
        save_member_info(cache_key, member_info)

    is_member = bool(member_info.get("isMember"))
    member = member_info.get("member") or {}
    if isinstance(member, dict) and member:
        nick = member.get("nickName") or member.get("name") or member.get("memberName") or "member"
        phone = mask_phone(str(member.get("phone") or ""))
        summary["memberText"] = f"{nick} {phone}".strip()
    else:
        summary["memberText"] = "会员" if is_member else "非会员"
    if member_info.get("shopName"):
        summary["loginShopName"] = str(member_info["shopName"])

    if is_member:
        summary["assets"] = get_assets(app, shop, token, server, proxies)
    print(
        f"💰 [资产] [{app['name']}][{shop['shopName']}] 登录门店={summary['loginShopName']} member={is_member} "
        f"{summary['memberText']} 代币={summary['assets']['coin']:g} 积分={summary['assets']['integral']:g} "
        f"彩票={summary['assets']['ticket']:g} 优惠券={summary['assets']['coupon']:g}"
    )

    if not is_member:
        summary["sign"] = "非会员跳过"
        print(f"⚠️ [签到] [{app['name']}][{shop['shopName']}] 签到: {summary['sign']}")
        return summary

    summary["sign"] = shop_sign(app, shop, token, server, proxies)
    icon = "✅" if ("签到成功" in summary["sign"] or "已签到" in summary["sign"] or "未开启" in summary["sign"]) else "⚠️"
    print(f"{icon} [签到] [{app['name']}][{shop['shopName']}] 签到: {summary['sign']}")
    return summary


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "shopSummary": "-",
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

    lines: List[str] = []
    ok_shops = 0
    total_shops = 0

    try:
        for app in APPS:
            print(f"\n===== {app['name']} appid={app['appid']} chainId={app['chainId']} 门店数={len(app['shops'])} =====")
            for shop in app["shops"]:
                total_shops += 1
                summary = shop_flow(app, shop, server, proxies)
                sign_text = summary["sign"]
                if ("签到成功" in sign_text or "已签到" in sign_text or "未开启" in sign_text
                        or "非会员" in sign_text or "无需重复预约" in sign_text):
                    ok_shops += 1
                lines.append(
                    f"{summary['appName']} | {summary['loginShopName']} | {summary['memberText']} | "
                    f"代币={summary['assets']['coin']:g} 积分={summary['assets']['integral']:g} "
                    f"彩票={summary['assets']['ticket']:g} 优惠券={summary['assets']['coupon']:g} | {sign_text}"
                )
                sleep(random.randint(1, 2))

        result["shopSummary"] = f"{ok_shops}/{total_shops} 店正常"
        result["success"] = ok_shops > 0
        result["_lines"] = lines
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        result["_lines"] = lines
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🧸 jingjianx潮玩门店统一任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🏪 门店：{res["shopSummary"]}
"""
        for line in res.get("_lines", []):
            content += f"  · {line}\n"
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
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "shopSummary": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 jingjianx潮玩门店统一任务执行完成       ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🧸 jingjianx潮玩门店统一任务完成", build_notify(results))


# --- YYB compatibility layer (managed) ---
import os as _yyb_os
import json as _yyb_json

def _yyb_accounts():
    result = []
    for line in _yyb_os.getenv("YYB_SERVER", "").splitlines():
        line = line.strip()
        if not line or "@" not in line or line == "[object Object]":
            continue
        endpoint, ref = (part.strip() for part in line.split("@", 1))
        if endpoint and ref:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = "http://" + endpoint
            result.append(endpoint.rstrip("/") + "@" + ref)
    return result


def _yyb_parts(server):
    value = str(server).strip()
    if "@" not in value:
        return value.rstrip("/"), ""
    return value.rsplit("@", 1)[0].rstrip("/"), value.rsplit("@", 1)[1]


def _yyb_appid(args, kwargs):
    appid = kwargs.get("appid") or kwargs.get("app_id")
    if not appid and args and isinstance(args[0], str):
        appid = args[0]
    if not appid:
        appid = globals().get("APPID") or globals().get("APP_ID") or ""
    if isinstance(appid, (list, tuple)):
        appid = appid[0] if appid else ""
    return str(appid)


def _yyb_json_request(server, path, appid, payload=None):
    import requests
    endpoint, ref = _yyb_parts(server)
    if not endpoint or not ref or not appid:
        raise RuntimeError("YYB 参数不完整：需要 地址@账号ID 和 app_id")
    headers = {}
    api_key = _yyb_os.getenv("YYB_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    response = requests.post(
        endpoint + path,
        json={"ref": ref, "app_id": str(appid), **(payload or {})},
        headers=headers,
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("YYB 返回非 JSON") from exc
    if response.status_code >= 400:
        raise RuntimeError(str(body.get("message") or body.get("msg") or body))
    return body


def _yyb_find_code(value):
    if isinstance(value, dict):
        if value.get("code") not in (None, "", "null", "invalid") and isinstance(value.get("code"), str):
            return value["code"]
        for child in value.values():
            found = _yyb_find_code(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _yyb_find_code(child)
            if found:
                return found
    return None


def _yyb_code(server, *args, **kwargs):
    body = _yyb_json_request(server, "/wxapp/getCode", _yyb_appid(args, kwargs))
    code = _yyb_find_code(body)
    if not code:
        raise RuntimeError(str(body.get("msg") or body.get("message") or "YYB 未返回 wx.login code"))
    return str(code)


def _yyb_phone(server, *args, **kwargs):
    body = _yyb_json_request(server, "/wxapp/getPhoneNumber", _yyb_appid(args, kwargs))
    return body.get("result") or body.get("data") or body


if _yyb_accounts():
    SERVERS = _yyb_accounts()


_yyb_original_get_code = get_code


def get_code(server, *args, **kwargs):
    if "@" in str(server):
        phone_code = kwargs.get("phone_code") is True or (args and args[0] is True)
        if phone_code:
            body = _yyb_phone(server)
            code = _yyb_find_code(body)
            if not code:
                raise RuntimeError("YYB 未返回手机号授权 code")
            return str(code)
        return _yyb_code(server, *args, **kwargs)
    return _yyb_original_get_code(server, *args, **kwargs)


if "get_code_for" in globals():
    _yyb_original_get_code_for = get_code_for

    def get_code_for(server, appid):
        if "@" in str(server):
            return _yyb_code(server, appid)
        return _yyb_original_get_code_for(server, appid)


def get_phone_payload(server, *args, **kwargs):
    if "@" in str(server):
        return _yyb_phone(server, *args, **kwargs)
    raise RuntimeError("当前账号不是 YYB_SERVER 格式，无法获取手机号授权包")


# Adapt the two common source-specific phone helpers when present.
if "get_phone_number_payload" in globals():
    get_phone_number_payload = get_phone_payload
if "get_phone_authorize" in globals():
    get_phone_authorize = get_phone_payload
if "get_phone_data" in globals():
    get_phone_data = get_phone_payload
if "get_phone_package" in globals():
    _yyb_original_get_phone_package = get_phone_package

    def get_phone_package(server, *args, **kwargs):
        if "@" not in str(server):
            return _yyb_original_get_phone_package(server, *args, **kwargs)
        body = _yyb_phone(server, *args, **kwargs)
        # YYB keeps the upstream response shape. Do not invent encryptedData/iv.
        if isinstance(body, dict):
            result = body.get("result") if isinstance(body.get("result"), dict) else body
            data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
            raw = data.get("raw") if isinstance(data, dict) and isinstance(data.get("raw"), dict) else data
            if isinstance(raw, dict):
                return {
                    "authCode": str(raw.get("code") or data.get("code") or ""),
                    "encryptPhoneNumber": str(raw.get("encryptedData") or ""),
                    "initVector": str(raw.get("iv") or ""),
                }
        return {"authCode": "", "encryptPhoneNumber": "", "initVector": ""}
if "get_userinfo_blob" in globals():
    _yyb_original_get_userinfo_blob = get_userinfo_blob

    def get_userinfo_blob(server):
        if "@" not in str(server):
            return _yyb_original_get_userinfo_blob(server)
        body = _yyb_json_request(server, "/wx/getuserinfo", _yyb_appid((), {}))
        info = body.get("user_info")
        # /wx/getuserinfo is a profile endpoint; it is not a source of
        # encryptedData/iv/signature. Never fabricate those fields.
        if not isinstance(info, dict):
            return None
        return {"userInfo": info, "rawData": _yyb_json.dumps(info, ensure_ascii=False), "errMsg": "getUserInfo:ok"}
# --- end YYB compatibility layer ---

if __name__ == "__main__":
    main()
