#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 兼容 GBK 终端：强制 stdout/stderr 使用 UTF-8（不影响排版与格式）
import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ========== 企业微信推送配置（可选） ==========
QYWX_TOKEN = __import__("os").getenv("QYWX_TOKEN", "")

# ==========================================================
# 功能说明：伊利QQ星 code 版（对齐铛铛一下.py 的 code 接口）
# 机制：code 接口获取微信 code -> 换取 jifen_auth_key（含 AES 密钥，签到/抽奖主链路）
#       与 mall auth_key（积分任务，可选）-> 缓存到本地 JSON；
#       下次运行先读取缓存凭据并验证有效性；
#       有效则直接复用（无需再获取 code），失效则重新获取 code 自动刷新。
# 注意：mall 侧登录依赖微信粉丝记录，缺失时自动降级仅跑 jifen 签到+抽奖，
#       并进入 6 小时退避避免每次白耗 code。
# ==========================================================

# 伊利QQ星签到 / 积分 / 抽奖 code 版
#
# 功能：
#   1. code 接口获取微信 code（对齐铛铛一下.py）
#   2. 使用 code 换 auth_key（mall）与 jifen_auth_key（积分商城，含 RSA/AES）
#   3. 每日签到 / 积分任务
#   4. 抽奖页签到 + 抽奖
#   5. 查询积分
#   6. 品赞代理，业务请求优先代理，失败直连兜底
#   7. PushPlus + 企业微信机器人 推送
#
# 环境变量：
#   PLUSPLUS_TOKEN    PushPlus token，可选
#   QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
#   PROXY_API         品赞代理提取 API，可选
#   PROXY_TYPE        http / socks5，默认 http
#
# 依赖：
#   pip install requests cryptography
#   socks5 代理需：pip install requests[socks]

import json
import os
import re
import random
import time
import base64
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from xml.etree import ElementTree as ET
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.backends import default_backend


APP_NAME = "伊利QQ星"
APPID = "wx650bdff059f63f5b"
SECRET = "d1e4b452117fa4ff4af6fa319fd858ff"
DEVICE_CODE = "0723B870-04B8-4EAD-BCAA-0E700A409ECB"

# code 接口服务（对齐铛铛一下.py 的 SERVERS，测试用 10.30.9.183:8088）
SERVERS = [
    "10.30.9.183:8088",
]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30
CACHE_DIR = os.environ.get("CODE_CACHE_DIR", os.path.join(os.path.expanduser("~"), "Documents", "写代码"))

os.makedirs(CACHE_DIR, exist_ok=True)

COOKIE_FILE = os.path.join(CACHE_DIR, "ylqqxcookie.json")
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
    print("║ 🥛 伊利QQ星 code 版                          ║")
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
        "Referer": f"https://servicewechat.com/{APPID}/162/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["token"] = token
    return headers


# ========== RSA / AES 加解密（积分商城 jifen 登录用） ==========
RSA_PRIVATE_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIICdgIBADANBgkqhkiG9w0BAQEFAASCAmAwggJcAgEAAoGBAIuV/5NI+/L75F2mSL1n2T731tc4+1fu9tDLhua4FyxwjIlI2N0gMSqpdZfDvStlicnvbxfeXE2ZGFhjnBhQNhxk3prlIXGDF8C/03WvKc7M8pnqPCRq7hm9R10xDJD67od/0f3lSRJX4NfH8PB6stYfu85YQMA4GldkW0M0wjjrAgMBAAECgYAQpAg9AdVviVIXTAyd7/R5SkiljdiBCi8Ig0sI1GeG18AZWcLo0b6qzFsFhcNWmhtMJKxE1zB/28GIQA/K4j0hBMV7me4x+VbfUmV4AH3TrgxmpPWCVNf7hIcIYrP6hG+5VsYiW6CmocyApLWL2ECLU8Dup6rkSwL9Ys++Sq+osQJBAPaNJyJ68Yo+O0B9CoyN9JiSgAEGTondClCKLHLEjCZCwe6vkIbDygVld48qHexQyc6/ZYQSyo0xqsy8wlYb74MCQQCQ72/px5BWZsDtC2/jNl7pHAYTaGI8mrKuOFPYHycOuWjjfhGysinNakpTxQCQ7Q4LDhb1vyWKsNP7v109Nqx5AkEArgX/o3TH3F4EkIYx1fe0t6RgOVjsQp8EUsjUisV0buUb4Y+GIbk8dQajlyeRK2Xyq72ot8pTsclm11A8k27wZQJAZoVYLpABo3xvv721WY2eOVqfWZ8ezivHdMFXXas7n4i7jyAgOL0aILms9fCGY/2rT1qaFx8s2RwX9x34QFKqUQJAR2H0otX4VsaAGcFfFPWo/Bs16AWdwALn2hOY9Vltbq2HykSDqezomzy9zWYVdArOMrq/uYfgt43ipcB356diZw==\n"
    "-----END PRIVATE KEY-----"
)

