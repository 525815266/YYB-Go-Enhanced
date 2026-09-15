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
TILTA影像城小程序（java.vrupup.com）签到动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. 使用 code 换 token（登录接口为推断，多个候选端点逐个尝试）
  3. 获取用户信息（昵称、手机号）
  4. 每日签到（按日历状态判断，未签自动签到）
  5. 做任务：文章点赞 / 浏览商品 / 阅读文章
  6. 汇总任务完成状态
  7. 查询积分
  8. PushPlus / 企业微信推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]

⚠️ 源脚本为抓包 authorization 型（无登录调用），登录接口为推断（POST
   /tietou-shop/api/login 等候选端点，body 带 code），未经真机验证，
   失败请抓包核对真实登录接口。
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


APP_NAME = "TILTA影像城小程序"
APPID = "wxca94758d1196d2ff"

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

API_BASE = "https://java.vrupup.com/tietou-shop/api"
# 登录接口为推断：按常见路径构造候选端点，逐个尝试，谁返回 token 用谁
LOGIN_URLS = [
    f"{API_BASE}/login",
    f"{API_BASE}/wx/login",
    f"{API_BASE}/user/login",
    f"{API_BASE}/auth/login",
]
USER_DETAIL_URL = f"{API_BASE}/get_user_detail"
SIGN_LIST_URL = f"{API_BASE}/personal/sign/list"
SIGN_SAVE_URL = f"{API_BASE}/personal/sign/save"
TASK_LIST_URL = f"{API_BASE}/task/list"
TASK_FINISH_URL = f"{API_BASE}/task/finishTask"
ARTICLE_PAGE_URL = f"{API_BASE}/article/page"
ARTICLE_LIKE_URL = f"{API_BASE}/article/like"
PRODUCT_CATE_LIST_URL = f"{API_BASE}/product/cate/list"
PRODUCT_PAGE_URL = f"{API_BASE}/product/page"
PRODUCT_DETAIL_URL = f"{API_BASE}/product/detail"
PRODUCT_PT_LIST_URL = f"{API_BASE}/v2/pt/queryptlist"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tltcookie.json")

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
    print("║ 🎥 TILTA影像城签到动态 code 版                ║")
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


def get_code(server: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url}")

    try:
        response = direct_session().get(
            url,
            params={"appId": APPID},
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


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/33/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["authorization"] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
        data.get("jwt"),
        data.get("authorization"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("jwt"),
            inner.get("authorization"),
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


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token（推断端点，逐个尝试）")
        last_data: Dict[str, Any] | None = None
        for login_url in LOGIN_URLS:
            try:
                response = request_with_proxy(
                    "POST",
                    login_url,
                    headers=common_headers(),
                    json={"code": code},
                    proxies=proxies,
                    server=server,
                )
                try:
                    data = response.json()
                except Exception:
                    data = {"raw": response.text[:800]}
                last_data = data
            except Exception as exc:
                print(f"⚠️ [登录] {login_url} 请求异常: {exc}")
                continue

            token = extract_token(data)
            if token:
                print(f"✅ [登录] token 获取成功: {mask(token)}")
                return token, data
            print(f"⚠️ [登录] {login_url} 未返回 token，尝试下一个")

        print(f"❌ [登录] 全部候选端点未识别 token: {json_preview(last_data)}")
        return None, last_data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token),
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


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token),
        json=payload,
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


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（用户信息接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            info_resp = api_post(server, USER_DETAIL_URL, cache_token, proxies, {})
            if info_resp.get("code") == 1:
                print("✅ [缓存] token 有效")
                return cache_token, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None

    token, raw_login = login_by_code(server, code, proxies)
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
    set_cached_token(server, token, expire_time)
    return token, raw_login


def fetch_article_id(server: str, token: str, proxies: Dict[str, str] | None, category_id: int) -> Any:
    """获取指定分类下的随机一篇文章 ID。categoryId：1 配件研究所 / 2 新闻中心 / 3 铁粉社区"""
    try:
        resp = api_post(server, ARTICLE_PAGE_URL, token, proxies, {
            "pageNumber": 1,
            "pageSize": 100,
            "articleCategoryId": category_id,
        })
        if resp.get("code") != 1:
            print(f"❌ [任务] 获取文章列表失败: {resp.get('msg') or json_preview(resp, 300)}")
            return None
        articles = [item for item in ((resp.get("data") or {}).get("list") or []) if isinstance(item, dict)]
        if not articles:
            print("❌ [任务] 文章列表为空")
            return None
        return random.choice(articles).get("id")
    except Exception as exc:
        print(f"❌ [任务] 获取文章列表异常: {exc}")
        return None


