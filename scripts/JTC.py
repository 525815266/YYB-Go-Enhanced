#!/usr/bin/env python3
# name: 捷停车
# cron: 16 13 * * *
# -*- coding: utf-8 -*-
# ==============================================
# 捷停车自动任务脚本（青龙面板专用版）
# 功能: 自动完成每日签到、浏览车位优选、浏览找优惠、领取奖励
# 适配: 捷停车小程序 v312 | 品赞代理 | PushPlus推送
# ==============================================
#
# 【青龙环境变量说明】
# YYB_SERVER 必填：wxcode服务地址，多地址换行填写
# PROXY_API 可选：品赞代理提取链接
# PROXY_TYPE 可选：http / socks5，默认http
# PLUSPLUS_TOKEN 可选：推送token
#

import os
import sys
import asyncio
import json
import random
import hashlib
import time
from datetime import datetime
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import binascii
from typing import List, Dict, Any, Optional, Tuple
from yyb_account_guard import filter_accounts, update_from_result

# 强制全局禁用所有系统代理环境变量
for env_var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
                'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy']:
    os.environ.pop(env_var, None)

# ===================== JWT库自动检测与修复 =====================
try:
    import jwt
    if not hasattr(jwt, 'decode'):
        raise ImportError("安装的是错误的jwt库，不是pyjwt")
except ImportError as e:
    print("❌ JWT库错误！请在青龙依赖管理执行以下命令：")
    print("pip uninstall -y jwt PyJWT")
    print("pip install pyjwt")
    sys.exit(1)

try:
    import httpx
    from httpx import AsyncHTTPTransport
    from httpx_socks import AsyncProxyTransport
except ImportError:
    print("❌ 缺少依赖库，请执行：")
    print("pip install httpx[http2] httpx-socks python-dotenv pycryptodome")
    sys.exit(1)

# ===================== 配置项 =====================
# 从环境变量 YYB_SERVER 读取wxcode服务地址，多行换行分隔
SERVERS = []
env_YYB_SERVER = os.getenv("YYB_SERVER", "")
if env_YYB_SERVER:
    raw_lines = env_YYB_SERVER.splitlines()
    SERVERS = [line.strip() for line in raw_lines if line.strip()]

# 无有效地址直接退出
if len(SERVERS) == 0:
    print("❌ 错误：未读取到环境变量 YYB_SERVER 或无有效IP端口！")
    print("青龙环境变量YYB_SERVER填写示例（每行一个地址）：")
    print("127.0.0.1:8088")
    print("192.168.1.21:8088")
    sys.exit(1)

print(f"✅ 成功读取 {len(SERVERS)} 台内网wxcode服务：")
for item in SERVERS:
    print(f" - {item}")
print("-" * 60 + "\n")

# PushPlus 通知Token（环境变量，可选）
PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")

# 品赞代理配置（环境变量，可选）
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http")
PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"

# 核心代理开关
ENABLE_PER_ACCOUNT_PROXY = True
PROXY_FETCH_INTERVAL = 5000  # 代理提取间隔（毫秒）
ENABLE_DIRECT_FALLBACK = True

# 固定配置（2026-05-13最新抓包）
APPID = "wx24b70f0ad2a9a89a"
SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)
APP_VERSION = "312"
XWEB_VERSION = "19823"
TOKEN_BASE_URL = "https://www.jslife.com.cn/wxhttp/weixin/xcx/get_openid_by_code"
BASE_URL = "https://sytgate.jslife.com.cn"
REPORT_URL = "https://etgw.jparking.cn/data-report-gateway/syt-data-report/receive"
TASK_QUERY_URL = "/base-gateway/integral/v2/task/query-new"

# UA池
USER_AGENT_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541923) XWEB/19823",
    f"Mozilla/5.0 (Linux; Android 14; 2512BPNDAC Build/UKQ1.230917.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.153 Mobile Safari/537.36 XWEB/{XWEB_VERSION} MMWEBSDK/20251006 MiniProgramEnv/android"
]

# 只保留指定的3个任务，其他全部跳过
SKIP_TASKS = {"T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09", "T10",
              "T11", "T12", "T46", "T48", "T49", "T50", "T81", "T87"}

# 强制执行的核心任务（已更新为最新编号）
FORCE_EXECUTE_TASKS = [
    ("T01", "浏览找优惠"),
    ("T47", "浏览车位优选")
]

# ===================== 工具函数 =====================
def sleep(ms: int) -> asyncio.Future:
    return asyncio.sleep(ms / 1000)

def random_int(min_val: int, max_val: int) -> int:
    return random.randint(min_val, max_val)

def get_ua() -> str:
    return random.choice(USER_AGENT_LIST)

def build_direct_transport() -> AsyncHTTPTransport:
    return AsyncHTTPTransport(verify=False)

def parse_yyb_go_entry(raw_value: str) -> Tuple[str, str]:
    value = str(raw_value or "").strip()
    if not value:
        return "", ""
    at_index = value.rfind("@")
    if at_index == -1:
        return value, ""
    server = value[:at_index].strip()
    ref = value[at_index + 1:].strip()
    server = server.removeprefix("http://").removeprefix("https://").rstrip("/")
    return server, ref


