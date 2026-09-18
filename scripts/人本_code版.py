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
人本（认养一头牛官方商城）code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /mall/xhr/minilogin 使用 code 换 token（token 优先取响应头 X-Auth-Token）
  3. 每日签到（签到并返回等级/手机号/积分）
  4. 自动申请可申请的试用商品（按等级匹配）
  5. 查询中奖记录
  6. 社区每日答题（随机选项作答）
  7. 社区发帖种草后自动删帖
  8. PushPlus 推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       本地 code 服务地址，默认 127.0.0.1:8088

⚠️ 登录限制：源脚本为“手机号一键授权登录”，/mall/xhr/minilogin 正常需要
   encryptedData/offset(iv)/phoneCode（手机号授权数据，本地 code 服务无法提供），
   本脚本仅用 wx.login 的 wxCode 尝试登录，未真机验证，失败请抓包核对。

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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "人本（认养一头牛官方商城）"
APPID = "wx0408f3f20d769a2f"

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

BASE_URL = "https://www.milkcard.mall.ryytngroup.com"
LOGIN_URL = f"{BASE_URL}/mall/xhr/minilogin"

CHECKIN_SAVE_URL = f"{BASE_URL}/mall/xhr/task/checkin/save"
CHECKIN_RULE_URL = f"{BASE_URL}/mall/xhr/task/checkin/getRule"
ADDRESS_LIST_URL = f"{BASE_URL}/mall/xhr/address/receive/list"
TRIAL_LIST_URL = f"{BASE_URL}/mall/xhr/freeTrial/getList"
TRIAL_APPLY_URL = f"{BASE_URL}/mall/xhr/freeTrial/apply"
WINNING_LIST_URL = f"{BASE_URL}/mall/xhr/freeTrialUser/getList"
QUIZ_ACTIVITIES_URL = f"{BASE_URL}/mall/xhr/quizActivity/activities"
QUIZ_SUBMIT_URL = f"{BASE_URL}/mall/xhr/quizActivity/submit"
COMMUNITY_RECOMMEND_URL = f"{BASE_URL}/mall/xhr/community/home/recommend/item"
COMMUNITY_PUSH_URL = f"{BASE_URL}/mall/xhr/community/posts/push"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ryytncookie.json")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.61(0x18003d24) "
    "NetType/4G Language/zh_CN"
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
    data = resp.get("data")
    return data if isinstance(data, dict) else {}


def mask_phone(phone: str) -> str:
    phone = str(phone or "")
    if len(phone) >= 11:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone


