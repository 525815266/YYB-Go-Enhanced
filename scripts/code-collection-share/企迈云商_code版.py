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
企迈云商 qmai 平台签到动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /account-center/oauth/mini-app-login 使用 code 换 token（AES-GCM 加密请求体）
  3. 读取签到状态（userSignStatistics）
  4. 每日签到（takePartInSign）
  5. Token 本地缓存与自动刷新
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

说明：
  企迈平台对 webapi.qmai.cn 全站强制 AES-256-GCM 请求体加密：
  - key = 宽松 base64 解码固定字符串后取前 32 字节
  - 明文 = JSON.stringify(body)，body 自动补 appid
  - 12 字节随机 iv，请求体 = {"payload": base64(ciphertext‖tag)}
  - 头 QM-Encrypt-Meta = base64(JSON{version, timestamp, iv})，响应同样加密
  源脚本用 wx_server 按 openid 取码，本 code 版改由本地 code 服务按 appId 取码，
  账号变量相应变为 storeId#activityId。

环境变量：
  PLUSPLUS_TOKEN  PushPlus token，可选
  QYWX_TOKEN      企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API       品赞代理提取 API，可选
  PROXY_TYPE      http / socks5，默认 http
  QMAI_ACCOUNTS   账号列表，格式 店铺storeId#签到activityId[#备注]，
                  一行一个店铺，多行或 & 分隔
                  （activityId 从签到页 webapi.qmai.cn/web/cmk-center/sign/* 请求体抓取）

依赖：
  pip install requests
  pip install pycryptodome   （AES-256-GCM 加解密）
  socks5 代理需：
  pip install requests[socks]
"""

import base64
import json
import os
import random
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from Crypto.Cipher import AES


APP_NAME = "企迈云商平台签到小程序"
APPID = "wx26c7aaacfa017719"

SERVERS = ["127.0.0.1:8088"]

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

BASE_URL = "https://webapi.qmai.cn/web"
LOGIN_URL = f"{BASE_URL}/account-center/oauth/mini-app-login"
SIGN_STATS_URL = f"{BASE_URL}/cmk-center/sign/userSignStatistics"
SIGN_TAKE_URL = f"{BASE_URL}/cmk-center/sign/takePartInSign"

# —— 企迈全站 AES-GCM 加密固定参数（源自解包 requestEncryptSdk）——
KEY_RAW = "mN6KpXq8Sv2WxYz9LdFcRgHjMnBvCtDxZaS3QwE5rT0yU7I4O1A"
KEY_VERSION = "1.0.0"
META_HEADER = "QM-Encrypt-Meta"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qmaicookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; M2012K11AC Build/SKQ1.220303.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/134.0.6998.136 Mobile Safari/537.36 MicroMessenger/8.0.48.2580(0x28003036) MiniProgramEnv/android"
)


def parse_accounts() -> List[Dict[str, Any]]:
    """解析 QMAI_ACCOUNTS：storeId#activityId[#备注]，一行一个或 & 分隔"""
    raw = os.getenv("QMAI_ACCOUNTS", "")
    accounts: List[Dict[str, Any]] = []
    for line in raw.replace("&", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [item.strip() for item in line.split("#")]
        store_id = parts[0] if len(parts) > 0 else ""
        activity_id = parts[1] if len(parts) > 1 else ""
        remark = parts[2] if len(parts) > 2 else ""
        accounts.append({"storeId": store_id, "activityId": activity_id, "remark": remark})
    return accounts


ACCOUNTS = parse_accounts()


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
    print("║ 🍵 企迈云商平台签到动态 code 版               ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(ACCOUNTS):<34}║")
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


def derive_key(raw: str) -> bytes:
    """解包 M()：宽松 base64 解码，非 32 字节则取前 32 补零"""
    try:
        cleaned = raw
        if cleaned.lower().startswith("key-"):
            cleaned = cleaned[4:]
        b = base64.b64decode(cleaned + "=" * ((4 - len(cleaned) % 4) % 4))
    except Exception:
        b = raw.encode("utf-8")
    out = bytearray(32)
    out[: min(len(b), 32)] = b[:32]
    return bytes(out)


KEY = derive_key(KEY_RAW)


def b64relax(value: str) -> bytes:
    return base64.b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def gcm_encrypt(plaintext: str, iv: bytes) -> str:
    """AES-256-GCM：返回 base64(ciphertext‖16字节tag)"""
    cipher = AES.new(KEY, AES.MODE_GCM, nonce=iv)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return base64.b64encode(ciphertext + tag).decode("utf-8")


def gcm_decrypt(payload_b64: str, iv: bytes) -> str:
    buf = base64.b64decode(payload_b64)
    tag = buf[-16:]
    data = buf[:-16]
    cipher = AES.new(KEY, AES.MODE_GCM, nonce=iv)
    cipher.update(b"")
    plain = cipher.decrypt_and_verify(data, tag)
    return plain.decode("utf-8")


def common_headers(store_id: str, token: str | None = None, appid: str = "") -> Dict[str, str]:
    headers = {
        "Accept": "v=1.0",
        "store-id": store_id,
        "Qm-From-Type": "catering",
        "Qm-From": "wechat",
        "Content-Type": "application/json",
        "Qm-User-Token": token or "",
        "Accept-Language": "zh",
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{appid or APPID}/0/page-frame.html",
        "xweb_xhr": "1",
    }
    return headers


def encrypted_post(
    server: str,
    url: str,
    account: Dict[str, Any],
    body: Dict[str, Any],
    token: str,
    proxies: Dict[str, str] | None,
) -> Dict[str, Any]:
    """AES-GCM 加密请求 + 解密响应（企迈全站契约）"""
    if not body.get("appid"):
        body["appid"] = account.get("appid") or APPID  # 解包 j() 自动补 appid

    iv = os.urandom(12)
    ts = int(time.time() * 1000)
    cipher = gcm_encrypt(json.dumps(body, ensure_ascii=False, separators=(",", ":")), iv)
    meta = base64.b64encode(
        json.dumps({"version": KEY_VERSION, "timestamp": ts, "iv": base64.b64encode(iv).decode("utf-8")}).encode("utf-8")
    ).decode("utf-8")

    headers = common_headers(account.get("storeId", ""), token, account.get("appid") or APPID)
    headers[META_HEADER] = meta

    response = request_with_proxy(
        "POST",
        url,
        headers=headers,
        json={"payload": cipher},
        proxies=proxies,
        server=server,
    )

    try:
        data = response.json()
    except Exception:
        return {"status": False, "message": f"HTTP {response.status_code}: {response.text[:300]}"}

    if response.status_code != 200:
        if isinstance(data, dict):
            return data
        return {"status": False, "message": f"HTTP {response.status_code}: {json_preview(data, 300)}"}

    # 响应可能是明文（错误信封）或加密 {payload}
    if isinstance(data, dict) and isinstance(data.get("payload"), str):
        rmeta = response.headers.get(META_HEADER)
        if not rmeta:
            return {"status": False, "message": f"响应加密但缺 {META_HEADER}"}
        try:
            mj = json.loads(b64relax(rmeta).decode("utf-8"))
            riv = b64relax(mj.get("iv", ""))
            return json.loads(gcm_decrypt(data["payload"], riv))
        except Exception as exc:
            return {"status": False, "message": f"响应解密失败: {exc}"}

    return data


def is_ok(resp: Dict[str, Any]) -> bool:
    return bool(resp) and resp.get("status") is True and int(resp.get("code") or 0) == 0


def msg_of(resp: Dict[str, Any]) -> str:
    return resp.get("message") or resp.get("msg") or json_preview(resp, 200)


def is_already_done(text: Any) -> bool:
    import re

    return bool(re.search(r"已签|已经签|签到过|重复|已完成|already", str(text or ""), re.I))


def is_auth_error(resp: Dict[str, Any]) -> bool:
    import re

    try:
        code = int(resp.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 401:
        return True
    return bool(re.search(r"token|登录|未授权|失效|过期", msg_of(resp), re.I))


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

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(
    server: str, code: str, account: Dict[str, Any], proxies: Dict[str, str] | None
) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        data = encrypted_post(
            server,
            LOGIN_URL,
            account,
            {"code": code, "eVersion": "1.0"},
            "",
            proxies,
        )

        token = (safe_data(data) or {}).get("token") or extract_token(data)
        if is_ok(data) and token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return str(token), data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, account: Dict[str, Any]) -> Dict[str, Any]:
    """企迈业务接口全部走加密 POST，本应用无纯 GET 业务接口"""
    return encrypted_post(server, url, account, {}, token, proxies)


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any], account: Dict[str, Any]) -> Dict[str, Any]:
    return encrypted_post(server, url, account, payload, token, proxies)


# ====================== Token缓存管理 ======================
def account_key(account: Dict[str, Any]) -> str:
    return f"{account.get('appid') or APPID}#{account.get('storeId', '')}#{account.get('activityId', '')}"


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
    del server  # 缓存按账号（appid#storeId#activityId）为键
    cache = load_token_cache()
    data = cache.get(CURRENT_KEY, {})
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print("✅ [缓存] 使用缓存 token")
                return data["token"]
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, token: str, expire_time: str) -> None:
    del server  # 缓存按账号（appid#storeId#activityId）为键
    cache = load_token_cache()
    cache[CURRENT_KEY] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


CURRENT_KEY = ""


def login_with_cache(
    server: str, account: Dict[str, Any], proxies: Dict[str, str] | None
) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（签到状态接口验证），失效自动 code 刷新"""
    global CURRENT_KEY
    CURRENT_KEY = account_key(account)

    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            stats_resp = api_post(server, SIGN_STATS_URL, cache_token, proxies, {"activityId": account.get("activityId", "")}, account)
            if is_ok(stats_resp) or not is_auth_error(stats_resp):
                if is_ok(stats_resp):
                    print("✅ [缓存] token 有效")
                    return cache_token, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None

    token, raw_login = login_by_code(server, code, account, proxies)
    if not token:
        return None, raw_login

    # 企迈 token 未下发过期时间，默认 7 天，实际以签到状态接口验证为准
    expire_time = datetime.fromtimestamp(time.time() + 7 * 24 * 3600).isoformat()
    set_cached_token(server, token, expire_time)
    return token, raw_login


def run_account(index: int, total: int, server: str, account: Dict[str, Any]) -> Dict[str, Any]:
    tag = account.get("remark") or account.get("storeId") or "-"
    result = {
        "server": server,
        "tag": tag,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "signStatus": "-",
        "error": "",
    }

    log_account_header(index, total, f"{server}（{tag}）")

    if not account.get("storeId") or not account.get("activityId"):
        result["error"] = "变量值要写成 storeId#activityId[#备注]（activityId 从签到页抓一次）"
        print(f"❌ [账号] {result['error']}")
        return result

    proxies, proxy_ip = get_valid_proxy(tag)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    token, raw_login = login_with_cache(server, account, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)
    member = (safe_data(raw_login) or {}).get("user") or {}
    member_id = member.get("memberId") or member.get("id")
    if member_id:
        print(f"✅ [登录] 会员 {str(member_id)[:8]}…")

    try:
        # 签到状态
        stats_resp = api_post(server, SIGN_STATS_URL, token, proxies, {"activityId": account.get("activityId", "")}, account)
        signed = False
        if is_ok(stats_resp):
            d = safe_data(stats_resp)
            result["signStatus"] = json_preview(d, 140)
            print(f"📋 [签到] 状态: {result['signStatus']}")
            signed = d.get("todaySign") is True or d.get("isSign") is True or int(d.get("todaySignStatus") or 0) == 1
        else:
            print(f"⚠️ [签到] 读取签到状态失败: {msg_of(stats_resp)}")

        if signed:
            result["signMsg"] = "今日已签到"
            print("✅ [签到] 今日已签到")
        else:
            sign_resp = api_post(server, SIGN_TAKE_URL, token, proxies, {"activityId": account.get("activityId", "")}, account)
            if is_ok(sign_resp):
                d = safe_data(sign_resp)
                gain = d.get("point")
                if gain is None:
                    gain = d.get("integral")
                if gain is None:
                    gain = d.get("reward")
                result["signMsg"] = f"签到成功{f'，+{gain}' if gain is not None else ''}"
                print(f"✅ [签到] {result['signMsg']}")
            elif is_already_done(msg_of(sign_resp)):
                result["signMsg"] = f"今日已签到（{msg_of(sign_resp)}）"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                result["signMsg"] = f"签到失败: {msg_of(sign_resp)}"
                print(f"❌ [签到] {result['signMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍵 企迈云商平台签到任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}（{res["tag"]}）
🌐 代理：{res["proxyStatus"]}（{res["proxyIp"]}）
📋 状态：{res["signStatus"]}
📝 签到：{res["signMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    if not ACCOUNTS:
        print("❌ [主程序] 未配置 QMAI_ACCOUNTS（格式：storeId#activityId[#备注]）")
        return

    results: List[Dict[str, Any]] = []

    for index, account in enumerate(ACCOUNTS, 1):
        server = SERVERS[(index - 1) % len(SERVERS)]
        try:
            result = run_account(index, len(ACCOUNTS), server, account)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {account.get('storeId') or server} 执行异常: {exc}")
            results.append({
                "server": server,
                "tag": account.get("remark") or account.get("storeId") or "-",
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "signStatus": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(ACCOUNTS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 企迈云商任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🍵 企迈云商平台签到任务完成", build_notify(results))


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