async def get_code_via_yyb(server_entry: str, appid: str) -> Optional[str]:
    server, ref = parse_yyb_go_entry(server_entry)
    if not server:
        print(f"❌ [{server_entry}] 获取code失败 | 服务地址为空")
        return None
    if not ref:
        print(f"❌ [{server_entry}] 获取code失败 | 缺少openid/ref")
        return None

    url = f"http://{server}/wxapp/getCode"
    try:
        async with httpx.AsyncClient(timeout=20.0, verify=False, trust_env=False) as client:
            response = await client.post(url, json={"ref": ref, "app_id": appid})
            res = response.json()

        code = (((res.get("data") or {}).get("result") or {}).get("code"))
        if res.get("code") != 0 or not code:
            print(f"❌ [{server_entry}] 获取code失败 | 返回异常: {str(res)[:200]}")
            return None

        print(f"✅ [{server}] 获取code成功")
        return code
    except json.JSONDecodeError:
        print(f"❌ [{server_entry}] 获取code失败 | 响应不是JSON格式")
        return None
    except Exception as e:
        print(f"❌ [{server_entry}] 获取code异常 | 原因: {str(e)}")
        return None

# ===================== 品赞代理系统 =====================
def parse_proxy_response(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        proxy_obj = None
        if data.get("data") and isinstance(data["data"], list) and len(data["data"]) > 0:
            proxy_obj = data["data"][0]
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif data.get("result") and data.get("result").get("ip") and data.get("result").get("port"):
            proxy_obj = data["result"]

        if proxy_obj:
            return {
                "host": proxy_obj["ip"],
                "port": int(proxy_obj["port"]),
                "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                "password": proxy_obj.get("pass") or proxy_obj.get("password") or ""
            }
    except json.JSONDecodeError:
        if ":" in text:
            parts = text.split(":")
            if len(parts) >= 2:
                return {
                    "host": parts[0],
                    "port": int(parts[1]),
                    "username": parts[2] if len(parts) >= 3 else "",
                    "password": parts[3] if len(parts) >= 4 else ""
                }
    return None

async def validate_proxy(proxy_info: Dict[str, Any]) -> bool:
    if not proxy_info:
        return False

    try:
        transport = build_proxy_transport(proxy_info)
        async with httpx.AsyncClient(transport=transport, timeout=15.0, verify=False) as client:
            response = await client.get(PROXY_VALIDATE_URL)
            if response.status_code == 200:
                ip = response.json().get("origin", "未知")
                print(f"✅ 代理验证通过 | 出口IP: {ip} | 预计有效期: 5分钟")
                return True
    except Exception as e:
        print(f"⚠️ 代理验证失败 | 原因: {str(e)}")
    return False

def build_proxy_transport(proxy_info: Dict[str, Any]) -> Optional[AsyncProxyTransport]:
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info["username"]
    password = proxy_info["password"]

    try:
        if PROXY_TYPE == "socks5":
            proxy_url = f"socks5://{username}:{password}@{host}:{port}" if username and password else f"socks5://{host}:{port}"
        else:
            proxy_url = f"http://{username}:{password}@{host}:{port}" if username and password else f"http://{host}:{port}"

        return AsyncProxyTransport.from_url(proxy_url, verify=False)
    except Exception as e:
        print(f"❌ 代理生成失败 | 原因: {str(e)}")
        return None

async def get_valid_proxy(account_name: str) -> Optional[Dict[str, Any]]:
    if not PROXY_API:
        print(f"ℹ️ [{account_name}] 未配置代理 | 使用直连模式")
        return None

    print(f"🔌 [{account_name}] 正在获取专属代理...")

    for i in range(PROXY_RETRY_TIMES):
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=build_direct_transport()) as client:
                response = await client.get(PROXY_API)
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                print(f"⚠️ [{account_name}] 第{i+1}次获取代理失败 | 响应格式错误")
                continue

            if await validate_proxy(proxy_info):
                return proxy_info
            else:
                print(f"⚠️ [{account_name}] 第{i+1}次代理不可用 | 重试中...")

        except Exception as e:
            print(f"⚠️ [{account_name}] 第{i+1}次获取代理异常 | 原因: {str(e)}")

        if i < PROXY_RETRY_TIMES - 1:
            await sleep(2000)

    print(f"❌ [{account_name}] 代理获取失败 | 切换直连模式")
    return None

