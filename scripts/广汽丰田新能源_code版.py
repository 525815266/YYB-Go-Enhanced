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


"""广汽丰田新能源动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. 小程序 code 登录换 xcxToken
  3. OAuth 换网关 token（随机 AES-128-CBC + RSA-PKCS1v1.5 包裹密钥，响应也加密需解密）
  4. 每日签到（先查签到本再签，含连续天数）
  5. PushPlus 推送
  6. 品赞代理，业务请求优先代理，失败直连兜底
  依赖：pip install pycryptodome

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests pycryptodome
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

import base64
import hashlib
import json as _json
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad
from datetime import datetime, timezone

APP_NAME = "广汽丰田新能源小程序"
APPID = "wxd8a42d1c0c59c15d"

SERVERS = [
    "192.168.31.179:8088",
    "192.168.31.36:8088",
    "192.168.31.88:8088",
    "192.168.31.62:8088",
]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

LOG_NAME = "广汽丰田新能源"
LOG_ICON = "🚗"

API_VERSION = "1.4.0"
PAGE_VERSION = "138"
GW_BASE = "https://gw.nevapp.gtmc.com.cn"
XCX_BASE = "https://xcx.nevapp.gtmc.com.cn/wxapp/nev-prod/bff-nev-wxapp"
APP_ID = "ecb4fdd3-da09-408a-913b-44d311d03105"
APP_SIG_SECRET = "611ac848-be11-404e-b7a3-54f735d2eb3e"
BASIC_AUTH = "Basic bmV2YXBwOnNlY3JldA=="
OAUTH_USERNAME = os.getenv("GAC_OAUTH_USER", "18825160040")

PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA49jxpFBAoEslNYrHb0wT
8nCpGBn3hvjgToNkp7lFpsSeRS7WbHoFJEvmf1U83cHrbTzRFRowPft/FGBw6/6d
ZcmMjMgz1n0FWlqk0d7QjEDL+t9Dj9tH9e/qdGfJ3bzR0ZgpgQMpKpx5I5fcEgzM
YnHWGLZBY+v+PlPTN/1mz0nnRtIIxb8YuZZFvadfGTC8jeD7tMERpd5zENml5cLb
VujENsag9AIpvLdvR6fSewi3l9QmssWpty50UpcAWsvAs+ExRYyUe/s1lwfSdSci
W6Lrj4sp4MMaWifdTQUbKKEeuRugEqJSDrxhxoybEbSbl2CYaTR8kifZ1n+lcAh6
cQIDAQAB
-----END PUBLIC KEY-----"""

PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCUEPwXFgsGTngq
ifX48k/5CRBNVA2/mLJhl+fP7Z0UHrSQmI31rtXcb9zN6PMG0jvNxk0oLvrUgf1K
/lfgDp0noUQpCbHqkCk0CGQogSIVr/ktu5lhev0/P+9pkFfXrrZWKYhBk/z7r/XY
vmsm4TVyFhge5WZqfY+HXhFmzJEu9lhq9VACXsfXJ6O778Dj3fF6hHsyNsai+qGN
L31bdObxJG8EhWNcwK0ejCa8XzsscasbjZ/AhTwAQf9kxT9diCZv2vWvK5QtDhxM
bqyQ6lFE8Ew9jaAHYnp2jxh3CwcAMp9B0+Ne4JOBaY7IjH9ENqMC29cYnhxNhj3Z
GcbEu6lpAgMBAAECggEBAISKY66iu8GscmLZ1kY/Whk55M7jw97TaDJ2UTrOn8KH
7ehVtxXKqIPH2qaztQBRJtl/fkfPLhcWOU9tN+pICqOT9zipBgtLeqaqMEYVuhYh
zPMEMDuTZai9qakcXZWjPnMIgID7YQVHsNGROse15yq13mehv7jpppZtPTSBQCEB
ZAw+SFNS4KVfBDKNntlesEuLJHGWWXnqxWwK3YA4IdUAJjT5kDEiYQs7uy2FHqdc
Znw7hV/Tt3OWDqrOB8zoZVhEg9dLvqpBaUi6yh9ihUYJBtFegmsFSY7MazHQjYnY
8bcEcoma22c3AZbGeRwTwrNrlL0/UvF60L1njx4xhSUCgYEA0Xgh4mFSrp5E0UbM
vy5TnpayH1hcaJNFjyGgQGdwgnE69gzR1Grqv+ihSjTbPvQHu9IGnuXb6Pdm/tuj
2ml4xTJ9OnTe2/x/TzMIserNfRD1v6prxjNgZc+YDEebxHTWDBtCNpdbOEy27yO4
fc9UvIoIbgG5eDTcMwCtiIt+98sCgYEAtPUPBqegfiDzyBP7l2hxhGwFgIrsFYIg
3lJwwlyYpZEt8p/TMwPAMb2k+nfQPtyS6T2bBGr2PAKUAubD1SrwGE4ndXO4SDB8
14ll93ZrE7X18iyoGBwbgpjGMONK3nbS2z+2WrFEtQZaUuLiiZp+hnxk5uW7EQ5R
nToOaUTPtRsCgYBNUOhA5Odd6LFCBb4BOxpGSR1KEJVbTDC6mhDKdOPEYgL/WtAA
dc5cM4OFHmlmnTBVlTo4YGOBZAAyReP+9DtNnks2zniL/nEHTLEC6sYaSa5Lpp3N
NJ16NtvKfIv0QaPYKB+Sgt96smY7cpXgaiy+wrxFzoEk623zrWZgJg0hbQKBgFMk
EO5O0CeDPl6cB8lt/FIKS5Dew0+yhSWAnTw/zQatKH5EPoY+3+w6pPVLXUu0jm9J
ldK2zkGOMbEPk8R6QOv55JlLPM02MfXZtBa5usLIpKLLL8Q8Dcu4I79MfxatY33G
zSLoNZgyvgc9JTZx3FYwCzAnNwbEHG1vwjVNn10nAoGASPxDtahASVh/IN6sjFR1
soU8fuzEzThpnchfNVp3BeROR/8fXyfyBk3hKGmh6PY41XttKrGBwCaztCwA6zoT
v7/SmzqNCzknq4uFbr9o455T0+0gtBKS6vFv1zCnvyMXjcmyCvB7gIRnhoq5W/z9
l8VtAagNi9JOhZpjCl7Ep70=
-----END PRIVATE KEY-----"""

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gacccookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF"
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


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ {LOG_ICON} {LOG_NAME:<44}║")
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

        code = str(data.get("code") or "")
        status = data.get("status")
        if data.get("err") != 0 or not code or code == "null" or code == "invalid" or (status is not None and status != "ok"):
            print(f"❌ [授权] code 服务返回无效 code (status={status}, codeType={data.get('codeType')})——该账号微信会话可能已失效，请重新扫码登录 code 服务")
            return None

        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


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



_RSA_PUBLIC = RSA.import_key(PUBLIC_KEY_PEM)
_RSA_PRIVATE = RSA.import_key(PRIVATE_KEY_PEM)


def rand_str(length: int = 6) -> str:
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(chars) for _ in range(length))


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def aes_encrypt(obj: Any) -> Dict[str, str]:
    """随机 key/iv 的 AES-128-CBC，key@DS@iv 用 RSA 公钥包裹"""
    key = rand_str(16)
    iv = rand_str(16)
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    plain = obj if isinstance(obj, str) else _json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    encrypted = cipher.encrypt(pad(plain.encode("utf-8"), 16))
    wrapped = PKCS1_v1_5.new(_RSA_PUBLIC).encrypt(f"{key}@DS@{iv}".encode("utf-8"))
    return {"encryptKey": base64.b64encode(wrapped).decode("utf-8"), "encryptData": base64.b64encode(encrypted).decode("utf-8")}


def aes_decrypt(enc_data: str, enc_key: str) -> str:
    keyiv = PKCS1_v1_5.new(_RSA_PRIVATE).decrypt(base64.b64decode(enc_key), sentinel=None)
    if keyiv is None:
        raise RuntimeError("RSA解密失败")
    key, _, iv = keyiv.decode("utf-8").partition("@DS@")
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    decrypted = cipher.decrypt(base64.b64decode(enc_data))
    return unpad(decrypted, 16).decode("utf-8")


def parse_jwt(token: str) -> Dict[str, Any]:
    try:
        raw = str(token or "").strip()
        payload_b64 = raw.split(".")[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        return _json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        return {}


class AccountSession:
    """单个 code 服务地址的会话: xcxToken + gwToken"""

    def __init__(self, server: str):
        self.server = server
        self.xcx_token = ""
        self.gw_token = ""
        self.gac_open_id = ""
        self.device_id = md5_hex(server)[:16]
        self.unregistered = False

    def json_request(self, method: str, url: str, headers: Dict[str, str],
                     data: Any = None, proxies: Dict[str, str] | None = None) -> Any:
        kwargs: Dict[str, Any] = {"headers": headers, "timeout": REQUEST_TIMEOUT}
        if proxies:
            kwargs["proxies"] = proxies
        if data is not None:
            kwargs["data"] = data if isinstance(data, str) else _json.dumps(data, ensure_ascii=False)
        try:
            response = requests.request(method, url, **kwargs)
        except Exception:
            if not proxies or not ENABLE_DIRECT_FALLBACK:
                raise
            kwargs.pop("proxies", None)
            response = requests.request(method, url, **kwargs)
        try:
            return response.json()
        except Exception:
            return {"header": {"code": -1, "message": f"JSON解析失败/HTTP {response.status_code}: {response.text[:200]}"}}

    def set_cached_token(self) -> None:
        cache = load_token_cache()
        cache[self.server] = {"xcxToken": self.xcx_token, "gwToken": self.gw_token,
                              "gacOpenId": self.gac_open_id, "updateTime": datetime.now().isoformat()}
        save_token_cache(cache)

    def xcx_login(self, code: str, proxies: Dict[str, str] | None) -> None:
        url = f"{XCX_BASE}/auth/login?code={quote(code)}&clickUrl={quote('/pages/index/index')}&clickId="
        data = self.json_request("POST", url, {
            "User-Agent": USER_AGENT, "content-type": "application/json",
            "apiVersion": API_VERSION, "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
        }, data={"code": code, "clickUrl": "/pages/index/index", "clickId": ""}, proxies=proxies)
        header = data.get("header") or {}
        if header.get("code") != 10000000:
            msg = header.get("message") or json_preview(data, 200)
            if __import__("re").search(r"隐私政策|隐私协议|同意.*协议|未注册|未绑定|未实名|完善|注册会员|绑定手机", str(msg)):
                self.unregistered = True
                raise RuntimeError(f"NO_ACCOUNT:{msg}")
            raise RuntimeError(f"小程序登录失败: {msg}")
        body = data.get("body") or {}
        self.xcx_token = str(body.get("token") or "")
        self.gac_open_id = str(body.get("openId") or "")
        if not self.xcx_token:
            raise RuntimeError(f"小程序登录返回无token: {json_preview(body, 200)}")
        print(f"✅ [登录] 小程序登录成功 openId={self.gac_open_id or '?'} tokenLen={len(self.xcx_token)}")

    def exchange_gw(self, proxies: Dict[str, str] | None) -> None:
        timestamp = int(time.time() * 1000)
        nonce = rand_str(6)
        sig = md5_hex(f"{timestamp}{BASIC_AUTH}{nonce}{APP_ID}{APP_SIG_SECRET}")
        body = aes_encrypt({"grant_type": "password", "username": OAUTH_USERNAME,
                            "password": self.xcx_token, "auth_type": "newminipg"})
        data = self.json_request("POST", f"{GW_BASE}/ha/iam/api/sec/oauth/token", {
            "User-Agent": USER_AGENT, "content-type": "application/json", "Authorization": BASIC_AUTH,
            "appId": APP_ID, "timestamp": str(timestamp), "xweb_xhr": "1", "nonce": nonce,
            "sig": sig, "deviceId": self.device_id, "operateSystem": "h5", "appVersion": "",
            "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
        }, data=body, proxies=proxies)
        plain = data
        if isinstance(data, dict) and data.get("encryptData") and data.get("encryptKey"):
            try:
                plain = _json.loads(aes_decrypt(data["encryptData"], data["encryptKey"]))
            except Exception as exc:
                raise RuntimeError(f"网关token响应解密失败: {exc} raw={json_preview(data, 200)}")
        header = plain.get("header") or {}
        code = header.get("code")
        msg = header.get("message") or header.get("msg") or ""
        if code != 10000000 or not (plain.get("body") or {}).get("accessToken"):
            if (__import__("re").search(r"未注册|未绑定|不存在|未实名|no.?account|not.?found|会员|注册", str(msg), __import__("re").IGNORECASE)
                    or code == 10000404 or code == 10001002):
                self.unregistered = True
                raise RuntimeError(f"NO_ACCOUNT:网关侧无账号({code} {msg})")
            raise RuntimeError(f"换取网关token失败: code={code} msg={msg} {json_preview(plain, 200)}")
        self.gw_token = plain["body"]["accessToken"]
        print(f"✅ [登录] 网关token成功 tokenLen={len(str(self.gw_token))}")

    def gw_req(self, method: str, api_path: str, query: Dict[str, Any] | None = None,
               body: Dict[str, Any] | None = None, proxies: Dict[str, str] | None = None) -> Dict[str, Any]:
        qs = f"?{quote('&'.join(f'{k}={v}' for k, v in query.items()), safe='=')}" if query else ""
        ts = int(time.time() * 1000)
        nonce = rand_str(6)
        raw_token = __import__("re").sub(r"^Bearer\s+", "", str(self.gw_token), flags=__import__("re").IGNORECASE).strip()
        sig = md5_hex(f"{ts}{raw_token}{nonce}{APP_ID}{APP_SIG_SECRET}")
        headers = {
            "content-type": "application/json", "appId": APP_ID, "Authorization": self.gw_token,
            "timestamp": str(ts), "xweb_xhr": "1", "sig": sig, "nonce": nonce, "appVersion": "3.22",
            "operateSystem": "h5", "deviceId": self.device_id, "User-Agent": USER_AGENT,
            "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
        }
        send = aes_encrypt(body) if (method != "GET" and body is not None and len(body)) else {}
        data = self.json_request(method, f"{GW_BASE}{api_path}{qs}", headers,
                                 data=send if send else None, proxies=proxies)
        if isinstance(data, dict) and data.get("encryptData") and data.get("encryptKey"):
            data = _json.loads(aes_decrypt(data["encryptData"], data["encryptKey"]))
        return data if isinstance(data, dict) else {}

    def ensure_login(self, proxies: Dict[str, str] | None) -> None:
        cached = load_token_cache().get(self.server) or {}
        cached_gw = cached.get("gwToken") or ""
        exp = (parse_jwt(cached_gw) or {}).get("exp") or 0
        if cached_gw and exp > int(time.time()) + 60:
            self.gw_token = cached_gw
            try:
                self.attendance(proxies)
                print("✅ [缓存] 使用缓存token")
                return
            except Exception as exc:
                print(f"⚠️ [缓存] 缓存token失效，重新登录（{exc}）")
                self.gw_token = ""
        code = get_code(self.server)
        if not code:
            raise RuntimeError("code 服务未返回 code")
        self.xcx_login(code, proxies)
        self.exchange_gw(proxies)
        self.set_cached_token()
        print("✅ [登录] 登录成功")

    def attendance(self, proxies: Dict[str, str] | None) -> Dict[str, Any]:
        """本周一~周日范围（原脚本用 ISO 日期，UTC+8）"""
        now_cn = datetime.fromtimestamp(time.time() + 8 * 3600, tz=timezone.utc)
        weekday = (now_cn.weekday() + 1) % 7  # JS getUTCDay: 0=周日
        from datetime import timedelta
        start = now_cn - timedelta(days=weekday)
        end = start + timedelta(days=6)
        fmt = lambda x: x.strftime("%Y-%m-%d")
        d = self.gw_req("GET", "/main/api/marketing/lgn/sec/usersign/getAttendanceBook",
                        query={"beginTime": fmt(start), "endTime": fmt(end), "noLoad": "true"},
                        proxies=proxies)
        header = d.get("header") or {}
        if header.get("code") != 10000000:
            raise RuntimeError(f"查签到本失败: {header.get('code')} {header.get('message') or json_preview(d, 150)}")
        return d.get("body") or {}

    def sign(self, proxies: Dict[str, str] | None) -> str:
        book = None
        try:
            book = self.attendance(proxies)
        except Exception as exc:
            print(f"⚠️ [签到] 查签到本异常，直接尝试签到（{exc}）")
        if book and book.get("todayHasSigned"):
            extra = f"，已连续 {book.get('continuousDays')} 天" if book.get("continuousDays") else ""
            msg = f"今日已签到{extra}"
            print(f"✅ [签到] {msg}")
            return msg

        r = self.gw_req("POST", "/main/api/marketing/lgn/task/sec/signinV2",
                        query={"noLoad": "true", "noTip": "true"},
                        body={"gtmcUid": "", "fromApplication": "0"}, proxies=proxies)
        header = r.get("header") or {}
        if header.get("code") == 10000000:
            body = r.get("body") or {}
            pt = body.get("point") or body.get("integral") or body.get("score")
            msg = f"签到成功{f'，+{pt}' if pt is not None else ''}"
            print(f"✅ [签到] {msg}")
            return msg
        msg = header.get("message") or header.get("msg") or json_preview(r, 200)
        if __import__("re").search(r"已签|签到过|重复|已完成|repeat", str(msg), __import__("re").IGNORECASE):
            print(f"✅ [签到] 今日已签到（{msg}）")
            return f"今日已签到（{msg}）"
        print(f"❌ [签到] 签到失败: {msg}")
        return f"签到失败: {msg}"


EMPTY_RESULT = {
    "server": "-",
    "success": False,
    "proxyStatus": "未使用代理",
    "proxyIp": "-",
    "token": "-",
    "signMsg": "-",
    "error": "",
}


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = dict(EMPTY_RESULT)
    result["server"] = server

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    session = AccountSession(server)

    try:
        session.ensure_login(proxies)
        result["token"] = mask(session.gw_token)
        result["signMsg"] = session.sign(proxies)
        result["success"] = "签到成功" in result["signMsg"] or "已签到" in result["signMsg"]
        return result

    except RuntimeError as exc:
        if str(exc).startswith("NO_ACCOUNT"):
            cause = str(exc).replace("NO_ACCOUNT:", "")
            if __import__("re").search(r"隐私", cause):
                result["error"] = f"该微信号需先在广汽小程序里同意隐私政策/授权一次（{cause}），这是一次性用户操作，之后再跑即可"
            else:
                result["error"] = f"该微信号还没在广汽注册/绑定会员（{cause}），先在小程序里登录一次再跑"
            print(f"⚠️ [账号] {result['error']}")
            return result
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result
def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🚗 广汽丰田新能源四账号任务结果

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
            results.append({**EMPTY_RESULT, "server": server, "error": traceback.format_exc().strip()})

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ 🏁 广汽丰田新能源任务执行完成          ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus(f"{LOG_ICON} {LOG_NAME}四账号任务完成", build_notify(results))


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