def beijing_today_0am() -> str:
    now = datetime.now(timezone(timedelta(hours=8)))
    return now.strftime("%Y-%m-%d 00:00:00")


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🥛 人本（认养一头牛）动态 code 版            ║")
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
        "Accept": "application/json",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/305/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["X-Auth-Token"] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("x-auth-token"),
        data.get("xAuthToken"),
        data.get("accessToken"),
        data.get("access_token"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("x-auth-token"),
            inner.get("xAuthToken"),
            inner.get("accessToken"),
            inner.get("access_token"),
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
            json={
                "encryptedData": "",
                "offset": "",
                "wxCode": code,
                "code": "",
            },
            proxies=proxies,
            server=server,
        )

        # token 优先取响应头 X-Auth-Token
        token = response.headers.get("X-Auth-Token") or response.headers.get("x-auth-token")

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        if not token:
            token = extract_token(data)

        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return str(token), data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
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
    """优先使用缓存 token（中奖记录接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            resp = api_post(
                server,
                WINNING_LIST_URL,
                cache_token,
                proxies,
                {"parentTabStatus": 1, "pageNum": 1, "pageSize": 10, "subTabStatus": 1},
            )
            if resp.get("code") == 200:
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


def check_checkin(server: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    """签到并返回账号状态（幂等），data 含 grade/phone/point"""
    return api_post(server, CHECKIN_SAVE_URL, token, proxies, {})


def run_quiz(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    quiz_resp = api_get(server, QUIZ_ACTIVITIES_URL, token, proxies)
    if quiz_resp.get("code") != 200:
        print(f"⚠️ [答题] 获取社区答题题库失败: {quiz_resp.get('msg') or json_preview(quiz_resp, 300)}")
        return "获取题库失败"

    activities = quiz_resp.get("data") if isinstance(quiz_resp.get("data"), list) else []
    today = beijing_today_0am()
    today_quiz = next((item for item in activities if item.get("relatedDate") == today), None)
    if not today_quiz:
        print("⚠️ [答题] 今日暂无社区答题题目")
        return "今日无题目"

    print(f"📝 [答题] 获取到社区答题题目: {today_quiz.get('questionTitle')}")
    try:
        options = json.loads(today_quiz.get("options") or "[]")
    except Exception:
        print("❌ [答题] 解析答题选项失败")
        return "解析选项失败"
    if not options:
        print("⚠️ [答题] 未获取到题目选项，跳过答题")
        return "无选项"

    selected = random.choice(options)
    selected_key = selected.get("key")
    print(f"📝 [答题] 准备随机提交答案{selected_key}: {selected.get('value')}")

    wait_time = 6 + random.random() * 2
    print(f"⏳ [答题] 提交前等待 {wait_time:.0f}s")
    sleep(wait_time)

    submit_resp = api_post(
        server,
        QUIZ_SUBMIT_URL,
        token,
        proxies,
        {"quizActivityId": int(today_quiz.get("id")), "userAnswer": selected_key},
    )
    if submit_resp.get("code") != 200:
        print(f"❌ [答题] 提交答题失败: {submit_resp.get('msg') or json_preview(submit_resp, 300)}")
        return "提交失败"

    data = submit_resp.get("data") or {}
    actual_correct_key = data.get("correctAnswer") or (selected_key if data.get("isCorrect") == 1 else "")
    if data.get("isCorrect") == 1:
        msg = f"回答正确，获得{data.get('point', 0)}积分"
        print(f"✅ [答题] {msg}")
    else:
        suffix = f"，正确答案是{actual_correct_key}" if actual_correct_key else ""
        msg = f"答案错误{suffix}，今日未获得积分"
        print(f"⚠️ [答题] {msg}")
    return msg


def run_community_post(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    recommend_resp = api_post(
        server,
        COMMUNITY_RECOMMEND_URL,
        token,
        proxies,
        {"recommendationId": 7, "sort": "personalized", "direction": "desc", "pageNum": 1, "pageSize": 3},
    )
    if recommend_resp.get("code") != 200:
        print(f"⚠️ [发帖] 获取推荐内容失败: {recommend_resp.get('msg') or json_preview(recommend_resp, 300)}")
        return "获取推荐内容失败"

    items = safe_data(recommend_resp).get("list") or []
    if not items:
        print("⚠️ [发帖] 未获取到推荐帖子内容，跳过发帖")
        return "无推荐内容"

    item = random.choice(items)
    content = item.get("content") or ""
    image_urls = item.get("imageUrls") or []
    print(f"🌱 [发帖] 准备发帖，内容: {content[:10]}...")

    wait_time = 6 + random.random() * 2
    print(f"⏳ [发帖] 发帖前等待 {wait_time:.0f}s")
    sleep(wait_time)

    push_resp = api_post(
        server,
        COMMUNITY_PUSH_URL,
        token,
        proxies,
        {
            "postId": None,
            "title": "",
            "content": content,
            "imageUrls": image_urls,
            "topicLabelNames": [],
            "communityTopicActivityId": None,
            "communitPostDraftId": None,
            "freeTrialCommentId": None,
            "productIds": [],
        },
    )
    if push_resp.get("code") != 200:
        print(f"❌ [发帖] 发帖失败: {push_resp.get('msg') or json_preview(push_resp, 300)}")
        return "发帖失败"

    post_id = push_resp.get("data")
    print("✅ [发帖] 发帖成功，准备删帖...")
    if not post_id:
        print("❌ [发帖] 发帖响应中未获取到 postId，取消删帖")
        return "发帖成功，删帖失败（无 postId）"

    delete_resp = api_get(server, f"{BASE_URL}/mall/xhr/community/posts/delete?postId={quote(str(post_id))}", token, proxies)
    if delete_resp.get("code") != 200:
        print(f"❌ [发帖] 删帖失败: {delete_resp.get('msg') or json_preview(delete_resp, 300)}")
        return "发帖成功，删帖失败"

    print("✅ [发帖] 帖子删除成功")
    return "发帖并删帖成功"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "point": "-",
        "trialMsg": "-",
        "winningMsg": "-",
        "quizMsg": "-",
        "postMsg": "-",
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
        # 1. 签到（签到并返回等级/手机号/积分）
        checkin_resp = check_checkin(server, token, proxies)
        if checkin_resp.get("code") != 200:
            result["signMsg"] = checkin_resp.get("msg") or "签到失败"
            print(f"❌ [签到] {result['signMsg']}")
            result["error"] = f"签到失败: {result['signMsg']}"
            return result

        checkin_data = safe_data(checkin_resp)
        grade = checkin_data.get("grade")
        phone = mask_phone(str(checkin_data.get("phone") or ""))
        point = checkin_data.get("point", 0)
        result["point"] = str(point)
        result["signMsg"] = "签到成功"
        print(f"✅ [签到] {result['signMsg']}（手机 {phone}，等级 {grade}，当前积分 {point}）")

        # 辅助拉一次签到规则（不影响结果）
        api_get(server, CHECKIN_RULE_URL, token, proxies)

        # 2. 试用商品申请
        trial_msg_parts: List[str] = []
        address_resp = api_post(server, ADDRESS_LIST_URL, token, proxies, {})
        if address_resp.get("code") != 200:
            result["trialMsg"] = "获取收货地址失败"
            print(f"⚠️ [试用] 获取收货地址失败: {address_resp.get('msg') or json_preview(address_resp, 300)}")
        else:
            addresses = address_resp.get("data") if isinstance(address_resp.get("data"), list) else []
            if not addresses:
                result["trialMsg"] = "未填写收货地址，跳过试用申请"
                print("⚠️ [试用] 需要先在小程序 我的-收货地址 中填写地址")
            else:
                address_id = int(addresses[0].get("id"))
                city_name = str(addresses[0].get("cityName") or "")

                trial_resp = api_post(
                    server,
                    TRIAL_LIST_URL,
                    token,
                    proxies,
                    {"pageNum": 1, "pageSize": 10, "statusList": [1, 2]},
                )
                if trial_resp.get("code") != 200:
                    result["trialMsg"] = "获取试用商品列表失败"
                    print(f"⚠️ [试用] 获取试用商品列表失败: {trial_resp.get('msg') or json_preview(trial_resp, 300)}")
                else:
                    trial_list = safe_data(trial_resp).get("list") or []
                    for item in trial_list:
                        if item.get("freeTrialButton") != 3:
                            continue
                        grade_list = [str(x) for x in (item.get("gradeList") or [])]
                        if grade is None or str(grade) not in grade_list:
                            continue
                        trial_id = int(item.get("id"))
                        product_name = str(item.get("productName") or "")
                        draw_time = str(item.get("drawTime") or "")
                        print(f"🎁 [试用] 【{product_name}】可申请试用，开奖时间 {draw_time}")
                        apply_resp = api_post(server, TRIAL_APPLY_URL, token, proxies, {"id": trial_id, "addressId": address_id})
                        if apply_resp.get("code") == 200:
                            msg = f"{product_name} 申请成功（收货地址 {city_name}）"
                            print(f"✅ [试用] {msg}")
                        else:
                            msg = f"{product_name} 申请失败: {apply_resp.get('msg') or '未知错误'}"
                            print(f"❌ [试用] {msg}")
                        trial_msg_parts.append(msg)

                    result["trialMsg"] = "；".join(trial_msg_parts) if trial_msg_parts else "无符合条件可申请的试用商品"
                    print(f"🎁 [试用] {result['trialMsg']}")

        # 3. 中奖记录查询
        winning_resp = api_post(
            server,
            WINNING_LIST_URL,
            token,
            proxies,
            {"parentTabStatus": 1, "pageNum": 1, "pageSize": 10, "subTabStatus": 1},
        )
        if winning_resp.get("code") != 200:
            result["winningMsg"] = "查询中奖记录失败"
            print(f"⚠️ [中奖] 查询中奖记录失败: {winning_resp.get('msg') or json_preview(winning_resp, 300)}")
        else:
            records = safe_data(winning_resp).get("list") or []
            if not records:
                result["winningMsg"] = "暂无中奖记录"
                print("⚠️ [中奖] 暂无中奖记录")
            else:
                record_names = [str(item.get("productName") or "") for item in records]
                result["winningMsg"] = "、".join(name for name in record_names if name)
                for item in records:
                    print(f"🎉 [中奖] 恭喜中奖【{item.get('productName') or ''}】，完成试用后需要提交试用报告，否则会取消下次中奖资格")

        # 4. 社区每日答题
        try:
            result["quizMsg"] = run_quiz(server, token, proxies)
        except Exception as exc:
            result["quizMsg"] = f"答题异常: {exc}"
            print(f"❌ [答题] 社区答题异常: {exc}")
            result["error"] = traceback.format_exc().strip()

        # 5. 社区发帖种草后删帖
        try:
            result["postMsg"] = run_community_post(server, token, proxies)
        except Exception as exc:
            result["postMsg"] = f"发帖异常: {exc}"
            print(f"❌ [发帖] 发帖种草异常: {exc}")
            result["error"] = traceback.format_exc().strip()

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🥛 人本（认养一头牛）任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
📝 签到：{res["signMsg"]}
💰 积分：{res["point"]}
🎁 试用：{res["trialMsg"]}
🎉 中奖：{res["winningMsg"]}
📝 答题：{res["quizMsg"]}
🌱 发帖：{res["postMsg"]}
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
                "signMsg": "-",
                "point": "-",
                "trialMsg": "-",
                "winningMsg": "-",
                "quizMsg": "-",
                "postMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 人本（认养一头牛）任务执行完成            ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🥛 人本（认养一头牛）任务完成", build_notify(results))


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
