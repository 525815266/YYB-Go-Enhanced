#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 无忧计划

"""
无忧计划 - 自动任务脚本（基于抓包 + APK 逆向重写）

APP下载注册链接: https://dgccvi.com/#/register?ref=H79MRH7
注册必须填邀请码：H79MRH7

功能：
  1. App Attest 签名会话（逆向自 AppAttestBridge / AppAttestManager）
  2. 账号密码登录
  3. 每日签到 + 领取签到奖励
  4. 每日任务列表展示 + 任务奖励领取
  5. 广告联盟看广告赚金币（心跳节奏对齐真实 App）
  6. device_id 自动持久化，设备数达上限自动回填
  7. 内置 20 个真机 Android WebView UA，默认按账号轮换
  8. PushPlus 推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  WY_ACCOUNT      账号列表：每行一个账号，格式 账号#密码#device_id#备注
                  多账号用换行分隔；device_id、备注可省略，以 # 开头的行视为注释
                  示例:
                  WY_ACCOUNT=13800138000#abc123#dev001#主号
                             13900139000#xyz456
  WY_USER_AGENT   Android WebView UA（可选），多账号可用 & 分隔、按账号顺序对应；
                  不填时自动使用内置 20 个真机 UA 轮换（与 WY_ACCOUNT 账号顺序一一对应）
  WY_MAX_ADS      单次运行最多看几个广告（可选），默认不限制（由账号等级/服务端上限决定）
  PLUSPLUS_TOKEN  PushPlus token，可选
  PROXY_API       品赞代理提取 API，可选
  PROXY_TYPE      http / socks5，默认 http

device_id 说明：
  - 优先级: WY_ACCOUNT 第三段 > 本地 wy_devices.json 持久化 > 空值登录后从服务端回填
  - 新 device_id 触发 device_limit（设备数达上限）时，自动改用空 device_id 登录，
    再从 /api/app/user-devices 回填已绑定设备的 device_id 并持久化，下次直接复用

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import hashlib
import hmac
import json
import os
import random
import re
import secrets
import string
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, urlsplit

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Windows 控制台默认 GBK 编码，无法输出 emoji，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

APP_NAME = "无忧计划"

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

# 内置 20 个真机 Android WebView UA（全部为移动设备 UA，机型 + Build ID 均来自网上真机抓取记录：
# 简书《手机端user-agent大全》https://www.jianshu.com/p/b232b23fedb5
# 简书《237条微信内置浏览器UA，2022年9月最新版本》https://www.jianshu.com/p/df9c10eb4702
# 格式: Mozilla/5.0 (Linux; Android 版本; 机型 Build/编译号; wv) ... Version/4.0 Chrome/版本 Mobile Safari/537.36
BUILTIN_USER_AGENTS = [
    # 华为
    "Mozilla/5.0 (Linux; Android 9; ELE-AL00 Build/HUAWEIELE-AL00; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/66.0.3359.126 Mobile Safari/537.36",  # 华为 P30
    "Mozilla/5.0 (Linux; Android 10; ELE-AL00 Build/HUAWEIELE-AL00; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Mobile Safari/537.36",  # 华为 P30
    "Mozilla/5.0 (Linux; Android 10; EML-AL00 Build/HUAWEIEML-AL00; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 Mobile Safari/537.36",  # 华为 P20
    "Mozilla/5.0 (Linux; Android 10; SEA-AL10 Build/HUAWEISEA-AL10; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 Mobile Safari/537.36",  # 华为 nova 7 SE
    "Mozilla/5.0 (Linux; Android 10; HMA-AL00 Build/HUAWEIHMA-AL00; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/79.0.3945.93 Mobile Safari/537.36",  # 华为 Mate 20
    "Mozilla/5.0 (Linux; Android 10; OXF-AN10 Build/HUAWEIOXF-AN10; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Mobile Safari/537.36",  # 华为 Mate 30 Pro
    # 荣耀
    "Mozilla/5.0 (Linux; Android 10; HLK-AL00 Build/HONORHLK-AL00; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 Mobile Safari/537.36",  # 荣耀 9X
    "Mozilla/5.0 (Linux; Android 9; JSN-AL00a Build/HONORJSN-AL00a; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Mobile Safari/537.36",  # 荣耀 20
    # 小米
    "Mozilla/5.0 (Linux; Android 11; MI 9 Build/RKQ1.200826.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 Mobile Safari/537.36",  # 小米 9
    "Mozilla/5.0 (Linux; Android 10; Mi 10 Build/QKQ1.191117.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Mobile Safari/537.36",  # 小米 10
    "Mozilla/5.0 (Linux; Android 11; M2011K2C Build/RKQ1.200928.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 Mobile Safari/537.36",  # 小米 11
    "Mozilla/5.0 (Linux; Android 10; MI 8 SE Build/QKQ1.190828.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 Mobile Safari/537.36",  # 小米 8 SE
    "Mozilla/5.0 (Linux; Android 9; MIX 2S Build/PKQ1.180729.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Mobile Safari/537.36",  # 小米 MIX 2S
    # 红米
    "Mozilla/5.0 (Linux; Android 11; 21091116C Build/RP1A.200720.011; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 Mobile Safari/537.36",  # 红米 Note 11
    "Mozilla/5.0 (Linux; Android 11; M2004J7BC Build/RP1A.200720.011; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 Mobile Safari/537.36",  # 红米 Note 9
    "Mozilla/5.0 (Linux; Android 9; Redmi Note 8 Pro Build/PPR1.180610.011; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/66.0.3359.126 Mobile Safari/537.36",  # 红米 Note 8 Pro
    # OPPO / vivo / 一加
    "Mozilla/5.0 (Linux; Android 9; PACM00 Build/PPR1.180610.011; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Mobile Safari/537.36",  # OPPO R15
    "Mozilla/5.0 (Linux; Android 8.1.0; OPPO R11s Build/OPM1.171019.011; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Mobile Safari/537.36",  # OPPO R11s
    "Mozilla/5.0 (Linux; Android 9; V1813BT Build/PKQ1.181030.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Mobile Safari/537.36",  # vivo Z3
    "Mozilla/5.0 (Linux; Android 10; GM1910 Build/QKQ1.190716.003; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/78.0.3904.96 Mobile Safari/537.36",  # 一加 7 Pro
]

# ==================== 常量（来自 APK 逆向 assets/public/assets/index-*.js 的 VITE 配置） ====================

API_BASE = "https://api.dgccvi.com/api/app"     # VITE_API_BASE_URL
ADS_BASE = "https://ads.dgccvi.com/api/app"     # VITE_ADS_API_URL
APP_VERSION = "1.0.9"

LOGIN_URL = f"{API_BASE}/auth/login"
ATTEST_URL = f"{API_BASE}/attest"
ME_URL = f"{API_BASE}/me"
DAILY_TASKS_URL = f"{API_BASE}/daily-tasks"
CHECKIN_URL = f"{API_BASE}/checkin"
USER_DEVICES_URL = f"{API_BASE}/user-devices"

ADS_LIST_URL = f"{ADS_BASE}/alliance-ads"
ADS_SESSION_START_URL = f"{ADS_BASE}/alliance-ads/session/start"
ADS_HEARTBEAT_URL = f"{ADS_BASE}/alliance-ads/session/heartbeat"
ADS_COMPLETE_URL = f"{ADS_BASE}/alliance-ads/session/complete"

# 逆向自 AppAttestManager.signAttest 的 HMAC 密钥
ATTEST_KEY = "aac0ab40d0612c8549f88e87e476751a348f910156e9e73590ddaece2a4288d5"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEVICE_STORE = os.path.join(SCRIPT_DIR, "wy_devices.json")


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


def hmac_hex(key: str, msg: str) -> str:
    return hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compact_json(payload) -> bytes:
    """与前端 JSON.stringify 一致的紧凑序列化（不转义中文）"""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_device_store() -> dict:
    try:
        with open(DEVICE_STORE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_device_store(store: dict):
    try:
        with open(DEVICE_STORE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存 device_id 文件失败: {e}")


def gen_device_id() -> str:
    """复刻前端 deviceId 模块: `${Date.now()}-${Math.random().toString(36).slice(2)}`"""
    rand = "".join(random.choice(string.digits + string.ascii_lowercase) for _ in range(10))
    return f"{int(time.time() * 1000)}-{rand}"


def log_title(account_count: int) -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ ♻️ 无忧计划自动任务脚本                         ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {account_count:<34}║")
    print("╚" + "═" * 50 + "╝")


def log_account_header(index: int, total: int, account: str, remark: str = "") -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 📱 手机号 {account:<38}│")
    if remark:
        print(f"│ 🏷️ 备注 {remark:<40}│")
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


def parse_accounts() -> List[Dict[str, str]]:
    # 多账号格式: 每行一个账号，格式 账号#密码#device_id#备注（device_id/备注可省略，
    # 以 # 开头的行视为注释）
    # device_id 无需填写: 空值登录后自动从服务端回填并持久化到 wy_devices.json
    env_accounts = os.environ.get("WY_ACCOUNT", "").strip()

    accounts = []
    for acc_str in env_accounts.replace("\r\n", "\n").split("\n"):
        line = acc_str.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("#")
        if len(parts) >= 2:
            accounts.append({
                "account": parts[0].strip(),
                "password": parts[1].strip(),
                "device_id": parts[2].strip() if len(parts) >= 3 else "",
                "remark": parts[3].strip() if len(parts) >= 4 else "",
            })
    return accounts


def get_user_agents() -> List[str]:
    # 多 UA 用 & 分隔，按账号顺序对应；数量不足时循环取用
    # 不填 WY_USER_AGENT 时使用内置 20 个真机 Android WebView UA
    user_agents = [ua.strip() for ua in os.environ.get("WY_USER_AGENT", "").split("&") if ua.strip()]
    return user_agents or list(BUILTIN_USER_AGENTS)


class AppAttest:
    """App Attest 签名会话（逆向自 AppAttestBridge / AppAttestManager）

    1. POST /api/app/attest {integrity_token:"", device_id, ts, nonce, native_proof}
       native_proof = HMAC-SHA256(ATTEST_KEY, "attest\nts\nnonce\ndevice_id")
    2. 响应 {ok, session_id, session_secret, expires_in}
    3. 后续请求头:
       X-App-Session: session_id
       X-App-Ts:      unix 秒
       X-App-Nonce:   32 位随机 hex
       X-App-Sign:    HMAC-SHA256(session_secret, "METHOD\npath\nts\nnonce\nbody_sha256")
       其中 path 只取 URL 路径部分（不含 query），body_sha256 为请求体(无体则为空串)的 SHA-256 hex
    """

    def __init__(self, http: requests.Session, device_id: str, proxies: Dict[str, str] | None, log):
        self.http = http
        self.device_id = device_id
        self.proxies = proxies
        self.log = log
        self.session_id = None
        self.session_secret = None
        self.expires_at = 0

    def has_session(self) -> bool:
        return bool(self.session_id and self.session_secret and time.time() < self.expires_at)

    def ensure(self, force: bool = False) -> bool:
        """确保签名会话可用；App 端每 15 分钟刷新一次，这里按有效期提前 60 秒刷新"""
        if not force and self.has_session():
            return True
        try:
            ts = str(int(time.time()))
            nonce = secrets.token_hex(16)
            native_proof = hmac_hex(ATTEST_KEY, f"attest\n{ts}\n{nonce}\n{self.device_id}")
            payload = {
                "integrity_token": "",
                "device_id": self.device_id,
                "ts": ts,
                "nonce": nonce,
                "native_proof": native_proof,
            }
            headers = dict(self.http.headers)
            headers["Content-Type"] = "application/json"
            resp = request_with_proxy(
                "POST",
                ATTEST_URL,
                headers=headers,
                data=compact_json(payload),
                proxies=self.proxies,
                server="attest",
                verify=False,
                timeout=15,
            )
            data = resp.json()
            if data.get("ok") and data.get("session_id") and data.get("session_secret"):
                self.session_id = data["session_id"]
                self.session_secret = data["session_secret"]
                self.expires_at = time.time() + int(data.get("expires_in", 1800)) - 60
                return True
            self.log(f"⚠️ [签名] attest 失败: {str(data)[:200]}")
        except Exception as e:
            self.log(f"⚠️ [签名] attest 异常: {e}")
        return False

    def sign_headers(self, method: str, url: str, body: bytes) -> dict:
        """生成四个签名头；无会话时返回空 dict（服务端暂未强制所有接口验签）"""
        if not self.has_session():
            return {}
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        path = urlsplit(url).path or "/"
        body_hash = sha256_hex(body)
        msg = f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{body_hash}"
        sign = hmac_hex(self.session_secret, msg)
        return {
            "X-App-Session": self.session_id,
            "X-App-Ts": ts,
            "X-App-Nonce": nonce,
            "X-App-Sign": sign,
        }

class WuYouPlan:
    def __init__(self, account, password, device_id="", ua="", proxies=None):
        self.account = account
        self.password = password
        self.ua = ua or random.choice(BUILTIN_USER_AGENTS)
        self.proxies = proxies
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://localhost",
            "referer": "https://localhost/",
            "x-requested-with": "com.dgccvi.app",
            "user-agent": self.ua,
            "accept-encoding": "gzip, deflate",
            "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        })
        # device_id: 显式指定 > 本地持久化 > 新生成并持久化
        self.device_id = device_id or self._load_or_create_device_id()
        self.attest = AppAttest(self.session, self.device_id, self.proxies, self.log)
        self.token = None
        self.user_id = None
        self.user_info = None
        self.total_coins_earned = 0
        self.last_error = ""
        self.sign_msg = "-"
        self.ad_msg = "-"

    def log(self, msg):
        print(msg)

    def _load_or_create_device_id(self) -> str:
        store = load_device_store()
        dev = store.get(self.account)
        if dev:
            return dev
        dev = gen_device_id()
        store[self.account] = dev
        save_device_store(store)
        return dev

    # ==================== 带签名的请求封装 ====================

    def _send(self, method: str, url: str, *, headers=None, data=None, params=None, timeout: int = 20):
        request_headers = dict(self.session.headers)
        if headers:
            request_headers.update(headers)
        kwargs = {"headers": request_headers}
        if data is not None:
            kwargs["data"] = data
        if params is not None:
            kwargs["params"] = params
        return request_with_proxy(
            method,
            url,
            proxies=self.proxies,
            server=self.account,
            verify=False,
            timeout=timeout,
            **kwargs,
        )

    def _request(self, method: str, url: str, payload=None, params=None, retry_on_app_required=True):
        body = b"" if payload is None else compact_json(payload)
        headers = dict(self.attest.sign_headers(method, url, body))
        if payload is not None:
            headers["Content-Type"] = "application/json"
        resp = self._send(
            method, url,
            data=body if payload is not None else None,
            params=params, headers=headers,
        )
        # 服务端要求 App 签名时（403 + code=app_required），重新 attest 后重试一次
        if retry_on_app_required and resp.status_code == 403:
            try:
                err = resp.json()
            except Exception:
                err = {}
            if err.get("code") == "app_required":
                self.log("🔁 [签名] 收到 app_required，重新进行 attest 签名...")
                if self.attest.ensure(force=True):
                    return self._request(method, url, payload, params, retry_on_app_required=False)
        return resp

    def _get(self, url, params=None):
        return self._request("GET", url, params=params)

    def _post(self, url, payload=None, params=None):
        return self._request("POST", url, payload=payload if payload is not None else {}, params=params)

    # ==================== 登录 ====================

    def login(self):
        """登录获取 token（携带 device_id + app_version，与抓包一致）

        新 device_id 触发 device_limit 时，改用空 device_id 重试，
        成功后从服务端回填已绑定设备的 device_id 并持久化。
        """
        self.attest.ensure()
        payload = {
            "account": self.account,
            "password": self.password,
            "device_id": self.device_id,
            "platform": "android",
            "app_version": APP_VERSION,
        }
        resp = self._post(LOGIN_URL, payload)
        data = resp.json()
        token = data.get("token")

        # 设备数量达上限: 换空 device_id 重试（不注册新设备）
        used_fallback = False
        if not token and data.get("code") == "device_limit":
            self.log("⚠️ [登录] 设备数量已达上限，尝试用空 device_id 登录（复用已绑定设备）...")
            payload["device_id"] = ""
            resp = self._post(LOGIN_URL, payload)
            data = resp.json()
            token = data.get("token")
            used_fallback = True

        if token:
            self.token = token
            self.user_info = data.get("user", {})
            self.user_id = self.user_info.get("id")
            self.session.headers.update({"authorization": f"Bearer {self.token}"})
            self.log(f"✅ [登录] 登录成功 | 用户ID: {self.user_id} | device_id: {self.device_id or '(待回填)'}")
            # 兜底登录或本地无 device_id 时，从服务端回填已绑定设备
            # （否则广告等接口会拿着一个未绑定的 device_id 请求）
            if used_fallback or not self.device_id:
                if self.sync_device_from_server():
                    self.log(f"📱 [设备] 已回填服务端绑定设备 device_id: {self.device_id}")
                else:
                    self.log("⚠️ [设备] 未能回填 device_id（账号可能未绑定任何设备），广告流程可能受限")
            return True
        else:
            self.last_error = str(data)[:300]
            self.log(f"❌ [登录] 登录失败: {self.last_error}")
            return False

    def sync_device_from_server(self) -> bool:
        """从 /api/app/user-devices 回填当前账号已绑定的 device_id，并持久化 + 用新 device_id 重新 attest"""
        try:
            resp = self._get(USER_DEVICES_URL, params={"device_id": self.device_id})
            data = resp.json()
            devices = data.get("devices", [])
            chosen = next((d for d in devices if d.get("is_current")), None) or (devices[0] if devices else None)
            if chosen and chosen.get("device_id"):
                new_device_id = chosen["device_id"]
                if new_device_id != self.device_id:
                    self.device_id = new_device_id
                    # attest 会话绑定的是旧 device_id，必须用新 device_id 重新签名
                    self.attest.device_id = new_device_id
                    self.attest.session_id = None
                    self.attest.session_secret = None
                    self.attest.ensure(force=True)
                # 持久化，下次直接带上正确 device_id 登录
                store = load_device_store()
                store[self.account] = self.device_id
                save_device_store(store)
                return True
        except Exception as e:
            self.log(f"⚠️ [设备] 回填 device_id 失败: {e}")
        return False

    # ==================== 用户/任务信息 ====================

    def get_user_info(self):
        """查询用户信息（含金币余额），与 App 相同携带 device_id/platform/app_version"""
        resp = self._get(ME_URL, params={
            "device_id": self.device_id,
            "platform": "android",
            "app_version": APP_VERSION,
        })
        return resp.json().get("user", {})

    def get_user_devices(self):
        resp = self._get(USER_DEVICES_URL, params={"device_id": self.device_id})
        data = resp.json()
        devices = data.get("devices", [])
        device_ids = [d.get("device_id", "") for d in devices]
        self.log(f"📱 [设备] 查询设备 | device_id: {device_ids}")
        return data

    def get_daily_tasks(self):
        resp = self._get(DAILY_TASKS_URL)
        return resp.json()

    def show_tasks(self, data) -> str:
        tasks = data.get("tasks", [])
        today = data.get("today", "")
        pending = data.get("pending_claim", 0)
        self.log(f"📋 [任务] 日期: {today} | 待领取: {pending}个")
        print("-" * 60)
        total_daily = 0
        total_weekly = 0
        for task in tasks:
            icon = task.get("icon", "📌")
            title = task.get("title", "")
            reward = task.get("reward_coins", 0)
            progress = task.get("current_progress", 0)
            target = task.get("condition_value", 0)
            completed = task.get("is_completed", False)
            claimed = task.get("is_claimed", False)
            period = task.get("period_type", "")
            task_key = task.get("task_key", "")
            if claimed:
                status = "✅ 已领取"
            elif completed:
                status = "🎁 可领取"
            else:
                status = f"⏳ {progress}/{target}"
            self.log(f"  {icon} {title} | {status} | +{reward}金币 | [{period}] [{task_key}]")
            if period == "daily":
                total_daily += reward
            else:
                total_weekly += reward
        print("-" * 60)
        self.log(f"💰 [任务] 每日奖励合计: {total_daily}金币 | 每周奖励合计: {total_weekly}金币")
        return f"待领取 {pending}个 | 每日 {total_daily}金币 | 每周 {total_weekly}金币"

    # ==================== 每日签到 ====================

    def checkin(self):
        self.log("📅 [签到] 执行每日签到...")
        resp = self._post(CHECKIN_URL)
        data = resp.json()
        coins = data.get("coins_awarded", 0)
        day = data.get("day_number", 0)
        msg = data.get("message", "")
        self.total_coins_earned += coins
        if not msg and coins == 0:
            self.sign_msg = "今日已签到过"
            self.log(f"   ⚠️ {self.sign_msg}")
        else:
            self.sign_msg = f"{msg} | 连续第{day}天 | +{coins}金币"
            self.log(f"   {self.sign_msg}")

        self.log("🎁 [签到] 领取签到奖励...")
        resp2 = self._post(f"{DAILY_TASKS_URL}/daily_checkin/claim")
        data2 = resp2.json()
        if data2.get("ok"):
            claim_coins = data2.get("coins", 0)
            claim_msg = data2.get("message", "")
            self.total_coins_earned += claim_coins
            self.sign_msg += f" | {claim_msg}(+{claim_coins}金币)"
            self.log(f"   {claim_msg} | +{claim_coins}金币")
        else:
            err_text = str(data2)[:200]
            if "已领取" in err_text or "已领" in err_text:
                self.sign_msg += " | 签到奖励已领取"
                self.log("   ⚠️ [签到] 签到奖励今日已领取")
            else:
                self.log(f"   ⚠️ [签到] 领取签到奖励失败: {err_text}")
        return data

    def claim_task(self, task_key):
        url = f"{DAILY_TASKS_URL}/{task_key}/claim"
        resp = self._post(url)
        data = resp.json()
        if data.get("ok"):
            coins = data.get("coins", 0)
            msg = data.get("message", "")
            self.total_coins_earned += coins
            self.log(f"   {msg} | +{coins}金币")
        else:
            self.log(f"   ⚠️ [任务] 领取失败 ({task_key}): {str(data)[:200]}")
        return data

    # ==================== 广告联盟（ads.dgccvi.com） ====================

    def get_ads_info(self):
        resp = self._get(ADS_LIST_URL, params={"device_id": self.device_id})
        data = resp.json()
        self.log(f"📡 [广告] enabled={data.get('enabled')} | 每日上限={data.get('max_views_per_day')} | 可选={len(data.get('items', []))}")
        return data

    def start_ad_session(self):
        payload = {"device_id": self.device_id, "client": "app"}
        resp = self._post(ADS_SESSION_START_URL, payload)
        try:
            data = resp.json()
        except Exception:
            data = {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:150]}"}
        return data

    def send_heartbeat(self, play_token, progress_seconds):
        payload = {"play_token": play_token, "progress_seconds": progress_seconds}
        resp = self._post(ADS_HEARTBEAT_URL, payload)
        return resp.json()

    def complete_ad_session(self, play_token, progress_seconds):
        payload = {"play_token": play_token, "progress_seconds": progress_seconds}
        resp = self._post(ADS_COMPLETE_URL, payload)
        return resp.json()

    def watch_ads(self, account_max_views=None):
        """完整广告观看流程（心跳节奏对齐真实 App）"""
        self.log("📺 [广告] 开始广告流程...")
        ads_info = self.get_ads_info()

        enabled = ads_info.get("enabled", False)
        # 广告接口上限与账号等级上限取小（金币账户 V1=20 次/天）
        max_views = ads_info.get("max_views_per_day", 20)
        # 可选 WY_MAX_ADS 限制单次运行观看数量，与服务端/账号上限取小
        env_max_views = os.environ.get("WY_MAX_ADS", "").strip()
        if env_max_views.isdigit():
            max_views = min(max_views, int(env_max_views))
        if account_max_views:
            max_views = min(max_views, account_max_views)
        items = ads_info.get("items", [])
        heartbeat_interval = ads_info.get("heartbeat_interval", 30)

        if not enabled:
            self.log("   ⚠️ [广告] 广告功能未启用")
            self.ad_msg = "广告功能未启用"
            return
        if max_views <= 0:
            if env_max_views == "0":
                self.log("   ⚠️ [广告] WY_MAX_ADS=0，已跳过广告")
                self.ad_msg = "WY_MAX_ADS=0，已跳过"
            else:
                self.log("   ⚠️ [广告] 今日广告次数已用完")
                self.ad_msg = "今日广告次数已用完"
            return
        self.log(f"   [广告] 广告已启用 | 今日可看 {max_views} 次 | 共 {len(items)} 个广告可选")

        success_count = 0
        fail_count = 0

        for i in range(max_views):
            self.log(f"\n{'─' * 50}")
            self.log(f"📺 [广告] 第 {i+1}/{max_views} 个广告")

            session_data = self.start_ad_session()
            if not session_data.get("ok"):
                msg = session_data.get("message") or str(session_data)[:200]
                self.log(f"   ❌ [广告] 启动广告会话失败: {msg}")
                fail_count += 1
                # 次数用尽等场景直接退出
                break

            sess = session_data.get("session", {})
            play_token = sess.get("play_token")
            duration = sess.get("duration_seconds", 30)
            reward = sess.get("reward_coins", 0)
            hb_interval = sess.get("heartbeat_interval", heartbeat_interval)
            ad_info = sess.get("ad", {})

            self.log(f"   📱 [广告] {ad_info.get('title', '未知')} | 时长: {duration}秒 | 💰 奖励: {reward}金币")

            # 模拟真实观看: 开播即报一次心跳
            time.sleep(random.uniform(0.2, 1.5))
            elapsed = random.uniform(0.1, 0.5)
            self.send_heartbeat(play_token, round(elapsed, 2))

            # 按 heartbeat_interval 周期上报（模拟视频 timeupdate）
            next_hb = hb_interval + random.uniform(0.1, 0.3)
            while elapsed < duration:
                remain = duration - elapsed
                step = min(next_hb, remain)
                time.sleep(max(step, 0.1) if step > 0.1 else 0.1)
                elapsed = min(elapsed + step, duration)
                if elapsed >= duration:
                    break
                self.send_heartbeat(play_token, round(elapsed, 2))
                self.log(f"   💓 [广告] 心跳 | 进度: {round(elapsed, 2)}/{duration}秒")
                next_hb = hb_interval + random.uniform(0.1, 0.3)

            # 视频结束: 补两次心跳（ended 事件 + complete 前强制上报，与抓包一致）
            final_progress = round(duration + random.uniform(0.05, 0.3), 2)
            self.send_heartbeat(play_token, final_progress)
            time.sleep(random.uniform(0.5, 1.2))
            self.send_heartbeat(play_token, final_progress)

            self.log("   🏁 [广告] 完成观看，领取奖励...")
            complete_data = self.complete_ad_session(play_token, final_progress)

            if not complete_data.get("ok"):
                # 领取失败等 2 秒重试一次（网络抖动等瞬时错误）
                self.log("   🔁 [广告] 领取失败，2 秒后重试一次...")
                time.sleep(2)
                complete_data = self.complete_ad_session(play_token, final_progress)

            if complete_data.get("ok"):
                coins = complete_data.get("gold_coins", 0)
                msg = complete_data.get("message", "")
                self.total_coins_earned += coins
                success_count += 1
                self.log(f"   ✅ [广告] {msg} | +{coins}金币 | 累计: {self.total_coins_earned}金币")
            else:
                err_msg = complete_data.get("message", "领取失败")
                self.log(f"   ❌ [广告] {err_msg}")
                fail_count += 1

            # 广告间隔: 服务端下发（当前 3-5 秒）
            if i < max_views - 1:
                interval = complete_data.get("next_request_available_in") or \
                    session_data.get("request_interval_seconds") or \
                    random.randint(3, 5)
                self.log(f"   ⏳ [广告] 等待 {interval} 秒后请求下一个广告...")
                time.sleep(interval)

        self.log(f"\n{'─' * 50}")
        self.log(f"📊 [广告] 广告观看汇总: 成功 {success_count} 次 | 失败 {fail_count} 次 | 本次运行累计 +{self.total_coins_earned}金币")
        self.ad_msg = f"成功 {success_count} 次 | 失败 {fail_count} 次"

    # ==================== 主流程 ====================

    def run(self) -> Dict[str, Any]:
        result = {
            "success": False,
            "loginMsg": "-",
            "signMsg": "-",
            "taskMsg": "-",
            "adMsg": "-",
            "coins": "-",
            "error": "",
        }

        self.log(f"🚀 [主程序] {APP_NAME} - 账号: {self.account} | UA: {mask(self.ua)}")

        if not self.login():
            result["error"] = f"登录失败: {self.last_error}"
            return result
        result["loginMsg"] = f"用户ID: {self.user_id}"

        # 0. 查询当前金币余额与账号等级广告上限
        user = self.get_user_info()
        nickname = user.get("nickname", "")
        wallet = user.get("wallet", {})
        start_coins = wallet.get("gold_coins", 0)
        account_max_views = user.get("max_alliance_ads_per_day")
        level = (user.get("gold_level") or {}).get("name", "")
        self.log(f"👤 [用户] {nickname} | 等级: {level} | 当前金币: {start_coins} | 广告上限: {account_max_views}/天")

        # 1. 获取每日任务
        self.log("📋 [任务] 获取任务列表...")
        tasks_data = self.get_daily_tasks()
        result["taskMsg"] = self.show_tasks(tasks_data)

        # 2. 每日签到
        self.checkin()
        result["signMsg"] = self.sign_msg

        # 3. 查询设备绑定信息
        try:
            devices_data = self.get_user_devices()
            max_devices = devices_data.get("max_devices", 0)
            used = devices_data.get("devices_used", 0)
            phone_masked = devices_data.get("phone_masked", "")
            self.log(f"📱 [设备] 设备: {used}/{max_devices} | 手机号: {phone_masked}")
            tip = devices_data.get("tip", "")
            if tip:
                self.log(f"   💡 {tip}")
        except Exception as e:
            self.log(f"   ⚠️ [设备] 查询设备信息失败: {e}")

        # 4. 看广告赚金币
        self.watch_ads(account_max_views=account_max_views)
        result["adMsg"] = self.ad_msg

        # 5. 领取可领取的任务奖励（签到后任务状态可能已更新，重新拉取）
        tasks_data = self.get_daily_tasks()
        for task in tasks_data.get("tasks", []):
            task_key = task.get("task_key", "")
            if task.get("is_completed") and not task.get("is_claimed"):
                self.claim_task(task_key)

        # 6. 查询最终金币余额
        user2 = self.get_user_info()
        end_coins = user2.get("wallet", {}).get("gold_coins", 0)
        earned = end_coins - start_coins
        result["coins"] = f"{earned:+d}（总 {end_coins}）"
        self.log(f"✨ [完成] 任务执行完毕 | 本次获得: {earned}金币 | 总金币: {end_coins}")

        result["success"] = True
        return result

def run_account(index: int, total: int, account: Dict[str, Any], ua: str) -> Dict[str, Any]:
    result = {
        "account": account["account"],
        "remark": account.get("remark", ""),
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "ua": mask(ua),
        "loginMsg": "-",
        "signMsg": "-",
        "taskMsg": "-",
        "adMsg": "-",
        "coins": "-",
        "error": "",
    }

    log_account_header(index, total, account["account"], account.get("remark", ""))

    proxies, proxy_ip = get_valid_proxy(account["account"])
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    try:
        app = WuYouPlan(
            account["account"],
            account["password"],
            account.get("device_id", ""),
            ua=ua,
            proxies=proxies,
        )
        result.update(app.run())
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")

    return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""♻️ 无忧计划任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        remark_line = f"🏷️ 备注：{res['remark']}\n" if res.get("remark") else ""
        content += f"""