# ===================== 智能代理管理器（新增） =====================
class ProxyManager:
    def __init__(self, account_name: str):
        self.account_name = account_name
        self.proxy_info: Optional[Dict[str, Any]] = None
        self.rebuild_count = 0
        self.max_rebuild = 3  # 单账号最多重建3次代理
        self.last_rebuild_time = 0

    async def get_proxy(self) -> Optional[Dict[str, Any]]:
        """获取有效代理（首次获取或重建时调用）"""
        if not PROXY_API:
            return None

        # 防止频繁重建
        if time.time() - self.last_rebuild_time < 5:
            await sleep(2000)

        self.proxy_info = await get_valid_proxy(self.account_name)
        self.last_rebuild_time = time.time()
        return self.proxy_info

    async def rebuild(self) -> bool:
        """重建代理（失败时自动降级为直连）"""
        if self.rebuild_count >= self.max_rebuild:
            print(f"❌ [{self.account_name}] 代理重建次数已达上限，切换为直连模式")
            self.proxy_info = None
            return False

        self.rebuild_count += 1
        print(f"🔄 [{self.account_name}] 正在重建代理（第{self.rebuild_count}次）...")

        # 关闭旧代理连接
        if self.proxy_info:
            self.proxy_info = None

        new_proxy = await self.get_proxy()
        if new_proxy:
            print(f"✅ [{self.account_name}] 代理重建成功 | 新出口IP: {new_proxy['host']}:{new_proxy['port']}")
            return True
        else:
            print(f"⚠️ [{self.account_name}] 代理重建失败，切换为直连模式")
            return False

    def is_proxy_error(self, e: Exception) -> bool:
        """判断是否为代理相关错误"""
        error_str = str(e).lower()
        proxy_error_keywords = [
            "invalid proxy response",
            "proxy connection failed",
            "socks error",
            "connection reset by peer",
            "connection refused",
            "timeout",
            "proxy",
            "transport closed"
        ]
        return any(keyword in error_str for keyword in proxy_error_keywords)

# ===================== PushPlus推送函数 =====================
async def send_plusplus_notification(title: str, content: str) -> None:
    if not PLUSPLUS_TOKEN:
        return

    try:
        async with httpx.AsyncClient(timeout=5.0, transport=build_direct_transport()) as client:
            response = await client.post(
                "https://www.pushplus.plus/send",
                json={
                    "token": PLUSPLUS_TOKEN,
                    "title": title,
                    "content": content,
                    "template": "txt"
                }
            )
        if response.status_code == 200:
            print("✅ 通知推送成功")
    except Exception as e:
        print(f"❌ 通知推送失败 | 原因: {str(e)}")

# ===================== 数据上报生成器 =====================
class DataReportGenerator:
    def __init__(self):
        self.secret_key = "GaT92Kf6cbDc1Pea9S720GJnL56A14x3R"

    def generate_nonce(self, timestamp=None):
        if timestamp is None:
            timestamp = int(datetime.now().timestamp() * 1000)
        random_7_digits = str(random.randint(1000000, 9999999))
        return f"{random_7_digits}{timestamp}", timestamp

    def generate_sign(self, data):
        sorted_params = []
        for key in sorted(data.keys()):
            value = data[key]
            if value is not None:
                sorted_params.append(f"{key}={value}")
        sign_string = "&".join(sorted_params) + "&" + self.secret_key
        return hashlib.md5(sign_string.encode('utf-8')).hexdigest().upper()

    def create_report_data(self, user_id, open_id, event_name="ShowGoToClaim", page_event_name="RentalPage", task_info=None, extra_props=None):
        timestamp = int(datetime.now().timestamp() * 1000)
        nonce, _ = self.generate_nonce(timestamp)

        event_property = {"pageEventName": page_event_name}
        if extra_props and isinstance(extra_props, dict):
            event_property.update(extra_props)

        if event_name == "GoToFinishClick" and task_info:
            event_property.update({
                "TaskName": task_info.get("showTitle", ""),
                "TaskNo": task_info.get("taskNo", ""),
                "pageEventName": "PointsTaskPage",
                "referrer": "pages/my/my",
                "curPgUrl": "subPkg/tcb/index"
            })
        elif event_name in ("TaskStart", "TaskAction", "TaskProgress", "TaskFinish", "TaskReceive") and task_info:
            event_property.update({
                "TaskName": task_info.get("showTitle", ""),
                "TaskNo": task_info.get("taskNo", ""),
                "stage": event_name,
                "pageEventName": "PointsTaskPage",
            })

        data = {
            "opSystem": "windows",
            "opSystemVersion": "Windows 10 x64",
            "phoneModel": "microsoft",
            "brand": "microsoft",
            "language": "zh_CN",
            "userAgent": "",
            "deviceId": "",
            "screenResolution": "415*800",
            "longitude": None,
            "latitude": None,
            "serviceProviders": "",
            "netType": "unknown",
            "productName": "捷停车微信小程序",
            "productVersion": "4.0.6.26",
            "dataSourceType": "JTC_WX_MINI",
            "userId": user_id,
            "openId": open_id,
            "eventStartTime": timestamp,
            "MaterialId": "",
            "SourceId": "",
            "eventType": "activity",
            "eventName": event_name,
            "eventProperty": json.dumps(event_property, ensure_ascii=False),
            "signType": "MD5",
            "timestamp": timestamp,
            "nonce": nonce
        }

        data["sign"] = self.generate_sign(data)
        return data