RSA_MODULUS = "i5X/k0j78vvkXaZIvWfZPvfW1zj7V+720MuG5rgXLHCMiUjY3SAxKql1l8O9K2WJye9vF95cTZkYWGOcGFA2HGTemuUhcYMXwL/Tda8pzszymeo8JGruGb1HXTEMkPruh3/R/eVJElfg18fw8Hqy1h+7zlhAwDgaV2RbQzTCOOs="
RSA_EXPONENT = "AQAB"


def aes_encrypt(plaintext: str, key: str, iv: str) -> str:
    key_b = key.encode("utf-8")
    iv_b = iv.encode("utf-8")
    padder = sym_padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key_b), modes.CBC(iv_b), backend=default_backend())
    encryptor = cipher.encryptor()
    ct = encryptor.update(data) + encryptor.finalize()
    return base64.b64encode(ct).decode()


def aes_decrypt(ciphertext_b64: str, key: str, iv: str) -> str:
    key_b = key.encode("utf-8")
    iv_b = iv.encode("utf-8")
    ct = base64.b64decode(ciphertext_b64)
    cipher = Cipher(algorithms.AES(key_b), modes.CBC(iv_b), backend=default_backend())
    decryptor = cipher.decryptor()
    pt = decryptor.update(ct) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    data = unpadder.update(pt) + unpadder.finalize()
    return data.decode("utf-8")


def rsa_decrypt(b64_data: str) -> bytes:
    priv = serialization.load_pem_private_key(RSA_PRIVATE_PEM.encode(), password=None, backend=default_backend())
    return priv.decrypt(base64.b64decode(b64_data), asym_padding.PKCS1v15())


# ====================== Token 缓存管理（按 code 接口 host 缓存） ======================
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
    if data and (data.get("auth_key") or data.get("jifen_auth_key")):
        return data
    return None