def like_article(server: str, token: str, proxies: Dict[str, str] | None, article_id: Any) -> None:
    """铁粉社区文章点赞。"""
    try:
        resp = api_post(server, ARTICLE_LIKE_URL, token, proxies, {"articleId": article_id})
        if resp.get("code") != 1:
            print(f"❌ [任务] 点赞文章失败: {resp.get('msg') or json_preview(resp, 300)}")
            return
        print("✅ [任务] 点赞文章成功！")
    except Exception as exc:
        print(f"❌ [任务] 点赞文章异常: {exc}")


def browse_product(server: str, token: str, proxies: Dict[str, str] | None) -> None:
    """首页浏览一件商品（随机分类 → 随机商品 → 详情 → 套餐列表）。"""
    try:
        resp = api_post(server, PRODUCT_CATE_LIST_URL, token, proxies, {"pid": 15, "type": 2})
        if resp.get("code") != 1:
            print(f"❌ [任务] 获取商品分类失败: {resp.get('msg') or json_preview(resp, 300)}")
            return
        categories = [item for item in (resp.get("data") or []) if isinstance(item, dict)]
        if not categories:
            print("❌ [任务] 商品分类为空")
            return
        cate_two_id = random.choice(categories).get("id")

        sleep(random.uniform(1.0, 1.5))
        page_resp = api_post(server, PRODUCT_PAGE_URL, token, proxies, {
            "pageNumber": 1,
            "pageSize": 10,
            "cateOneId": "15",
            "cateTwoId": cate_two_id,
            "name": "",
            "productType": "1",
            "sort": "",
        })
        if page_resp.get("code") != 1:
            print(f"❌ [任务] 获取商品列表失败: {page_resp.get('msg') or json_preview(page_resp, 300)}")
            return
        products = [item for item in ((page_resp.get("data") or {}).get("list") or []) if isinstance(item, dict)]
        if not products:
            print("❌ [任务] 商品列表为空")
            return
        product = random.choice(products)
        product_id = product.get("id")
        print(f"👀 [任务] 开始浏览【{product.get('name')}】")

        sleep(random.uniform(1.0, 2.0))
        detail_resp = api_post(server, PRODUCT_DETAIL_URL, token, proxies, {"id": product_id})
        if detail_resp.get("code") != 1:
            print(f"❌ [任务] 获取商品详情失败: {detail_resp.get('msg') or json_preview(detail_resp, 300)}")
            return

        sleep(random.uniform(1.0, 2.0))
        pt_resp = api_post(server, PRODUCT_PT_LIST_URL, token, proxies, {"id": product_id})
        if pt_resp.get("code") != 1:
            print(f"❌ [任务] 获取套餐列表失败: {pt_resp.get('msg') or json_preview(pt_resp, 300)}")
            return
        print("✅ [任务] 浏览商品成功")
    except Exception as exc:
        print(f"❌ [任务] 浏览商品异常: {exc}")