# ===================== 核心业务类（代理自动重连版） =====================
class JtcBot:
    def __init__(self, server: str, proxy_manager: Optional[ProxyManager] = None):
        self.server = server
        self.proxy_manager = proxy_manager
        self.token = None
        self.user_id = None
        self.open_id = None
        self.longitude = None
        self.latitude = None
        self.ua = get_ua()
        self.client: Optional[httpx.AsyncClient] = None
        self.report_generator = DataReportGenerator()
        self.task_browse_seconds = {}

    async def __aenter__(self):
        # 初始化代理管理器
        if not self.proxy_manager:
            self.proxy_manager = ProxyManager(self.server)
            await self.proxy_manager.get_proxy()

        # 首次创建客户端
        await self._rebuild_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()

    async def _rebuild_client(self) -> None:
        """重建客户端（代理变更时自动调用）"""
        # 关闭旧客户端
        if self.client:
            await self.client.aclose()

        # 根据当前代理状态创建新transport
        if self.proxy_manager and self.proxy_manager.proxy_info:
            transport = build_proxy_transport(self.proxy_manager.proxy_info)
            mode = "代理"
        else:
            transport = build_direct_transport()
            mode = "直连"

        # 创建新客户端，保留所有已有的请求头和token
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=self._get_base_headers(),
            transport=transport,
            http2=True,
            timeout=30.0,
            verify=False
        )

        print(f"🔌 [{self.server}] 客户端已重建 | 当前模式: {mode}")

    def _get_base_headers(self) -> Dict[str, Any]:
        headers = {
            "Host": "sytgate.jslife.com.cn",
            "Connection": "keep-alive",
            "applicationVersion": "1.0.1",
            "User-Agent": self.ua,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Referer": f"https://servicewechat.com/{APPID}/{APP_VERSION}/page-frame.html",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9"
        }

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        return headers

    # 通用请求包装器（自动处理代理错误）
    async def _safe_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """安全请求：自动捕获代理错误并重建代理重试"""
        for retry in range(2):  # 最多重试2次（1次原代理，1次重建后）
            try:
                response = await self.client.request(method, url, **kwargs)
                return response
            except Exception as e:
                if self.proxy_manager and self.proxy_manager.is_proxy_error(e):
                    print(f"⚠️ [{self.server}] 请求失败 | 代理错误: {str(e)}")
                    if retry == 0:
                        # 第一次失败：重建代理
                        await self.proxy_manager.rebuild()
                        await self._rebuild_client()
                        continue
                    else:
                        # 第二次失败：降级为直连
                        print(f"⚠️ [{self.server}] 代理重试失败，切换直连重试")
                        self.proxy_manager.proxy_info = None
                        await self._rebuild_client()
                        continue
                else:
                    # 非代理错误，直接抛出
                    raise e

        # 所有重试都失败
        raise Exception("请求失败，所有重试次数已耗尽")

    async def get_code(self) -> Optional[str]:
        """从本地服务获取code（增加3次重试）"""
        return await get_code_via_yyb(self.server, APPID)

    async def get_token_by_code(self, code: str) -> Optional[str]:
        """通过code换取token（适配最新obj响应格式）"""
        print(f"🔑 [{self.server}] 正在换取token...")

        timestamp = int(time.time() * 1000)
        token_url = f"{TOKEN_BASE_URL}?t={timestamp}"

        headers = {
            "Host": "www.jslife.com.cn",
            "Connection": "keep-alive",
            "applicationVersion": "1.0.1",
            "User-Agent": self.ua,
            "xweb_xhr": "1",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "*/*",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"https://servicewechat.com/{APPID}/{APP_VERSION}/page-frame.html",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9"
        }

        payload = {
            "code": code,
            "userType": "WX_XCX_JTC",
            "appId": APPID
        }

        try:
            transport = build_proxy_transport(self.proxy_manager.proxy_info) if self.proxy_manager.proxy_info else build_direct_transport()
            mode = "代理" if self.proxy_manager.proxy_info else "直连"

            print(f"🌐 [{self.server}] 使用{mode}发起token请求 | URL: {token_url}")
            async with httpx.AsyncClient(
                transport=transport,
                headers=headers,
                timeout=20.0,
                http2=True,
                verify=False
            ) as client:
                response = await client.post(token_url, json=payload)

            print(f"📝 [{self.server}] token接口响应 | 状态码: {response.status_code}")

            if response.status_code != 200:
                raise Exception(f"HTTP错误: {response.status_code}")

            res = response.json()
            # 适配最新响应格式（resultCode=0表示成功，token在obj字段）
            if res.get("resultCode") == "0" and res.get("obj") and res["obj"].get("token"):
                self.token = res["obj"]["token"]
                print(f"✅ [{self.server}] 获取token成功 | 模式: {mode}")
                return self.token
            # 兼容旧格式
            elif (res.get("success") and res.get("data") and res["data"].get("token")):
                self.token = res["data"]["token"]
                print(f"✅ [{self.server}] 获取token成功（旧格式）| 模式: {mode}")
                return self.token
            else:
                raise Exception(f"业务错误: {res.get('message', '未知错误')}")
        except Exception as e:
            print(f"⚠️ [{self.server}] {mode}获取token失败 | 原因: {str(e)}")

            if self.proxy_manager.proxy_info and ENABLE_DIRECT_FALLBACK:
                print(f"🌐 [{self.server}] 切换直连重试...")
                try:
                    async with httpx.AsyncClient(
                        headers=headers,
                        timeout=20.0,
                        http2=True,
                        transport=build_direct_transport(),
                        verify=False
                    ) as client:
                        response = await client.post(token_url, json=payload)

                    print(f"📝 [{self.server}] 直连响应 | 状态码: {response.status_code}")

                    res = response.json()
                    if res.get("resultCode") == "0" and res.get("obj") and res["obj"].get("token"):
                        self.token = res["obj"]["token"]
                        print(f"✅ [{self.server}] 直连获取token成功")
                        return self.token
                    elif (res.get("success") and res.get("data") and res["data"].get("token")):
                        self.token = res["data"]["token"]
                        print(f"✅ [{self.server}] 直连获取token成功（旧格式）")
                        return self.token
                    else:
                        raise Exception(f"直连业务错误: {res.get('message', '未知错误')}")
                except Exception as e2:
                    print(f"❌ [{self.server}] 直连获取token失败 | 原因: {str(e2)}")

        return None

    def parse_jwt(self):
        """解析JWT获取用户信息（兼容新token格式）"""
        try:
            decoded = jwt.decode(self.token, options={"verify_signature": False}, algorithms=["HS256"])
            sub_data = json.loads(decoded.get("sub", "{}"))
            self.user_id = sub_data.get("userId")
            self.open_id = sub_data.get("id")
            exp = decoded.get("exp")
            return exp
        except Exception as e:
            print(f"❌ [{self.server}] JWT解析失败 | 原因: {str(e)}")
            return None

    async def get_location_info(self):
        """获取经纬度坐标信息"""
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=build_direct_transport(), verify=False) as client:
                response = await client.get("https://ipinfo.io/json")
                data = response.json()

            loc = data.get("loc", "")
            if loc and "," in loc:
                lat_str, lon_str = loc.split(",")
                base_lat = float(lat_str.strip())
                base_lon = float(lon_str.strip())

                self.longitude = base_lon + random.randint(0, 9999999999999) / 10**16
                self.latitude = base_lat + random.randint(0, 999999999999999) / 10**18

                print(f"✅ [{self.server}] 经纬度已补全 | 经度: {self.longitude:.6f} | 纬度: {self.latitude:.6f}")
                return True
            else:
                raise Exception("无法解析经纬度")
        except Exception as e:
            print(f"ℹ️ [{self.server}] 使用默认北京坐标 | 原因: {str(e)}")
            self.longitude = 116.413384
            self.latitude = 39.910925
            return True

    async def send_data_report(self, event_name="ShowGoToClaim", task_info=None, extra_props=None):
        """异步埋点数据上报（使用安全请求）"""
        try:
            report_data = self.report_generator.create_report_data(
                self.user_id, self.open_id, event_name, "RentalPage", task_info, extra_props
            )
            report_data["longitude"] = self.longitude
            report_data["latitude"] = self.latitude

            headers = {
                "Host": "etgw.jparking.cn",
                "applicationVersion": "1.0.0",
                "User-Agent": self.ua,
                "xweb_xhr": "1",
                "Content-Type": "application/json",
                "uc_id": self.open_id,
                "Accept": "*/*",
                "Referer": f"https://servicewechat.com/{APPID}/{APP_VERSION}/page-frame.html",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": "zh-CN,zh;q=0.9"
            }

            transport = build_proxy_transport(self.proxy_manager.proxy_info) if self.proxy_manager.proxy_info else build_direct_transport()

            async with httpx.AsyncClient(
                transport=transport,
                timeout=15.0,
                verify=False
            ) as client:
                await client.post(REPORT_URL, headers=headers, json=report_data)
            return True
        except Exception as e:
            return False

    def check_response(self, response_data):
        """检查响应是否正常（适配最新resultCode格式）"""
        if response_data.get("resultCode") != "0" and response_data.get("code") != "0":
            error_msg = response_data.get("message", "未知错误")
            # 特殊处理："已达到最大领取次数"视为成功
            if "已达到最大领取次数" in error_msg:
                print(f"ℹ️ [{self.server}] {error_msg}")
                return True
            print(f"❌ [{self.server}] 请求失败 | 原因: {error_msg}")
            return False
        return True

    def safe_get_reward(self, data, default=0):
        if data is None:
            return default
        if isinstance(data, (int, float)):
            return int(data)
        if isinstance(data, str):
            try:
                return int(float(data))
            except:
                return default
        if isinstance(data, dict):
            for key in ("amount", "value", "points", "integral", "data", "reward", "cnt", "count"):
                if key in data:
                    return self.safe_get_reward(data.get(key), default)
        return default

    def format_phone(self, phone):
        if len(phone) == 11:
            return f"{phone[:3]}****{phone[-4:]}"
        return phone

    async def get_user_info(self):
        """获取用户信息（使用安全请求）"""
        try:
            response = await self._safe_request(
                "POST",
                "/core-gateway/user/query/attention/info",
                json={"h5Source": "WX_XCX_JTC", "openId": self.open_id}
            )
            response_data = response.json()

            if self.check_response(response_data):
                return response_data.get("obj", {})
            return None
        except Exception as e:
            print(f"❌ [{self.server}] 获取用户信息失败 | 原因: {str(e)}")
            return None

    async def perform_sign_in(self):
        """执行签到操作（使用安全请求）"""
        print(f"📝 [{self.server}] 开始执行签到...")

        try:
            # 签到任务查询
            await self._safe_request(
                "POST",
                "/base-gateway/integral/v2/sign-in-task/query",
                json={"userId": self.user_id, "platformType": "WX_XCX_JTC"}
            )
            await sleep(1000)

            # 气泡奖励查询
            await self._safe_request(
                "POST",
                "/base-gateway/integral/v2/show/header-pop/query",
                json={"userId": self.user_id, "platformType": "WX_XCX_JTC", "osType": "ANDROID", "reqVersion": "V2.0"}
            )
            await sleep(1000)

            # 领取签到奖励
            response = await self._safe_request(
                "POST",
                "/base-gateway/integral/v2/task/receive",
                json={
                    "userId": self.user_id,
                    "taskNo": "T00",
                    "reqSource": "WX_XCX_JTC",
                    "platformType": "WX_XCX_JTC",
                    "osType": "WINDOWS",
                    "token": self.token
                }
            )
            response_data = response.json()

            if "今日已签到" in response_data.get("message", ""):
                print(f"ℹ️ [{self.server}] 今日已签到")
                return True
            elif self.check_response(response_data):
                reward = self.safe_get_reward(response_data.get("data", 0))
                if reward > 0:
                    print(f"✅ [{self.server}] 签到成功 | 获得{reward}捷停币")
                return True
            else:
                print(f"❌ [{self.server}] 签到失败")
                return False
        except Exception as e:
            print(f"❌ [{self.server}] 签到异常 | 原因: {str(e)}")
            return False

    async def get_task_list(self):
        """获取任务列表（使用安全请求）"""
        try:
            task_headers = {
                **self._get_base_headers(),
                "applicationVersion": "1.0.0",
                "UC_ID": self.open_id
            }

            payload = {
                "userId": self.user_id,
                "platformType": "WX_XCX_JTC",
                "osType": "WINDOWS",
                "reqVersion": "V2.0"
            }

            response = await self._safe_request(
                "POST",
                TASK_QUERY_URL,
                headers=task_headers,
                json=payload
            )
            response_data = response.json()

            if self.check_response(response_data):
                task_data = response_data.get("data", [])
                print(f"✅ [{self.server}] 获取到{len(task_data)}个任务")

                for task in task_data:
                    task_no = task.get("taskNo")
                    browse_seconds = task.get("browseSeconds", 10)
                    self.task_browse_seconds[task_no] = browse_seconds
                    print(f"  - {task.get('showTitle')} | 编号: {task_no} | 状态: {task.get('taskStatus')} | 停留: {browse_seconds}秒")

                return task_data
            return []
        except Exception as e:
            print(f"❌ [{self.server}] 获取任务列表失败 | 原因: {str(e)}")
            return []

    async def receive_task_reward(self, task_no, task_info=None):
        """领取任务奖励（使用安全请求）"""
        show_title = task_info.get("showTitle", "") if task_info else ""

        await self.send_data_report("GoToClaimClick", task_info)
        await sleep(200)
        await self.send_data_report("TaskReceive", task_info, extra_props={"step": "receive_start"})
        await sleep(200)
        await self.send_data_report("ClaimClick", task_info)
        await sleep(300)

        try:
            response = await self._safe_request(
                "POST",
                "/base-gateway/integral/v2/task/receive",
                json={
                    "userId": self.user_id,
                    "taskNo": task_no,
                    "reqSource": "WX_XCX_JTC",
                    "platformType": "WX_XCX_JTC",
                    "osType": "ANDROID"
                }
            )
            response_data = response.json()

            if self.check_response(response_data):
                reward = self.safe_get_reward(response_data.get("data", 0))
                if reward > 0:
                    print(f"✅ [{self.server}] 领取【{show_title}】奖励 | +{reward}捷停币")
                return reward
            return 0
        except Exception as e:
            print(f"❌ [{self.server}] 领取【{show_title}】奖励失败 | 原因: {str(e)}")
            return 0

    async def simulate_task_action(self, task_no, task_info=None):
        """模拟任务操作（自动适配接口要求的停留时间）"""
        await self.send_data_report("TaskStart", {"taskNo": task_no, "showTitle": ""}, extra_props={"step": "start"})
        await sleep(200)

        # 获取接口要求的停留时间，默认10秒
        stay_seconds = max(self.task_browse_seconds.get(task_no, 10) - 2, 5)

        if task_no == "T01":  # 浏览找优惠
            print(f"⏳ [{self.server}] 进入【找优惠】页面，停留{stay_seconds}秒...")
            await self.send_data_report("PageView")
            await sleep(1000)
            await self.send_data_report("FindDiscountClick")
            await sleep(1000)
            # 严格按照接口要求停留
            await sleep(stay_seconds * 1000)
            await self.send_data_report("TaskAction", {"taskNo": task_no}, extra_props={"action": "find_discount"})
            await sleep(500)

        elif task_no == "T47":  # 浏览车位优选（已更新为最新编号）
            print(f"⏳ [{self.server}] 进入【车位优选】页面，停留{stay_seconds}秒...")
            await self.send_data_report("PageView")
            await sleep(1000)
            await self.send_data_report("ParkingSpaceClick")
            await sleep(1000)
            # 严格按照接口要求停留
            await sleep(stay_seconds * 1000)
            await self.send_data_report("TaskAction", {"taskNo": task_no}, extra_props={"action": "view_parking"})
            await sleep(500)

        await self.send_data_report("TaskProgress", {"taskNo": task_no}, extra_props={"progress": "100%"})
        await sleep(200)

    async def complete_task(self, task_no, task_info=None):
        """完成任务（使用安全请求，自动处理代理错误）"""
        show_title = task_info.get("showTitle", "") if task_info else ""

        if task_info and task_info.get("taskStatus") == "DOWN":
            print(f"ℹ️ [{self.server}] 【{show_title}】已完成，跳过执行")
            return True

        print(f"🔄 [{self.server}] 正在完成【{show_title}】")

        try:
            await self.simulate_task_action(task_no, task_info)

            if task_info:
                await self.send_data_report("GoToFinishClick", task_info)
                await sleep(500)

            await self.send_data_report("TaskProgress", task_info, extra_props={"progress": "near_finish"})
            await sleep(300)

            response = await self._safe_request(
                "POST",
                "/base-gateway/integral/v2/task/complete",
                json={
                    "userId": self.user_id,
                    "taskNo": task_no,
                    "receiveTag": True,
                    "reqSource": "WX_XCX_JTC",
                    "platformType": "WX_XCX_JTC",
                    "osType": "IOS",
                    "token": self.token
                }
            )
            response_data = response.json()

            if self.check_response(response_data):
                await sleep(500)
                await self.send_data_report("TaskFinish", task_info, extra_props={"stage": "finish"})
                await sleep(200)
                await self.send_data_report("ShowGoToClaim", task_info)

                reward = self.safe_get_reward(response_data.get("data", 0))
                if reward > 0:
                    print(f"✅ [{self.server}] 完成【{show_title}】| +{reward}捷停币")
                return True
            else:
                print(f"❌ [{self.server}] 完成【{show_title}】失败")
                return False
        except Exception as e:
            print(f"❌ [{self.server}] 完成【{show_title}】异常 | 原因: {str(e)}")
            return False

    async def get_balance(self):
        """获取账户余额（使用安全请求）"""
        try:
            response = await self._safe_request(
                "POST",
                "/base-gateway/integral/v2/balance/query",
                json={"reqSource": "WX_XCX_JTC", "userId": self.user_id, "openId": self.open_id}
            )
            response_data = response.json()

            if self.check_response(response_data):
                return response_data.get("data", {})
            return {}
        except Exception as e:
            print(f"❌ [{self.server}] 获取余额失败 | 原因: {str(e)}")
            return {}

    async def run(self) -> Dict[str, Any]:
        """运行主流程（优化：已完成任务直接领取奖励）"""
        result = {
            "server": self.server,
            "success": False,
            "proxy_status": "直连" if not self.proxy_manager.proxy_info else "专属代理",
            "phone": "未知",
            "sign_msg": "",
            "task_count": 0,
            "total_reward": 0,
            "balance": 0,
            "deduct_amount": 0,
            "error": ""
        }

        print(f"\n{'='*40}")
        print(f"[{self.server}] 开始执行任务")
        print(f"{'='*40}")

        try:
            # 启动延迟
            await sleep(random_int(2000, 5000))

            # 1. 获取code
            code = await self.get_code()
            if not code:
                result["error"] = "获取code失败"
                return result

            # 2. 获取token
            token = await self.get_token_by_code(code)
            if not token:
                result["error"] = "获取token失败"
                return result

            # 3. 解析JWT
            exp = self.parse_jwt()
            if not self.user_id or not self.open_id:
                result["error"] = "JWT解析失败"
                return result

            # 4. 获取经纬度
            await self.get_location_info()

            # 5. 获取用户信息
            user_info = await self.get_user_info()
            if user_info:
                result["phone"] = self.format_phone(user_info.get("telephone", "未知"))
                print(f"👤 [{self.server}] 用户: {result['phone']}")

            # 6. 执行签到
            sign_success = await self.perform_sign_in()
            result["sign_msg"] = "签到成功" if sign_success else "今日已签到"
            await sleep(1000)

            # 7. 处理任务
            task_data = await self.get_task_list()
            receivable_tasks = []
            incomplete_tasks = []
            task_info_map = {}

            if task_data:
                for task in task_data:
                    task_no = task.get("taskNo")
                    task_status = task.get("taskStatus")
                    show_title = task.get("showTitle", "")
                    task_info_map[task_no] = task

                    if task_no in SKIP_TASKS:
                        print(f"⏭️ [{self.server}] 跳过任务: {show_title}")
                        continue

                    if task_status == "RECEIVE":
                        receivable_tasks.append((task_no, show_title))
                    elif task_status == "GOTO":
                        incomplete_tasks.append((task_no, show_title))

            # 强制执行核心任务（即使不在任务列表中）
            print(f"\n🔧 [{self.server}] 检查强制核心任务...")
            for task_no, show_title in FORCE_EXECUTE_TASKS:
                if task_no not in task_info_map and task_no not in SKIP_TASKS:
                    print(f"➕ [{self.server}] 添加强制任务: {show_title}")
                    incomplete_tasks.append((task_no, show_title))
                    task_info_map[task_no] = {"taskNo": task_no, "showTitle": show_title, "taskStatus": "GOTO"}

            # 领取可领取的任务奖励
            for task_no, show_title in receivable_tasks:
                reward = await self.receive_task_reward(task_no, task_info_map.get(task_no))
                result["total_reward"] += reward
                await sleep(1000)

            # 完成未完成的任务
            for task_no, show_title in incomplete_tasks:
                success = await self.complete_task(task_no, task_info_map.get(task_no))
                if success:
                    result["task_count"] += 1
                    await sleep(1000)
                    # 领取新完成的任务奖励
                    await sleep(3000)
                    reward = await self.receive_task_reward(task_no, task_info_map.get(task_no))
                    result["total_reward"] += reward

            # 8. 获取最终余额
            balance_info = await self.get_balance()
            if balance_info:
                result["balance"] = balance_info.get("accountAmt", 0)
                result["deduct_amount"] = balance_info.get("deductAmount", 0)
                print(f"💰 [{self.server}] 当前余额: {result['balance']}捷停币 | 可抵扣: {result['deduct_amount']}元")

            result["success"] = True
            # 已删除：共获得0捷停币 相关输出
            print(f"✅ [{self.server}] 任务执行完成")

        except Exception as e:
            result["error"] = str(e)
            print(f"❌ [{self.server}] 执行异常 | 原因: {str(e)}")

        return result