🧩 账号 {idx}
📱 手机号：{res["account"]}
{remark_line}🌐 代理：{res["proxyStatus"]}
📡 出口IP：{res["proxyIp"]}
🖥️ UA：{res["ua"]}
🔐 登录：{res["loginMsg"]}
📝 签到：{res["signMsg"]}
📋 任务：{res["taskMsg"]}
📺 广告：{res["adMsg"]}
💰 金币：{res["coins"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    accounts = parse_accounts()
    log_title(len(accounts))

    if not accounts:
        print("❌ [配置] 未配置账号，请在环境变量 WY_ACCOUNT 中设置，格式: 账号#密码#device_id#备注 (多账号用换行分隔)")
        print("   示例: WY_ACCOUNT=13800138000#abc123#dev001#主号")
        return

    user_agents = get_user_agents()
    ua_source = "环境变量 WY_USER_AGENT" if os.environ.get("WY_USER_AGENT", "").strip() else "内置 20 个真机 UA"
    print(f"🖥️ [UA] 使用{ua_source}，共 {len(user_agents)} 个，按账号顺序轮换")

    results: List[Dict[str, Any]] = []

    for index, account in enumerate(accounts, 1):
        ua = user_agents[(index - 1) % len(user_agents)]
        try:
            result = run_account(index, len(accounts), account, ua)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {account['account']} 执行异常: {exc}")
            results.append({
                "account": account["account"],
                "remark": account.get("remark", ""),
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "ua": mask(ua),
                "loginMsg": "-",
                "signMsg": "-",
                "taskMsg": "-",
                "adMsg": "-",
                "coins": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(accounts):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 无忧计划任务执行完成                         ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus(f"♻️ {APP_NAME}任务完成", build_notify(results))


if __name__ == "__main__":
    main()
