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
霖久智服小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /base/uniapp/uaa/member/mp/auth/quick 使用 code 换账号凭证（推断接口）
  3. 获取 memberId（/mc/member/autoMember）
  4. 获取任务列表（/mt/mini/task/list）
  5. 单任务（签到/浏览快递小程序/饿了么半屏广告，/mt/web/action/add，AES+RSA 加密）
  6. 视频广告任务（循环 AD 直到失败）
  7. 查询可用积分（/mc/member/memberPoint）
  8. PushPlus 推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

⚠️ 登录接口为推断，未经真机验证，失败请抓包核对

说明：源脚本为抓包多头型（wqwl_ljzf 填 accoutId#authToken#手机号#openId#sessionKey#备注），
      无登录调用。这里按最佳推断实现 code 登录；同时保留环境变量 LJZF
      手动填抓包 CK（5 段 # 分隔，第 6 段为可选备注），多账号换行分隔。
      /mt/web/action/add 请求体 AES-256-CBC 加密 + RSA 加密 AES 密钥，
      加密逻辑照源脚本 encryptRequestData 原样移植。

环境变量：
  LJZF              手动抓包 CK（accoutId#authToken#手机号#openId#sessionKey#备注，
                    多个换行分隔），填了则跳过 code 登录
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       本地 code 服务地址，默认 127.0.0.1:8088

依赖：
  pip install requests
  pip install pycryptodome   （AES-256-CBC + RSA-PKCS1 加密）
  socks5 代理需：
  pip install requests[socks]
"""

import base64
import json
import os
import random
import re
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, urlencode

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad


APP_NAME = "霖久智服小程序"
APPID = "wx0a9f159eddb2c5f8"

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

TENANT_ID = "10111"
CLIENT_ID = "64"
BASE_URL = "https://linjiucloud-api.ysservice.com.cn"
LOGIN_URL = f"{BASE_URL}/base/uniapp/uaa/member/mp/auth/quick"  # ⚠️ 推断端点
AUTO_MEMBER_URL = f"{BASE_URL}/mc/member/autoMember"
TASK_LIST_URL = f"{BASE_URL}/mt/mini/task/list"
ACTION_ADD_URL = f"{BASE_URL}/mt/web/action/add"
MEMBER_POINT_URL = f"{BASE_URL}/mc/member/memberPoint"

# 照源脚本 encryptRequestData 的固定 RSA 公钥（应用级常量）
PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAgDjIfkejLVzxwxqP29PA
6ugWJmpXPNK7yFHioPJQRTlvI0Cx++95v/0hWTitPqOaGJp6zDu6QdCuAHF/wXVU
HSQQL7tJUCNhBNqe/0CsAaAq2HlAUHTNKB4mg02JmpWZB/lpGSkbgjuF7HBpBd2W
L2xPpyI7E8SaYBzU7RHXtpVWoxLMsP/OvL1HH8N5oMx+Zz1y+OaDIcFG4WMzN17h
o1V/TT3EgdfTirdtxg9usw8xNj9Q3pkafBQT0lnHdzvUjEmZNoP3MBczjy6iZyor
EoT/GbwnNdB2DqTeJmEdEYJ6YFsvIl/XV7YEdy/Cr7ngNK8793lj031zEFx0eb5+
uQIDAQAB
-----END PUBLIC KEY-----"""

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ljzfcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_4_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50 NetType/WIFI Language/zh_CN"
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
    print("║ ⚡ 霖久智服动态 code 版                      ║")
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


def common_headers(account: Dict[str, Any] | None = None) -> Dict[str, str]:
    account = account or {}
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "xweb_xhr": "1",
        "X-Tenant-Id": TENANT_ID,
        "X-Client-Id": CLIENT_ID,
        "X-Client-Type": "mini_program",
        "X-Project-id": "",
        "Accept": "*/*",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": f"https://servicewechat.com/{APPID}/116/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    if account.get("accountId"):
        headers["X-Account-Id"] = str(account["accountId"])
    if account.get("authToken"):
        headers["X-Auth-Token"] = str(account["authToken"])
    return headers