# ===================== 主程序 =====================
async def main():
    print('===== 捷停车每日任务 =====\n')
    print(f"📅 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🔌 待执行账号: {len(SERVERS)} 个")
    print(f"🌐 代理模式: {'单账号独立代理' if ENABLE_PER_ACCOUNT_PROXY else '全局共用代理'}")
    print(f"📋 执行任务: 每日签到、浏览找优惠(T01)、浏览车位优选(T47)")
    print(f"🔧 自动适配: 接口要求的停留时间 | 代理自动重连已启用\n")

    results = []
    for index, server in enumerate(SERVERS):
        # 为每个账号创建独立的代理管理器
        proxy_manager = ProxyManager(server)
        if ENABLE_PER_ACCOUNT_PROXY and PROXY_API:
            await proxy_manager.get_proxy()
            await sleep(PROXY_FETCH_INTERVAL)

        async with JtcBot(server, proxy_manager) as bot:
            result = await bot.run()
            update_from_result(server, result, app_id=APPID)
            results.append(result)

        if index < len(SERVERS) - 1:
            print(f"\n⏳ 等待2秒后执行下一个账号...")
            await sleep(2000)

    # 汇总结果
    notify_content = " 捷停车每日任务执行结果\n"
    notify_content += f"\n📅 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    notify_content += f"\n📋 执行任务: 每日签到、浏览找优惠(T01)、浏览车位优选(T47)\n"

    for res in results:
        notify_content += f"\n {res['server']} ({res['phone']})\n"
        notify_content += f"- 代理状态：{res['proxy_status']}\n"
        notify_content += f"- 执行状态：{'成功' if res['success'] else '失败'}\n"
        if res['success']:
            notify_content += f"- 签到结果：{res['sign_msg']}\n"
            notify_content += f"- 当前余额：{res['balance']}捷停币\n"
            notify_content += f"- 可抵扣金额：{res['deduct_amount']}元\n"
        else:
            notify_content += f"- 失败原因：{res['error']}\n"

    await send_plusplus_notification("捷停车每日任务完成", notify_content)

    print('\n' + '='*40)
    print('🎉 所有账号执行完成')
    print(f"📊 成功: {sum(1 for r in results if r['success'])}/{len(results)} 个")
    print(f"💰 今日总获得: {sum(r['total_reward'] for r in results if r['success'])} 捷停币")
    print('='*40)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断执行")
    except Exception as e:
        print(f"\n❌ 程序异常: {str(e)}")
