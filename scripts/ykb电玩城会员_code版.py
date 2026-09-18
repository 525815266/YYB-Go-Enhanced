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


"""ykb_huiyuan 电玩城会员统一动态 code 版

功能：
  1. 四端口本地服务获取微信 code（按每个小程序的 appid 分别取）
  2. 覆盖 58 个同体系电玩城小程序（爱游乐/宝贝王/酷玩空间/大玩家等）
  3. 会员登录（appletLogin 两套接口自动降级，Bearer token）
  4. 会员签到（GetDetail 判断 → Submit），失败自动降级到任务签到
  5. 查询资产（代币/金币/积分/彩票/优惠券）
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

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
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests

APP_NAME = "ykb电玩城会员统一"
APPID = "wxf133aa0a4f191ffc"

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

LOG_NAME = "ykb电玩城会员"
LOG_ICON = "🕹️"

API_BASE = "https://pw.gzych.vip"

APPS = [
    {"ck": "ayldf", "name": "爱游乐东方", "appid": "wxf133aa0a4f191ffc", "templateVersion": "game_2.32.0"},
    {"ck": "ayljz", "name": "爱游乐胶州", "appid": "wx52005ba8c71a756a", "templateVersion": "game_2.32.0"},
    {"ck": "aylpld", "name": "爱游乐蓬莱店", "appid": "wxb4afba83d5ceadae", "templateVersion": "game_2.31.0"},
    {"ck": "bbwjtqzylzx", "name": "宝贝王家庭亲子娱乐中心", "appid": "wx61935615c3edb2d0", "templateVersion": "game_2.24.3"},
    {"ck": "bbwcqncly", "name": "宝贝王重庆南川乐园", "appid": "wx8fef16e51ca130db", "templateVersion": "game_2.15.1"},
    {"ck": "bbwhhxp", "name": "宝贝王怀化溆浦店", "appid": "wxbb9a887cca8663c2", "templateVersion": "game_2.30.4"},
    {"ck": "cwkjjtyl", "name": "超玩空间家庭娱乐", "appid": "wx1c59744a3a6ffef8", "templateVersion": "game_2.30.4"},
    {"ck": "dewbl", "name": "第e玩+宝龙店", "appid": "wx88fd603ba1ae66a2", "templateVersion": "game_2.33.7"},
    {"ck": "dlbfhx", "name": "大连缤纷幻想", "appid": "wx78b4c1140cefdcb0", "templateVersion": "game_2.30.1"},
    {"ck": "ddmxly", "name": "迪迪冒险乐园", "appid": "wxcd4a69c8c70a5419", "templateVersion": "game_2.15.1"},
    {"ck": "ddmxlyjzd", "name": "迪迪冒险乐园玖洲道店", "appid": "wx70dceb5a4f6daa56", "templateVersion": "game_2.15.1"},
    {"ck": "dqjtyl", "name": "豆趣家庭娱乐中心", "appid": "wxc9a77835500632bc", "templateVersion": "game_2.23.3"},
    {"ck": "fcgxmcws", "name": "防城港星梦潮玩社", "appid": "wx6683f77a42e1595e", "templateVersion": "game_2.30.4"},
    {"ck": "fckwjnhdmly", "name": "丰城市酷玩嘉年华动漫乐园", "appid": "wxb8739c52e9702339", "templateVersion": "game_2.20.6"},
    {"ck": "fmwg", "name": "飞马王国", "appid": "wx9703d60d09a566e7", "templateVersion": "game_2.15.1"},
    {"ck": "fxjnh", "name": "纷享X嘉年华", "appid": "wxcdbcf18be89c4d47", "templateVersion": "game_2.15.1"},
    {"ck": "hblytx", "name": "湖北乐游天下", "appid": "wxf65bcc2eb8c4eeff", "templateVersion": "game_2.16.1"},
    {"ck": "hlsglc", "name": "欢乐拾光乐昌店", "appid": "wx5f972a9b4cd13634", "templateVersion": "game_2.24.6"},
    {"ck": "jddzdtw", "name": "机动地带电玩城", "appid": "wx0e5c8715d36d15e6", "templateVersion": "game_2.30.4"},
    {"ck": "jqdw", "name": "鲸奇电玩中心", "appid": "wxa6dc5cc49b137460", "templateVersion": "game_2.30.1"},
    {"ck": "jsdwj", "name": "九顺大玩家", "appid": "wxf99906d27f33fc1d", "templateVersion": "game_2.30.2"},
    {"ck": "kwkjshhqg", "name": "酷玩空间上海环球港店", "appid": "wxb08bbd1dd73a27ab", "templateVersion": "game_2.32.0"},
    {"ck": "kwkjshsjh", "name": "酷玩空间上海世纪汇店", "appid": "wxd1ca01877cfd33eb", "templateVersion": "game_2.33.3"},
    {"ck": "kwxqjbel", "name": "酷玩猩球金宝二楼店", "appid": "wx405ee9e38f6a2038", "templateVersion": "game_2.30.2"},
    {"ck": "kwxqjbsl", "name": "酷玩猩球金宝三楼店", "appid": "wxf79a0dc11791d955", "templateVersion": "game_2.30.4"},
    {"ck": "lmrjxdqxg", "name": "蓝梦日记炫动柒栖谷店", "appid": "wxa23df268c16943d7", "templateVersion": "game_2.33.5"},
    {"ck": "lqclnd", "name": "乐其城柳南店", "appid": "wx25a19136c041f337", "templateVersion": "game_2.33.7"},
    {"ck": "lqcwgcd", "name": "乐其潮玩广场店", "appid": "wx1f4ee737668cf5a1", "templateVersion": "game_2.33.7"},
    {"ck": "lqcwyl", "name": "乐其潮玩玉林店", "appid": "wxf0e4c545914bd807", "templateVersion": "game_2.33.7"},
    {"ck": "lzsczqcjl", "name": "柳州市城中区超级乐", "appid": "wxd4521567cc4b39c0", "templateVersion": "game_2.33.7"},
    {"ck": "mhdcwdw", "name": "梦幻岛潮玩电玩", "appid": "wx226c9fbd60679a74", "templateVersion": "game_2.15.1"},
    {"ck": "mhelhyzhd", "name": "萌孩儿乐园海洋振华店", "appid": "wx420705aa0369e3f9", "templateVersion": "game_2.28.0"},
    {"ck": "mqyyc", "name": "米其游艺城", "appid": "wx0ce00dae4a87ad54", "templateVersion": "game_2.30.1"},
    {"ck": "mcyxcw", "name": "麻涌嬉游潮玩家庭娱乐中心", "appid": "wx4d303dc69f029e4e", "templateVersion": "game_2.30.2"},
    {"ck": "mydyl", "name": "梦游岛娱乐", "appid": "wxb7ad7be346c73a5e", "templateVersion": "game_2.30.4"},
    {"ck": "phtkzcwy", "name": "平湖天空之城吾悦", "appid": "wx387274288d2dcbc2", "templateVersion": "game_2.15.1"},
    {"ck": "ppxjtczly", "name": "派派星家庭成长乐园", "appid": "wxce55874aeb73adc2", "templateVersion": "game_2.30.4"},
    {"ck": "qqyydmly", "name": "奇奇游艺动漫乐园", "appid": "wx17446921ce05b350", "templateVersion": "game_2.24.5"},
    {"ck": "qqyyly", "name": "奇奇游艺乐园", "appid": "wx1cb50cac06c8f487", "templateVersion": "game_2.30.1"},
    {"ck": "rdsgdlhq", "name": "热带时光大沥黄岐店", "appid": "wxb1993ab85850dc69", "templateVersion": "game_2.30.4"},
    {"ck": "rdsghp", "name": "热带时光黄埔店", "appid": "wxa5ad3ec98ea33db0", "templateVersion": "game_2.30.1"},
    {"ck": "rdsgsz", "name": "热带时光家庭娱乐中心深圳店", "appid": "wxc6b29b0eaf5331b8", "templateVersion": "game_2.27.0"},
    {"ck": "sgcwly", "name": "拾光潮玩乐园", "appid": "wxbffd1cdbe3f11d33", "templateVersion": "game_2.23.3"},
    {"ck": "sgsscw", "name": "拾光松鼠潮玩店", "appid": "wx2823161452551c9e", "templateVersion": "game_2.29.3"},
    {"ck": "tdxly", "name": "泰迪熊乐园", "appid": "wx5cfc933a4a2a93f4", "templateVersion": "game_2.30.2"},
    {"ck": "topwjycccpark", "name": "TOP玩家 银川cc park店", "appid": "wxc86c913d8d9a4ab4", "templateVersion": "game_2.15.1"},
    {"ck": "wjfb", "name": "玩家风暴", "appid": "wx27625bb2d9a8384e", "templateVersion": "game_2.15.1"},
    {"ck": "xjddw", "name": "X机地电玩", "appid": "wx1652d4e11cc0a1c2", "templateVersion": "game_2.23.3"},
    {"ck": "xcdwhtgcd", "name": "猩潮电玩恒天广场店", "appid": "wx2ad6d80d65b237af", "templateVersion": "game_2.19.3"},
    {"ck": "xnhhwdbbw", "name": "西宁海湖万达宝贝王", "appid": "wx7a3335dc207999f3", "templateVersion": "game_2.27.0"},
    {"ck": "xqlddylc", "name": "享区乐到底游乐场", "appid": "wx070d007b211099d9", "templateVersion": "game_2.24.5"},
    {"ck": "ybxwcw", "name": "月伴星玩潮玩店", "appid": "wxb5e0812b9a0ccc8c", "templateVersion": "game_2.30.4"},
    {"ck": "ykb", "name": "太空橙电玩城", "appid": "wxcd8a2d92245ee75b", "templateVersion": "game_2.30.4"},
    {"ck": "ylsyzqqjwlc", "name": "玉林市玉州区奇迹未来城潮漫电玩", "appid": "wx4e02b4d9520d6df1", "templateVersion": "game_2.28.0"},
    {"ck": "ylycmdwd", "name": "壹零壹潮漫电玩店", "appid": "wx49deb1f94bab5091", "templateVersion": "game_2.30.4"},
    {"ck": "yyhwqldcw", "name": "酉阳红卫桥乐动潮玩", "appid": "wx263d7c4bbca7feca", "templateVersion": "game_2.30.2"},
    {"ck": "zbbbwl", "name": "重百宝贝王乐园", "appid": "wx9dc5d73cb2d62bdc", "templateVersion": "game_2.30.4"},
    {"ck": "zjmxwt", "name": "终极梦想沃特", "appid": "wx733a59c79a4bb702", "templateVersion": "game_2.31.0"},
]

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ykballcookie.json")

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



def month_range() -> Dict[str, int]:
    now = datetime.now()
    first_day = int(datetime(now.year, now.month, 1).timestamp() * 1000)
    if now.month == 12:
        next_month = int(datetime(now.year + 1, 1, 1).timestamp() * 1000)
    else:
        next_month = int(datetime(now.year, now.month + 1, 1).timestamp() * 1000)
    return {"BeginDate": first_day - 15 * 24 * 60 * 60 * 1000,
            "EndDate": next_month - 1000 + 15 * 24 * 60 * 60 * 1000}


STATUS_MAP = {"Completed": "Complete", "Failed": "Fail", "Going": "Going",
              "Complete": "Complete", "Fail": "Fail"}


def normalize_status(status: Any) -> str:
    return STATUS_MAP.get(str(status or ""), str(status or ""))


def first_value(source: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if source.get(key) is not None:
            return source[key]
    return 0


class AccountSession:
    """单个 code 服务地址的会话: token（Bearer）"""

    def __init__(self, server: str):
        self.server = server
        self.token = ""
        self.customer_name = ""
        self.open_id = ""

    def app_request(self, app: Dict[str, Any], method: str, api_path: str,
                    params: Dict[str, Any] | None = None, data: Dict[str, Any] | None = None,
                    skip_token: bool = False, proxies: Dict[str, str] | None = None) -> Any:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"https://servicewechat.com/{app['appid']}/1/page-frame.html",
            "Accept": "application/json, text/plain, */*",
            "content-type": "application/json",
        }
        if self.token and not skip_token:
            headers["Authorization"] = self.token
        kwargs: Dict[str, Any] = {"headers": headers, "timeout": REQUEST_TIMEOUT}
        if proxies:
            kwargs["proxies"] = proxies
        url = f"{app.get('apiBase') or API_BASE}{api_path if api_path.startswith('/') else '/' + api_path}"
        if method == "GET":
            kwargs["params"] = params or {}
            try:
                response = requests.get(url, **kwargs)
            except Exception:
                if not proxies or not ENABLE_DIRECT_FALLBACK:
                    raise
                kwargs.pop("proxies", None)
                response = requests.get(url, **kwargs)
        else:
            kwargs["json"] = data or {}
            try:
                response = requests.post(url, **kwargs)
            except Exception:
                if not proxies or not ENABLE_DIRECT_FALLBACK:
                    raise
                kwargs.pop("proxies", None)
                response = requests.post(url, **kwargs)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        result = response.json()
        response_status = result.get("ResponseStatus") if isinstance(result, dict) else None
        if response_status:
            if int(response_status.get("ErrorCode") or 0) != 0:
                raise RuntimeError(f"{response_status.get('Message') or json_preview(result, 150)}({response_status.get('ErrorCode')})")
            return result.get("Data")
        if isinstance(result, dict) and result.get("code") not in (None, 0, "0", 200, "200"):
            raise RuntimeError(result.get("msg") or result.get("message") or json_preview(result, 150))
        if isinstance(result, dict):
            if result.get("Data") is not None:
                return result["Data"]
            if result.get("data") is not None:
                return result["data"]
        return result

    def login_app(self, app: Dict[str, Any], proxies: Dict[str, str] | None) -> bool:
        """单个小程序: 取 code → 两套 appletLogin 接口降级尝试。成功返回 True"""
        login_apis = [
            "/ykb_huiyuan/api/v3/MembeGameLogin/appletLogin",
            "/ykbmini/api/v1/HomeAny/appletLogin",
        ]
        for api_path in login_apis:
            try:
                code = get_code_for(server=self.server, appid=app["appid"])
                if not code:
                    return False
                data = self.app_request(app, "POST", api_path, skip_token=True, data={
                    "Code": code,
                    "AppId": app["appid"],
                    "WechatVersion": "3.9.12",
                    "MobilePlatform": "windows",
                    "MallCode": "",
                    "TemplateVersion": app.get("templateVersion") or "",
                    "extra": {"q": ""},
                }, proxies=proxies)
                data = data if isinstance(data, dict) else {}
                self.token = str(data.get("token") or data.get("Token") or "")
                self.open_id = str(data.get("openId") or data.get("OpenId") or "")
                self.customer_name = str(data.get("customerName") or data.get("CustomerName") or "")
                if not self.token:
                    raise RuntimeError(f"登录响应无Token: {json_preview(data, 150)}")
                return True
            except Exception as exc:
                print(f"⚠️ [{app['name']}] 登录接口 {api_path} 失败: {exc}")
        return False

    def get_member_info(self, app: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
        try:
            data = self.app_request(app, "GET", "/ykb_huiyuan/api/v1/MemberMine/Info", proxies=proxies)
            data = data if isinstance(data, dict) else {}
            name = data.get("Name") or data.get("NickName") or self.customer_name or "未知"
            assets = self.get_assets(app, data, proxies)
            return f"{name} 代币={assets['coin']} 金币={assets['goldCoin']} 积分={assets['integral']} 彩票={assets['ticket']} 优惠券={assets['coupon']}"
        except Exception as exc:
            return f"查询失败({exc})"

    def get_assets(self, app: Dict[str, Any], member: Dict[str, Any],
                   proxies: Dict[str, str] | None) -> Dict[str, Any]:
        assets = {"coin": 0, "goldCoin": 0, "integral": 0, "ticket": 0, "coupon": 0}
        source = member if isinstance(member, dict) else {}
        assets["coin"] = first_value(source, ["BalanceNum", "MyScrip", "Coin", "CoinAmount", "Scrip", "Balance", "StoredCoin", "GameCoin"])
        assets["goldCoin"] = first_value(source, ["GoldCoin", "GoldCoinAmount"])
        assets["integral"] = first_value(source, ["ARTotalIntegral", "Integral", "Exchange", "Points", "Point"])
        assets["ticket"] = first_value(source, ["Ticket", "TicketPackage", "TicketCount", "Lottery", "LotteryTicket"])
        assets["coupon"] = first_value(source, ["Coupon", "CouponCount"])
        try:
            data = self.app_request(app, "GET", "/ykb_huiyuan/api/v1/Member/GetMemberStoredValue", proxies=proxies)
            data = data if isinstance(data, dict) else {}
            lists = []
            for key in ("List", "Data", "LeaguerValues"):
                value = data.get(key)
                if isinstance(value, list):
                    lists.extend(value)
            if isinstance(data.get("Data"), dict) and isinstance(data["Data"].get("LeaguerValues"), list):
                lists.extend(data["Data"]["LeaguerValues"])
            for item in lists:
                if not isinstance(item, dict):
                    continue
                type_text = str(item.get("Equity") or item.get("Type") or item.get("StoredValueType")
                                or item.get("StoreCategory") or item.get("Category") or item.get("Name")
                                or item.get("Title") or item.get("Key") or "")
                amount = first_value(item, ["BalanceNum", "Balance", "AllAmount", "Amount", "Num", "Count", "Value", "Total", "Available", "StoredValue"])
                if __import__("re").search(r"GoldCoin|金币", type_text, __import__("re").IGNORECASE):
                    assets["goldCoin"] = amount
                elif __import__("re").search(r"Ticket|TicketPackage|彩票|票", type_text, __import__("re").IGNORECASE):
                    assets["ticket"] = amount
                elif __import__("re").search(r"Coupon|优惠券|券", type_text, __import__("re").IGNORECASE):
                    assets["coupon"] = amount
                elif __import__("re").search(r"Integral|Point|积分|Exchange", type_text, __import__("re").IGNORECASE):
                    assets["integral"] = amount
                elif __import__("re").search(r"Coin|代币|MyScrip|币", type_text, __import__("re").IGNORECASE):
                    assets["coin"] = amount
        except Exception as exc:
            print(f"⚠️ [资产] 查询储值资产失败: {exc}")
        return assets

    def try_member_check_in(self, app: Dict[str, Any], proxies: Dict[str, str] | None) -> "bool | None":
        """会员签到；接口不可用返回 None 并降级任务签到"""
        try:
            detail = self.app_request(app, "GET", "/ykb_huiyuan/api/v1/MemberCheckIn/GetDetail",
                                      params=month_range(), proxies=proxies)
            detail = detail if isinstance(detail, dict) else {}
            daily = next((item for item in (detail.get("RewardRules") or [])
                          if isinstance(item, dict) and item.get("CycleType") == "Daily"), None)
            daily_text = f"{daily['Amount']}{daily.get('RewardAlias') or ''}" if daily else "未知"
            print(f"📋 [签到] 会员签到状态: 已连续{detail.get('Days') or 0}天 今日奖励={daily_text}")
            if detail.get("IsCheckIn"):
                print("✅ [签到] 今日已签到")
                return True
            result = self.app_request(app, "GET", "/ykb_huiyuan/api/v1/MemberCheckIn/Submit", proxies=proxies)
            after = self.app_request(app, "GET", "/ykb_huiyuan/api/v1/MemberCheckIn/GetDetail",
                                     params=month_range(), proxies=proxies)
            msg = f"签到成功 signed={bool((after if isinstance(after, dict) else {}).get('IsCheckIn'))}"
            if isinstance(result, dict) and result.get("Message"):
                msg += f": {result['Message']}"
            print(f"✅ [签到] {msg}")
            return True
        except Exception as exc:
            print(f"⚠️ [签到] 会员签到接口不可用，改用任务签到: {exc}")
            if __import__("re").search(r"登录|授权|token|非法", str(exc), __import__("re").IGNORECASE):
                self.token = ""
            return None

    def task_sign(self, app: Dict[str, Any], proxies: Dict[str, str] | None) -> str:
        try:
            data = self.app_request(app, "GET", "/hdb/api/v1/ClientTask/GetTaskListFromYDG",
                                    params={"TaskType": "AllTask"}, proxies=proxies)
            tasks = (data or {}).get("List") if isinstance(data, dict) else None
            tasks = tasks or (data if isinstance(data, list) else [])
            sign_tasks = [t for t in tasks if isinstance(t, dict) and __import__("re").search(
                r"签到|每日|天天|登录|打卡|check.?in|sign",
                " ".join(str(t.get(k) or "") for k in ("TaskName", "Name", "Remark", "Title", "TaskType", "TypeName")),
                __import__("re").IGNORECASE)]
            if not sign_tasks:
                print("⚠️ [签到] 未找到签到/每日任务")
                return "未找到签到/每日任务"
            texts = []
            for task in sign_tasks:
                texts.append(self.handle_task(app, task, proxies))
            return " | ".join(texts)
        except Exception as exc:
            return f"任务签到失败({exc})"

    def handle_task(self, app: Dict[str, Any], task: Dict[str, Any],
                    proxies: Dict[str, str] | None) -> str:
        name = task.get("TaskName") or task.get("Name") or task.get("TaskID") or "未知任务"
        detail = self.get_task_detail(app, task, proxies)
        detail = detail if isinstance(detail, dict) else {}
        status = normalize_status(detail.get("Status"))
        print(f"📋 [任务] 「{name}」状态={status or '未知'}")
        if status == "Complete":
            if detail.get("RewardReceiveStatus") == "UnReceive":
                return self.receive_task(app, detail, task, proxies)
            return f"任务已完成: {name}"
        if not task.get("TaskID"):
            return f"任务{name}无TaskID"
        challenge = self.app_request(app, "POST", "/hdb/api/v1/ClientTask/ReceiveTask",
                                     data={"ID": task.get("TaskID")}, proxies=proxies)
        challenge = challenge if isinstance(challenge, dict) else {}
        user_task_id = challenge.get("UserTaskId") or challenge.get("UserTaskID")
        if user_task_id:
            detail = self.get_task_detail(app, {"UserTaskID": user_task_id}, proxies)
            detail = detail if isinstance(detail, dict) else {}
            if normalize_status(detail.get("Status")) == "Complete" and detail.get("RewardReceiveStatus") == "UnReceive":
                return self.receive_task(app, detail, {**task, "UserTaskID": user_task_id}, proxies)
            return f"任务状态={normalize_status(detail.get('Status')) or '未知'}"
        return f"任务{name}处理完成"

    def get_task_detail(self, app: Dict[str, Any], task: Dict[str, Any],
                        proxies: Dict[str, str] | None) -> Any:
        if task.get("UserTaskID"):
            return self.app_request(app, "GET", "/hdb/api/v1/ClientTask/GetUserTaskDetail",
                                    params={"UserTaskID": task["UserTaskID"]}, proxies=proxies)
        return self.app_request(app, "GET", "/hdb/api/v1/ClientTask/GetTaskDetail",
                                params={"TaskID": task.get("TaskID")}, proxies=proxies)

    def receive_task(self, app: Dict[str, Any], detail: Dict[str, Any], task: Dict[str, Any],
                     proxies: Dict[str, str] | None) -> str:
        user_task_id = detail.get("UserTaskId") or detail.get("UserTaskID") or task.get("UserTaskID") or ""
        if not user_task_id:
            return "缺少 UserTaskId"
        data = self.app_request(app, "POST", "/hdb/api/v1/ClientTask/ReceiveTaskRewards",
                                data={"UserTaskId": user_task_id}, proxies=proxies)
        rewards = (data if isinstance(data, dict) else {}).get("Rewards") or (data if isinstance(data, dict) else {}).get("RewardList") or []
        text = "、".join(f"{r.get('Amount', '')}{r.get('Name') or r.get('RewardName') or ''}" for r in rewards if isinstance(r, dict))
        print(f"🎁 [任务] 领取任务奖励成功{text and (': ' + text)}")
        return f"奖励已领取{text and (': ' + text)}"


def get_code_for(server: str, appid: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] {url} appId={appid}")
    try:
        response = direct_session().get(url, params={"appId": appid}, timeout=20)
        data = response.json()
        code_val = str(data.get("code") or "")
        status = data.get("status")
        if data.get("err") != 0 or not code_val or code_val == "null" or code_val == "invalid" or (status is not None and status != "ok"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


EMPTY_RESULT = {
    "server": "-",
    "success": False,
    "proxyStatus": "未使用代理",
    "proxyIp": "-",
    "signSummary": "-",
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
    lines = []
    ok_count = 0
    login_fail = 0

    for app in APPS:
        app["apiBase"] = API_BASE
        print(f"🕹️ [{app['name']}] 开始处理")
        try:
            if not session.login_app(app, proxies):
                login_fail += 1
                lines.append(f"{app['name']}: 登录失败")
                continue
            result["token"] = mask(session.token)
            member_msg = session.get_member_info(app, proxies)
            member_check = session.try_member_check_in(app, proxies)
            if member_check is None:
                sign_msg = session.task_sign(app, proxies)
            else:
                sign_msg = "会员签到流程完成"
            ok = "签到成功" in sign_msg or "已签到" in sign_msg or "奖励已领取" in sign_msg
            if ok:
                ok_count += 1
            lines.append(f"{app['name']}: {sign_msg} ({member_msg})")
        except Exception as exc:
            login_fail += 1
            lines.append(f"{app['name']}: 异常({exc})")
        sleep(random.randint(1, 2))

    result["signSummary"] = f"{len(APPS)} 个小程序: 成功{ok_count} / 登录失败{login_fail}"
    result["_lines"] = lines
    result["success"] = ok_count > 0
    print(f"🏁 [汇总] {result['signSummary']}")
    return result
def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🕹️ ykb电玩城会员四账号任务结果

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
🕹️ 汇总：{res["signSummary"]}
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
            results.append({**EMPTY_RESULT, "server": server, "error": traceback.format_exc().strip()})

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ 🏁 ykb电玩城会员任务执行完成          ║")
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