def finish_task(server: str, token: str, proxies: Dict[str, str] | None, task_id: int) -> None:
    """阅读类任务：请求后按源脚本等待 10~11s 再判定。"""
    try:
        resp = api_post(server, TASK_FINISH_URL, token, proxies, {"id": task_id})
        sleep(random.uniform(10.0, 11.0))
        if resp.get("code") != 1:
            print(f"❌ [任务] 完成任务失败: {resp.get('msg') or json_preview(resp, 300)}")
            return
        print("✅ [任务] 阅读成功！")
    except Exception as exc:
        print(f"❌ [任务] 完成任务异常: {exc}")


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "user": "-",
        "signMsg": "-",
        "taskMsg": "-",
        "points": "-",
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

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        # 1. 用户信息
        info_resp = api_post(server, USER_DETAIL_URL, token, proxies, {})
        if info_resp.get("code") == 1:
            info_data = safe_data(info_resp)
            nick_name = info_data.get("nickName") or "-"
            mobile = str(info_data.get("mobile") or "-")
            hidden_mobile = f"{mobile[:3]}***{mobile[-3:]}" if len(mobile) >= 6 else mobile
            result["user"] = f"{nick_name}({mobile})"
            print(f"👤 [用户] {nick_name}({hidden_mobile})")
        else:
            print(f"⚠️ [用户] 获取用户信息失败: {info_resp.get('msg') or json_preview(info_resp, 300)}")

        sleep(random.uniform(2.0, 2.5))

        # 2. 签到（按签到日历判断）
        now = datetime.now()
        plan_date = f"{now.year}-{now.month:02d}-{now.day}"
        day = now.day
        sign_resp = api_post(server, SIGN_LIST_URL, token, proxies, {"planDate": plan_date})
        if sign_resp.get("code") == 1:
            sign_data = None
            for item in ((safe_data(sign_resp).get("calendar")) or []):
                if isinstance(item, dict) and item.get("day") == day:
                    sign_data = item
                    break
            if sign_data:
                if sign_data.get("status") == 1:
                    result["signMsg"] = "今日已签到"
                    print(f"✅ [签到] {result['signMsg']}")
                else:
                    sleep(random.uniform(1.0, 1.5))
                    save_resp = api_post(server, SIGN_SAVE_URL, token, proxies, {})
                    if save_resp.get("code") == 1:
                        result["signMsg"] = "签到成功！"
                        print(f"✅ [签到] {result['signMsg']}")
                    else:
                        result["signMsg"] = f"签到失败: {save_resp.get('msg') or json_preview(save_resp, 300)}"
                        print(f"❌ [签到] {result['signMsg']}")
            else:
                result["signMsg"] = "未获取到签到任务，请手动打开小程序是否正常！"
                print(f"⚠️ [签到] {result['signMsg']}")
        else:
            print(f"⚠️ [签到] 获取签到列表失败: {sign_resp.get('msg') or json_preview(sign_resp, 300)}")

        sleep(random.uniform(2.0, 2.5))

        # 3. 做任务
        task_resp = api_post(server, TASK_LIST_URL, token, proxies, {})
        if task_resp.get("code") == 1:
            # 过滤[评价最近一笔商品订单]（id=6）
            tasks = [item for item in (task_resp.get("data") or []) if isinstance(item, dict) and item.get("id") != 6]
            for task in tasks:
                task_name = task.get("name") or "未知任务"
                if task.get("taskStatus") != 0:
                    print(f"✅ [任务] 任务【{task_name}】已完成")
                    sleep(random.uniform(0.888, 1.2))
                    continue
                print(f"🎯 [任务] 开始做【{task_name}】")
                sleep(random.uniform(1.0, 1.5))
                task_id = task.get("id")
                if task_id == 1:
                    # 铁粉社区文章点赞
                    article_id = fetch_article_id(server, token, proxies, 3)
                    sleep(random.uniform(1.0, 2.0))
                    if article_id is not None:
                        like_article(server, token, proxies, article_id)
                elif task_id == 2:
                    # 首页浏览两件商品
                    browse_product(server, token, proxies)
                    sleep(random.uniform(1.0, 2.0))
                    browse_product(server, token, proxies)
                elif task_id in (3, 7, 8):
                    # 3 铁粉社区文章 / 7 新闻中心 / 8 配件研究所，均阅读 10s
                    finish_task(server, token, proxies, task_id)
                else:
                    print(f"⚠️ [任务] 有新任务冒出来了【{task_name}】")
        else:
            print(f"⚠️ [任务] 获取任务列表失败: {task_resp.get('msg') or json_preview(task_resp, 300)}")

        sleep(random.uniform(2.0, 2.5))

        # 4. 更新任务状态汇总
        task_lines: List[str] = []
        task_resp2 = api_post(server, TASK_LIST_URL, token, proxies, {})
        if task_resp2.get("code") == 1:
            tasks2 = [item for item in (task_resp2.get("data") or []) if isinstance(item, dict) and item.get("id") != 6]
            for task in tasks2:
                task_name = task.get("name") or "未知任务"
                if task.get("taskStatus") == 1:
                    task_lines.append(f"{task_name}---已完成✅")
                else:
                    task_lines.append(f"{task_name}---未完成❌")
            result["taskMsg"] = "\n".join(task_lines) if task_lines else "-"
        else:
            print(f"⚠️ [任务] 更新任务状态失败: {task_resp2.get('msg') or json_preview(task_resp2, 300)}")

        sleep(random.uniform(2.0, 2.5))

        # 5. 查询积分
        points_resp = api_post(server, USER_DETAIL_URL, token, proxies, {})
        if points_resp.get("code") == 1:
            points_value = safe_data(points_resp).get("point")
            result["points"] = str(points_value) if points_value is not None else "-"
            print(f"💰 [积分] 当前积分: {result['points']}")
        else:
            print(f"⚠️ [积分] 获取积分失败: {points_resp.get('msg') or json_preview(points_resp, 300)}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🎥 TILTA影像城任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
👤 用户：{res["user"]}
📝 签到：{res["signMsg"]}
🎯 任务：{res["taskMsg"]}
💰 积分：{res["points"]}
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
                "user": "-",
                "signMsg": "-",
                "taskMsg": "-",
                "points": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 TILTA影像城任务执行完成                    ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🎥 TILTA影像城任务完成", build_notify(results))


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