class YiLiQQStar:
    TASKS = {
        11: "发起分享",
        31: "单次签到",
        40: "分享文章",
        47: "使用工具",
        53: "知识库每日打卡",
        55: "分享小程序",
        56: "关注公众号",
        62: "活动签到",
        75: "活动连续签到",
    }

    def __init__(self, server: str, proxies: Dict[str, str] | None = None, nickname: str = ""):
        self.server = server
        self.proxies = proxies
        self.nickname = nickname or server
        self.base_url = "https://mall.yili.com/MAMAIF/MCSWSIAPI.asmx/Call"
        self.jifen_url = "https://jifen.yilibabyclub.com/mclubif2/SmallProgram.asmx/Call"
        self.jifen_api = "https://jifen.yilibabyclub.com/MCSAppletsAPI/MCSWSIAPI.asmx/Call"
        self.device_code = APPID
        self.activity_id = "13D88C0D-A850-4278-A718-35CD397EF922"

        self.auth_key = None
        self.jifen_auth_key = None
        self.user_id = None
        self.crm_id = None
        self.open_id = None
        self.union_id = None
        self.aes_key = None
        self.aes_iv = None
        self.guid = None
        self.mall_ok = False
        self.points_before = 0
        self.last_result = {"signMsg": "-", "lotteryMsg": "-", "balance": "-"}

        self.headers = {
            'User-Agent': USER_AGENT,
            'Content-Type': 'application/x-www-form-urlencoded',
            'xweb_xhr': '1',
            'Referer': f'https://servicewechat.com/{APPID}/162/page-frame.html',
        }
        self.jifen_headers = {
            'User-Agent': USER_AGENT,
            'Content-Type': 'application/json;charset=utf-8',
            'Referer': f'https://servicewechat.com/{APPID}/162/page-frame.html',
        }
        self._load_from_cache()

    def _load_from_cache(self):
        data = get_cached_token(self.server)
        if data:
            self.auth_key = data.get("auth_key")
            self.jifen_auth_key = data.get("jifen_auth_key")
            self.aes_key = data.get("aes_key")
            self.aes_iv = data.get("aes_iv")
            self.open_id = data.get("open_id")
            self.union_id = data.get("union_id")
            self.crm_id = data.get("crm_id")
            self.guid = data.get("guid")

    def _save_token(self):
        cache = load_token_cache()
        cache[self.server] = {
            "auth_key": self.auth_key,
            "jifen_auth_key": self.jifen_auth_key,
            "aes_key": self.aes_key,
            "aes_iv": self.aes_iv,
            "open_id": self.open_id,
            "union_id": self.union_id,
            "crm_id": self.crm_id,
            "guid": self.guid,
            "expireTime": datetime.fromtimestamp(time.time() + 24 * 3600).isoformat(),
            "updateTime": datetime.now().isoformat(),
        }
        save_token_cache(cache)

    def _parse(self, resp):
        text = resp.text.strip()
        if not text:
            return {}
        if text.startswith('<?xml') or text.startswith('<string'):
            try:
                root = ET.fromstring(text)
                if root.text:
                    return json.loads(root.text)
            except Exception:
                m = re.search(r'<string[^>]*>(.*?)</string>', text, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group(1))
                    except Exception:
                        pass
        try:
            return json.loads(text)
        except Exception:
            return {}

    def call(self, method, params, retry=2):
        if isinstance(params, dict):
            p = json.dumps(params)
        elif isinstance(params, str) and params:
            p = params
        else:
            p = ""
        for i in range(retry):
            try:
                r = request_with_proxy(
                    "POST", self.base_url, proxies=self.proxies, server=self.server,
                    headers=self.headers,
                    data={'RequestPack': json.dumps({
                        "DeviceCode": self.device_code,
                        "AuthKey": self.auth_key or "0" * 36,
                        "Method": method, "Params": p
                    })},
                    timeout=15,
                )
                result = self._parse(r)
                if 'Result' in result and isinstance(result['Result'], str):
                    try:
                        result['Result'] = json.loads(result['Result'])
                    except Exception:
                        pass
                return result
            except Exception:
                if i < retry - 1:
                    time.sleep(3)
                else:
                    return {"Return": -999}

    def jifen_call(self, method, params=None, need_encrypt=True, auth=None):
        if params is None:
            params = {}
        if isinstance(params, dict):
            plain = json.dumps(params, separators=(',', ':'), ensure_ascii=False)
        else:
            plain = params or "{}"
        if need_encrypt and self.aes_key and self.aes_iv:
            params_json = aes_encrypt(plain, self.aes_key, self.aes_iv)
        else:
            params_json = plain
        # auth=None 时沿用当前 AuthKey；匿名接口必须显式传空串，
        # 否则会带过期 AuthKey 被服务端以 -100 authkey invalid 拒绝，导致无法重新登录
        auth_key = self.jifen_auth_key if auth is None else auth
        body = {
            "RequestPack": json.dumps({
                "Method": method,
                "AuthKey": auth_key or "",
                "ParamsJson": params_json,
                "DeviceCode": DEVICE_CODE
            }, separators=(',', ':'))
        }
        try:
            url = self.jifen_url if "StampActivityService" in method else self.jifen_api
            r = request_with_proxy("POST", url, proxies=self.proxies, server=self.server,
                                   headers=self.jifen_headers, json=body, timeout=15)
            data = r.json()
            d = data.get("d")
            if isinstance(d, str):
                d = json.loads(d)
            if d.get("Return", -1) >= 0 and d.get("Result") and self.aes_key:
                try:
                    if isinstance(d["Result"], str) and not d["Result"].startswith("{"):
                        d["Result"] = aes_decrypt(d["Result"], self.aes_key, self.aes_iv)
                except Exception:
                    pass
            return d
        except Exception as e:
            print(f"⚠️ jifen_call 异常: {e}")
            return {"Return": -999}

    def apply_aes_key(self):
        body = {
            "RequestPack": json.dumps({
                "Method": "LoginService.ApplyAESEncryptKey",
                "AuthKey": "",
                "ParamsJson": json.dumps({"Modulus": RSA_MODULUS, "Exponent": RSA_EXPONENT}),
                "DeviceCode": DEVICE_CODE
            })
        }
        try:
            r = request_with_proxy("POST", self.jifen_api, proxies=self.proxies, server=self.server,
                                   headers=self.jifen_headers, json=body, timeout=15)
            data = r.json()
            d = json.loads(data.get("d", "{}"))
            if d.get("Return") == 0:
                result = json.loads(d["Result"])
                self.aes_key = rsa_decrypt(result["CryptAESKey"]).decode()
                self.aes_iv = rsa_decrypt(result["CryptAESIV"]).decode()
                return True
        except Exception as e:
            print(f"❌ ApplyAESEncryptKey 失败: {e}")
        return False

    def login(self):
        code = get_code(self.server)
        if not code:
            print("❌ 获取微信code失败")
            return False
        r1 = self.call("WechatService.GetWxOpenID", json.dumps({
            "AppID": APPID, "Secret": SECRET, "Js_Code": code, "Grant_Type": "authorization_code"
        }))
        if r1.get('Return', -1) < 0:
            print("❌ 获取OpenID失败")
            return False
        result = r1.get('Result', {})
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                pass
        self.open_id = result.get('openid', '') or self.open_id
        if not self.open_id:
            print("❌ OpenID为空")
            return False
        last_err = ""
        for method in ("MemberService.LoginByWechatOpenId", "MemberService.LoginByWechatOpenID"):
            r2 = self.call(method, json.dumps({
                "Platform": APPID, "OpenId": self.open_id, "UnionId": result.get('unionid', '') or self.union_id or ""
            }))
            ret = r2.get('Return', -1)
            if ret >= 0:
                self.auth_key = (r2.get('Result', {}) or {}).get('AuthKey', '')
                if self.auth_key:
                    self._save_token()
                return bool(self.auth_key)
            last_err = r2.get('ReturnInfo') or ("Return:%s" % ret)
            print("⚠️  %s 返回 %s -> 尝试下一个登录方法" % (method, last_err))
            time.sleep(1)
        print("❌ 登录失败: " + str(last_err))
        return False

    def jifen_login(self):
        if not self.apply_aes_key():
            return False

        r = self.jifen_call("LoginService.GetAnonymityAuthkey", {"username": "anonymity"}, need_encrypt=True, auth="")
        if r.get("Return", -1) < 0:
            print("❌ GetAnonymityAuthkey 失败")
            return False
        try:
            self.jifen_auth_key = json.loads(r["Result"]).get("Authkey")
        except Exception:
            print("❌ 解析临时 AuthKey 失败")
            return False

        code = get_code(self.server)
        if not code:
            print("❌ 获取微信code失败")
            return False
        r = self.jifen_call("LoginService.GetWxOpenID", {
            "appid": APPID, "secret": SECRET, "js_code": code, "grant_type": "authorization_code"
        }, need_encrypt=True)
        if r.get("Return", -1) < 0:
            print("❌ GetWxOpenID 失败")
            return False
        try:
            res = json.loads(r["Result"]) if isinstance(r["Result"], str) else r["Result"]
            self.open_id = res.get("openid")
            self.union_id = res.get("unionid")
            session_key = res.get("session_key", "")
        except Exception:
            print("❌ 解析 OpenID 失败")
            return False

        # 3. CheckRegister → 注册检查并取会员 GUID（mall 未登录时 GUID 由此获取）
        guid = ""
        r = self.jifen_call("LoginService.CheckRegister", {
            "Unionid": self.union_id or "",
            "Mobile": "",
            "session_key": session_key
        }, need_encrypt=True)
        if r.get("Return", -1) >= 0:
            res = r.get("Result")
            if isinstance(res, str) and re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", res.strip()):
                guid = res.strip()
        if not guid and self.auth_key:
            info = self.get_info()
            if info:
                guid = info.get("CRMMemebrID") or info.get("BabyClubID") or ""
        if guid:
            self.guid = guid
            print(f"✅ [注册] 会员 GUID: {mask(guid)}")
        else:
            print("⚠️ [注册] 未获取到会员 GUID")

        # 4. UserLoginByGUID → 正式 AuthKey
        r = self.jifen_call("LoginService.UserLoginByGUID", {
            "GUID": self.guid or self.union_id or "",
            "Sequence": "6666",
            "UnionID": self.union_id or "",
            "session_key": session_key
        }, need_encrypt=True)
        if r.get("Return", -1) >= 0:
            try:
                self.jifen_auth_key = json.loads(r["Result"]).get("Authkey")
            except Exception:
                pass
        else:
            print(f"⚠️ [登录] UserLoginByGUID 失败: {r.get('Return')} {r.get('ReturnInfo')}")

        self._save_token()
        return bool(self.aes_key and self.open_id and self.jifen_auth_key)

    def get_info(self):
        r = self.call("MemberService.GetMyMemberInfo", "")
        if r.get('Return') == 0:
            info = r['Result']
            self.user_id = info.get('ID')
            self.crm_id = info.get('BabyCRMID') or info.get('OwnerClient')
            self.points_before = float(info.get('PointsBalance', 0))
            return info
        return None

    def get_points(self):
        r = self.call("PointsService.GetPointsBalance", "")
        return r.get('Result', {}) if r.get('Return') == 0 else None

    def do_join(self, jt):
        if not self.user_id:
            return None
        ji = json.dumps({"Activity": self.activity_id, "JoinType": jt, "UserId": self.user_id})
        return self.call("MemberService.CampaignJoin", json.dumps({"JoinInfo": ji}))

    def get_lottery_count(self):
        r = self.jifen_call("StampActivityService.GetQQActivityAward_MemberCount", {})
        ret = r.get("Return", -1)
        if ret >= 0:
            txt = str(r.get("Result", ""))
            m = re.search(r'(\d+)', txt)
            if m:
                return int(m.group(1))
            return int(ret)
        return 0

    def do_lottery(self):
        r = self.jifen_call("StampActivityService.QQActivity_Award", {})
        if r.get("Return") == 0 or r.get("Return") > 0:
            try:
                res = json.loads(r["Result"]) if isinstance(r["Result"], str) else r["Result"]
                name = res.get('AwardName', '') if isinstance(res, dict) else ''
                print(f"  🎉 抽中: {name or r['Result']}")
                return str(name or r.get('Result'))[:60]
            except Exception:
                print(f"  🎉 抽奖成功: {r.get('Result')}")
                return str(r.get('Result'))[:60]
        print(f"  ❌ 抽奖失败: {r.get('Return')} {r.get('ReturnInfo')}")
        return None

    def do_lottery_signin(self):
        print("  执行抽奖签到...")
        r = self.jifen_call("StampActivityService.QQActivityTaskadd", {
            "UnionID": self.union_id or "",
            "OpenID": self.open_id or "",
            "JoinClassify": 4
        })
        ret = r.get("Return", -999) if r else -999
        info = r.get("ReturnInfo") or r.get("Result") or ""
        if ret > 0 or ret == 0:
            print(f"  ✅ 抽奖签到成功 (Return:{ret})")
            return True
        if ret == -1702 or "今天的签到" in str(info):
            print(f"  ⏭️  今天已签到")
            return True
        print(f"  ❌ 抽奖签到失败: {ret} {info}")
        return False

    def run_daily_tasks(self):
        for jt, name in self.TASKS.items():
            r = self.do_join(jt)
            ret = r.get('Return', -999) if r else -999
            if ret >= 0:
                print(f"✅ [{jt}] {name} 完成!")
            elif ret in [-31, -33, -30]:
                print(f"⏭️  [{jt}] {name} 已完成")
            elif ret == -10:
                print(f"🔄 [{jt}] 刷新AuthKey...")
                if self.login():
                    r = self.do_join(jt)
                    print(f"  {'✅ 完成' if r and r.get('Return', -1) >= 0 else '❌ 失败'}")
            elif ret == -999:
                print(f"⚠️  [{jt}] {name} 网络错误")
            else:
                print(f"❌ [{jt}] {name}: {ret}")
            time.sleep(0.8)

    def run_lottery_section(self):
        print("  --- 抽奖板块 ---")
        sign_ok = self.do_lottery_signin()
        self.last_result["signMsg"] = "抽奖签到成功" if sign_ok else "抽奖签到失败"
        time.sleep(1)
        cnt = self.get_lottery_count()
        print(f"  当前抽奖次数: {cnt}")
        prizes = []
        while cnt > 0:
            print(f"  执行抽奖 (剩余 {cnt})...")
            name = self.do_lottery()
            if name:
                prizes.append(name)
                cnt -= 1
            else:
                break
            time.sleep(1.5)
            cnt = self.get_lottery_count()
        if prizes:
            self.last_result["lotteryMsg"] = "、".join(prizes)
        elif cnt <= 0:
            self.last_result["lotteryMsg"] = "无抽奖机会"

    def jifen_check(self):
        """验证缓存的 jifen_auth_key 是否仍有效（抽奖次数接口轻量验证）"""
        if not (self.jifen_auth_key and self.aes_key and self.aes_iv):
            return False
        r = self.jifen_call("StampActivityService.GetQQActivityAward_MemberCount", {})
        return r.get("Return", -1) >= 0

    def _mall_in_backoff(self):
        cache = load_token_cache()
        data = cache.get(self.server) or {}
        until = data.get("mallFailUntil")
        try:
            return bool(until) and datetime.fromisoformat(until).timestamp() > time.time()
        except Exception:
            return False

    def _mark_mall_fail(self):
        cache = load_token_cache()
        data = cache.get(self.server) or {}
        data["mallFailUntil"] = datetime.fromtimestamp(time.time() + 6 * 3600).isoformat()
        cache[self.server] = data
        save_token_cache(cache)

    def ensure_login(self):
        print("[登录] 使用 code 接口获取 token ...")

        # jifen 侧（签到/抽奖主链路）
        if self.jifen_check():
            print("✅ [缓存] jifen token 有效")
        elif self.jifen_login():
            print("✅ jifen 登录成功")
        else:
            print("⚠️ jifen 登录失败，抽奖板块不可用")

        # mall 侧（积分任务，缺微信粉丝记录的账号可能登录失败）
        if self.auth_key and self.get_info():
            self.mall_ok = True
            print("✅ [缓存] mall token 有效")
        elif self._mall_in_backoff():
            print("⏭️  [缓存] mall 近期登录失败，本次跳过积分任务")
        else:
            self.mall_ok = bool(self.login() and self.get_info())
            if self.mall_ok:
                print("✅ mall 登录成功")
            else:
                print("⚠️ mall 登录失败，跳过积分任务（6小时内不再重试）")
                self._mark_mall_fail()

        return self.mall_ok or self.jifen_check()

    def run_tasks(self):
        if self.mall_ok:
            info = self.get_info() or {}
            print(f"👤 {info.get('RealName')} | {info.get('MemberLevelName')} | {self.points_before}积分")

            self.run_daily_tasks()

            pts = self.get_points()
            if pts:
                a = float(pts.get('Points', self.points_before))
                d = a - self.points_before
                if d > 0:
                    print(f"\n🎉 积分: {self.points_before} → {a} (+{d})")
                else:
                    print(f"\n📊 积分: {self.points_before}")
                self.last_result["balance"] = str(pts.get('Points', a))
            else:
                self.last_result["balance"] = str(self.points_before)
        else:
            print("⚠️ mall 未登录，跳过积分任务")

        if self.jifen_auth_key and self.aes_key:
            self.run_lottery_section()

    def run(self):
        if not self.ensure_login():
            print("❌ 登录失败")
            return False
        self.run_tasks()
        return True

def run_account(index, total, server):
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "lotteryMsg": "-",
        "balance": "-",
        "withdrawMsg": "-",
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

    inst = YiLiQQStar(server, proxies)
    try:
        ok = inst.run()
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result

    if not ok:
        result["error"] = "登录失败"
        return result

    result["token"] = mask(inst.auth_key or inst.jifen_auth_key or "")

    res = inst.last_result
    result["signMsg"] = res.get("signMsg", "-")
    result["lotteryMsg"] = res.get("lotteryMsg", "-")
    result["balance"] = res.get("balance", "-")
    result["success"] = True
    return result


def build_notify(results):
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🥛 伊利QQ星 code 版任务结果

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
🎰 抽奖：{res["lotteryMsg"]}
💰 积分：{res["balance"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    results = []

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
                "lotteryMsg": "-",
                "balance": "-",
                "withdrawMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 伊利QQ星任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🥛 伊利QQ星任务完成", build_notify(results))


# YYB_SERVER 多账号适配：必须在 main() 前安装，避免首轮运行使用旧 code 服务。
from yyb_compat import install as _install_yyb
_install_yyb(globals())

if __name__ == "__main__":
    main()