def extract_account(data: Any) -> Dict[str, Any] | None:
    """从登录响应中提取 accountId/token/openId/sessionKey/mobile 等字段"""
    if not isinstance(data, dict):
        return None

    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    merged: Dict[str, Any] = {}
    merged.update(inner)
    merged.update({k: v for k, v in data.items() if k != "data"})

    account: Dict[str, Any] = {}
    for key, aliases in {
        "accountId": ("accountId", "account_id", "X-Account-Id"),
        "authToken": ("authToken", "auth_token", "token", "accessToken", "access_token"),
        "openId": ("openId", "openid", "open_id"),
        "sessionKey": ("sessionKey", "session_key"),
        "phone": ("mobile", "phone", "phoneNumber"),
    }.items():
        for alias in aliases:
            value = merged.get(alias)
            if value and str(value) != "null":
                account[key] = str(value)
                break

    return account or None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换账号凭证")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json={
                "appId": APPID,
                "code": code,
                "tenantId": TENANT_ID,
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        if data.get("code") != 0:
            print(f"❌ [登录] 接口返回失败: {data.get('message') or json_preview(data)}")
            return None, data

        account = extract_account(data)
        if account and account.get("authToken"):
            print(f"✅ [登录] 凭证获取成功: {mask(account['authToken'])}")
            return account, data

        print(f"❌ [登录] 未识别凭证字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, account: Dict[str, Any], proxies: Dict[str, str] | None, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if params:
        url = f"{url}?{urlencode(params)}"
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(account),
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "message": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, account: Dict[str, Any], proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(account),
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "message": f"JSON解析失败: {response.text[:300]}",
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


def get_cached_token(server: str) -> Dict[str, Any] | None:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("authToken") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} 凭证")
                return data
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, account: Dict[str, Any], expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {**account, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def remove_cached_token(server: str) -> None:
    cache = load_token_cache()
    if cache.get(server):
        del cache[server]
        save_token_cache(cache)


def rand_str(length: int) -> str:
    """照源脚本 randStr：62 位字母数字随机串"""
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(chars) for _ in range(length))


def rand_hex(length: int) -> str:
    """照源脚本 randHex：16 进制随机串"""
    chars = "0123456789abcdef"
    return "".join(random.choice(chars) for _ in range(length))


def encrypt_request_data(data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """照源脚本 encryptRequestData：AES-256-CBC 加密业务数据 + RSA 加密 AES 密钥

    返回 (envelope, headers)，envelope 作为请求体，headers 附加 X-Nonce/X-Timestamp
    """
    aes_key = rand_str(32)
    iv = rand_str(16)

    aes_key_bytes = aes_key.encode("utf-8")
    iv_bytes = iv.encode("utf-8")

    # AES-256-CBC 加密（PKCS7 填充，与 JS crypto.createCipheriv 默认一致）
    data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    cipher_aes = AES.new(aes_key_bytes, AES.MODE_CBC, iv_bytes)
    encrypted_data = base64.b64encode(
        cipher_aes.encrypt(pad(data_str.encode("utf-8"), AES.block_size))
    ).decode("utf-8")

    # RSA PKCS1 加密 base64 后的 AES 密钥
    aes_key_b64 = base64.b64encode(aes_key_bytes).decode("utf-8")
    rsa_key = RSA.import_key(PUBLIC_KEY_PEM)
    cipher_rsa = PKCS1_v1_5.new(rsa_key)
    encrypted_key = base64.b64encode(cipher_rsa.encrypt(aes_key_b64.encode("utf-8"))).decode("utf-8")

    envelope = {
        "encryptedKey": encrypted_key,
        "encryptedData": encrypted_data,
        "iv": base64.b64encode(iv_bytes).decode("utf-8"),
    }
    headers = {
        "X-Nonce": rand_hex(32),
        "X-Timestamp": str(int(time.time() * 1000)),
    }
    return envelope, headers


def load_manual_accounts() -> List[Dict[str, Any]]:
    """解析环境变量 LJZF：accoutId#authToken#手机号#openId#sessionKey#备注（多账号换行）"""
    raw = os.getenv("LJZF", "").strip()
    if not raw:
        return []

    accounts: List[Dict[str, Any]] = []
    for line in re.split(r"[\n]", raw):
        line = line.strip()
        if not line:
            continue
        parts = line.split("#")
        if len(parts) < 5:
            print(f"⚠️ [配置] CK 段数不足 5，跳过: {line[:30]}...")
            continue
        remark = parts[5].strip() if len(parts) > 5 and parts[5].strip() else f"{parts[0][:8]}"
        accounts.append({
            "accountId": parts[0].strip(),
            "authToken": parts[1].strip(),
            "phone": parts[2].strip(),
            "openId": parts[3].strip(),
            "sessionKey": parts[4].strip(),
            "remark": remark,
        })
    return accounts


def get_member_id(server: str, account: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
    """获取 memberId（/mc/member/autoMember，照源脚本）"""
    resp = api_post(
        server,
        AUTO_MEMBER_URL,
        account,
        proxies,
        {
            "channel": "CHARGE_PLATFORM",
            "mobile": account.get("phone", ""),
            "tenantId": TENANT_ID,
        },
    )
    if resp.get("code") == 0 and resp.get("data"):
        member_id = str(resp.get("data"))
        print(f"✅ [会员] memberId 获取成功: {mask(member_id)}")
        return member_id

    raise RuntimeError(f"获取memberId失败: {resp.get('message') or json_preview(resp, 300)}")


def task_list(server: str, account: Dict[str, Any], member_id: str, proxies: Dict[str, str] | None) -> bool:
    """获取任务列表（/mt/mini/task/list，照源脚本）"""
    resp = api_post(
        server,
        TASK_LIST_URL,
        account,
        proxies,
        {
            "memberId": member_id,
            "tenantId": TENANT_ID,
        },
    )
    if resp.get("code") == 0:
        print("✅ [任务] 任务列表获取成功")
        return True

    print(f"⚠️ [任务] 任务列表获取失败: {resp.get('message') or json_preview(resp, 300)}")
    return False


def do_single_task(
    server: str,
    account: Dict[str, Any],
    member_id: str,
    method_name: str,
    action_type: str,
    proxies: Dict[str, str] | None,
) -> Tuple[int, int]:
    """执行单个任务（/mt/web/action/add，AES+RSA 加密请求体，照源脚本 doSingleTask）"""
    user_name = f"用户{str(account.get('phone', ''))[-4:]}"
    payload = {
        "actionRecordCO": {
            "actionType": action_type,
            "actionUnit": "1",
            "channel": "LJZF",
            "createdBy": member_id,
            "createdName": user_name,
            "unitCount": "1",
        },
        "tenantId": TENANT_ID,
        "appId": APPID,
        "sessionKey": account.get("sessionKey", ""),
        "openId": account.get("openId", ""),
    }
    envelope, extra_headers = encrypt_request_data(payload)
    headers = {**common_headers(account), **extra_headers}

    response = request_with_proxy(
        "POST",
        ACTION_ADD_URL,
        headers=headers,
        json=envelope,
        proxies=proxies,
        server=server,
    )
    try:
        resp = response.json()
    except Exception:
        resp = {"code": -1, "message": f"JSON解析失败: {response.text[:300]}"}

    if resp.get("code") == 0:
        point = (resp.get("data") or {}).get("pointCount") or 0
        level = (resp.get("data") or {}).get("pointLevelCount") or 0
        print(f"✅ [{method_name}] 成功，获得积分{point},成长值：{level}")
        return int(point), int(level)

    print(f"❌ [{method_name}] 失败: {resp.get('message') or json_preview(resp, 300)}")
    return 0, 0


def watch_ad(server: str, account: Dict[str, Any], member_id: str, proxies: Dict[str, str] | None) -> Tuple[int, int]:
    """循环看视频广告（/mt/web/action/add AD，照源脚本 ad()，随机暂停 20-40s）"""
    method_name = "视频广告"
    count = 0
    total_point = 0
    total_level = 0
    # 照源脚本 while(true) 循环到接口失败为止；额外加 30 次上限防死循环
    while count < 30:
        print(f"⏳ [广告] 正在执行第{count + 1}次看{method_name}")
        point, level = do_single_task(server, account, member_id, method_name, "AD", proxies)
        if point == 0 and level == 0:
            print(f"✅ [{method_name}] 完成，共完成{count}次，获得{total_point}积分，获得{total_level}成长值")
            break

        count += 1
        total_point += point
        total_level += level

        wait_time = random.randint(20, 40)
        print(f"🕒 [广告] 随机暂停{wait_time}s")
        sleep(wait_time)

    return total_point, total_level


def get_info(server: str, account: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
    """获取个人信息可用积分（/mc/member/memberPoint，照源脚本 getInfo）"""
    resp = api_get(
        server,
        MEMBER_POINT_URL,
        account,
        proxies,
        {
            "mobile": account.get("phone", ""),
            "tenantId": TENANT_ID,
        },
    )
    if resp.get("code") == 0:
        points = (resp.get("data") or {}).get("availablePoints") or 0
        print(f"✅ [积分] 当前可用积分：{points}")
        return str(points)

    print(f"⚠️ [积分] 获取失败: {resp.get('message') or json_preview(resp, 300)}")
    return "-"


def account_valid(server: str, account: Dict[str, Any], proxies: Dict[str, str] | None) -> bool:
    """用 memberPoint 只读接口验证凭证是否有效"""
    resp = api_get(
        server,
        MEMBER_POINT_URL,
        account,
        proxies,
        {
            "mobile": account.get("phone", ""),
            "tenantId": TENANT_ID,
        },
    )
    return resp.get("code") == 0


def login_with_cache(server: str, proxies: Dict[str, str] | None, manual_account: Dict[str, Any] | None = None) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    """优先使用缓存凭证（memberPoint 接口验证），失效自动 code 刷新；手动 CK 直接使用"""
    if manual_account:
        return dict(manual_account), None

    cached = get_cached_token(server)
    if cached:
        print("🔍 [缓存] 验证凭证")
        try:
            if account_valid(server, cached, proxies):
                print("✅ [缓存] 凭证有效")
                return cached, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] 凭证已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None

    account, raw_login = login_by_code(server, code, proxies)
    if not account:
        return None, raw_login

    if not account.get("sessionKey"):
        account["sessionKey"] = ""
    if not account.get("phone"):
        print("⚠️ [登录] 登录响应未包含手机号，积分查询可能失败（建议用 LJZF 手动填 CK）")

    set_cached_token(server, account, datetime.fromtimestamp(time.time() + 20 * 3600).isoformat())
    return account, raw_login


def run_account(index: int, total: int, server: str, manual_account: Dict[str, Any] | None = None) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "adMsg": "-",
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

    account, raw_login = login_with_cache(server, proxies, manual_account)
    if not account:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(account.get("authToken", ""))

    try:
        # 1. 获取 memberId
        member_id = get_member_id(server, account, proxies)

        # 2. 获取任务列表
        task_list(server, account, member_id, proxies)
        sleep(1)

        # 3. 单任务（照源脚本：签到/浏览快递小程序/饿了么半屏广告）
        tasks = [
            ("SIGN_IN", "签到"),
            ("express", "浏览快递小程序"),
            ("ELE_HALF_SCREEN_INTERSTITIAL", "饿了么拉起半屏广告"),
        ]
        total_point = 0
        total_level = 0
        for action_type, task_name in tasks:
            print(f"🔍 [任务] 正在执行{task_name}")
            point, level = do_single_task(server, account, member_id, task_name, action_type, proxies)
            total_point += point
            total_level += level
            wait_time = random.randint(2, 5)
            sleep(wait_time)

        result["signMsg"] = f"任务积分 {total_point}，成长值 {total_level}"

        # 4. 视频广告
        sleep(1)
        ad_point, ad_level = watch_ad(server, account, member_id, proxies)
        result["adMsg"] = f"广告积分 {ad_point}，成长值 {ad_level}"

        # 5. 查询可用积分
        sleep(1)
        result["points"] = get_info(server, account, proxies)

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""⚡ 霖久智服任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
📝 任务：{res["signMsg"]}
📺 广告：{res["adMsg"]}
⭐ 积分：{res["points"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    manual_accounts = load_manual_accounts()
    if manual_accounts:
        print(f"🔑 [配置] 检测到手动 CK {len(manual_accounts)} 个，跳过 code 登录")

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            manual = manual_accounts[index - 1] if index - 1 < len(manual_accounts) else None
            result = run_account(index, len(SERVERS), server, manual)
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
                "adMsg": "-",
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
    print("║ 🏁 霖久智服任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("⚡ 霖久智服任务完成", build_notify(results))


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
