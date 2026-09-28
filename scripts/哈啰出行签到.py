#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 哈啰出行签到

"""
哈啰出行签到小程序 YYB Go 动态 code 版

功能：
  1. YYB Go 多账号获取微信 code
  2. /api?user.account.weixinEasyLogin 使用 code 换 token
  3. 每日签到（common.welfare.signAndRecommend）
  4. 按服务端状态领取普通宝箱，冷却/上限/进度不变即停止
  5. 分别查询免费骑车视频、本日奖励金视频、宝箱附带视频进度
     领取真实已完成任务的奖励；普通宝箱不等于免费骑车视频
  5. 查询金币余额（user.taurus.pointInfo）
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER        YYB Go 多账号，每行格式：地址@账号ID或OpenID
  YYB_ACCOUNT_NAMES 账号备注，可选，按账号顺序每行一个
  YD_CODE_SERVERS   旧版 code 服务，可选，仅未配置 YYB_SERVER 时使用
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  HELLO_BOX_WAIT_SECONDS 普通宝箱单轮最多等待秒数，默认0（最长60）
  HELLO_CITY_CODE    福利中心城市区号，可选
  HELLO_TASK_MAX_ROUNDS 普通任务领奖安全循环上限
  HELLO_ACCOUNT_LIMIT 临时验证时限制本次处理账号数，0或未设置表示全部

视频需在小程序实际播放。本脚本不模拟广告完成、不重放HAR中的广告凭据。
上方视频按活动周期计数，下方按当天阶段计数；不固定为5次、20次或500次。

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, OSError, TypeError):
    pass


APP_NAME = "哈啰出行小程序"
APPID = "wxb937e3d0b3ca117e"

YYB_SERVER_ENV = os.getenv("YYB_SERVER", "").strip()
YYB_ACCOUNT_NAMES = [item.strip() for item in os.getenv("YYB_ACCOUNT_NAMES", "").splitlines()]


def load_code_sources() -> List[str]:
    if YYB_SERVER_ENV:
        return [
            item.strip()
            for item in re.split(r"[\r\n&]+", YYB_SERVER_ENV)
            if item.strip() and item.strip() != "[object Object]"
        ]
    legacy = os.getenv("YD_CODE_SERVERS", "127.0.0.1:8088")
    return [item.strip() for item in re.split(r"[\r\n&,]+", legacy) if item.strip()]


SERVERS = load_code_sources()

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://api.hellobike.com/api"
MARKETING_URL = "https://marketingapi.hellobike.com/api"
LOGIN_URL = f"{BASE_URL}?user.account.weixinEasyLogin"
SIGN_URL = f"{BASE_URL}?common.welfare.signAndRecommend"
BOX_URL = f"{BASE_URL}?common.welfare.open.treasure.box"
POINT_URL = f"{BASE_URL}?user.taurus.pointInfo"
TASK_QUERY_ACTION = "common.welfare.taurus.task"
TASK_CLAIM_ACTION = "mars.offer.receiveAward"
TASK_SCENE = "platform_points"
TASK_SUB_SCENE = "platform_points_pointshp"

SIGN_PAYLOAD = {
    "from": "h5",
    "systemCode": 62,
    "platform": 4,
    "version": "6.72.1",
    "action": "common.welfare.signAndRecommend",
}

BOX_PAYLOAD = {
    "from": "h5",
    "systemCode": 62,
    "platform": 4,
    "version": "7.0.15",
    "action": "common.welfare.open.treasure.box",
    "cityCode": "021",
    "boxType": 1,
}

POINT_PAYLOAD = {
    "from": "h5",
    "systemCode": 62,
    "platform": 4,
    "version": "6.72.1",
    "action": "user.taurus.pointInfo",
    "pointType": 1,
}

WELFARE_PAYLOAD = {
    "from": "h5", "systemCode": 64, "platform": 2, "version": "7.0.30",
}
if os.getenv("HELLO_CITY_CODE"):
    WELFARE_PAYLOAD["cityCode"] = os.environ["HELLO_CITY_CODE"]
    BOX_PAYLOAD["cityCode"] = os.environ["HELLO_CITY_CODE"]

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 15; PKX110 Build/AP3A.240617.008; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.178 "
    "Mobile Safari/537.36 XWEB/1460249 MMWEBSDK/20240301 MMWEBID/8694 "
    "MicroMessenger/8.0.48.2580(0x28003035) WeChat/arm64 Weixin "
    "NetType/5G Language/zh_CN ABI/arm64 miniProgram/" + APPID
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


def bounded_env_int(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        print(f"⚠️ [配置] {name} 不是整数，使用默认值 {default}")
        return default
    return min(upper, max(lower, value))


def log_title(total: int | None = None) -> None:
    total = len(SERVERS) if total is None else total
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🚲 哈啰出行签到动态 code 版                 ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {total:<34}║")
    print("╚" + "═" * 50 + "╝")


def account_label(index: int, source: str) -> str:
    ref = source.rsplit("@", 1)[-1] if "@" in source else source
    name = YYB_ACCOUNT_NAMES[index - 1] if index <= len(YYB_ACCOUNT_NAMES) else ""
    return f"{name}（{ref}）" if name else ref


def log_account_header(index: int, total: int, server: str) -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 👤 标识 {account_label(index, server):<40}│")
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


def get_code(source: str) -> str | None:
    if "@" not in source:
        url = f"http://{source}/login"
        print(f"🔐 [授权] 请求旧版 code 服务: {url}")
        try:
            response = direct_session().get(url, params={"appId": APPID}, timeout=20)
            data = response.json()
            if data.get("err") != 0 or not data.get("code"):
                print(f"❌ [授权] code 获取失败: {json_preview(data)}")
                return None
            print("✅ [授权] code 获取成功")
            return data["code"]
        except Exception as exc:
            print(f"❌ [授权] code 获取异常: {exc}")
            return None

    server, ref = source.split("@", 1)
    server, ref = server.strip().rstrip("/"), ref.strip()
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    url = f"{server}/wxapp/getCode"
    print(f"🔐 [授权] 请求 YYB Go code: {server}，账号={ref}")

    try:
        response = direct_session().post(url, json={"ref": ref, "app_id": APPID}, timeout=20)
        data = response.json()
        code = (((data.get("data") or {}).get("result") or {}).get("code"))

        if response.status_code != 200 or str(data.get("code")) != "0" or not code:
            message = data.get("message") or data.get("msg") or json_preview(data)
            print(f"❌ [授权] YYB Go 获取 code 失败: HTTP {response.status_code}，{message}")
            return None

        print("✅ [授权] YYB Go 获取 code 成功")
        return str(code)
    except Exception as exc:
        print(f"❌ [授权] YYB Go 获取 code 异常: {exc}")
        return None


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/824/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["token"] = token
    return headers


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


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        payload = {
            "riskControlData": {
                "systemCode": 64,
                "network": "5g",
                "deviceLon": 121.74488362630208,
                "deviceLat": 31.02707302517361,
                "batteryLevel": "32",
                "openId": "",
                "unionId": "",
            },
            "version": "7.0.15",
            "releaseVersion": "7.0.15",
            "systemCode": "64",
            "appName": "AppHellobikeWXSS",
            "mobileModel": "PKX110",
            "weChatVersion": "8.0.48",
            "mobileSystem": "Android 15",
            "SDKVersion": "3.3.5",
            "systemPlatform": "android",
            "from": "wechat",
            "action": "user.account.weixinEasyLogin",
            "iv": None,
            "wechatLoginCode": code,
            "pageName": "pages/personal/index/index",
            "encryptedData": None,
            "city": "",
            "adCode": "",
            "cityCode": "",
            "channel": 0,
            "longitude": 121.74488362630208,
            "latitude": 31.02707302517361,
            "flagType": "WECHAT_SEAMLESS",
            "extendValue": json.dumps({"openId": ""}),
            "ssid": "5p3269gK0sd5Zcp_2026-01-01",
        }
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json=payload,
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = dict(payload)
    body["token"] = token

    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(),
        json=body,
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


def checked_data(response: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(response, dict):
        raise RuntimeError("接口返回格式异常")
    if response.get("code") != 0 or response.get("success") is False:
        raise RuntimeError(f"code={response.get('code')}，{response.get('msg') or response.get('message') or '请求失败'}")
    data = response.get("data")
    if not isinstance(data, dict) or not data:
        raise RuntimeError("接口未返回有效状态，不能当作已完成或冷却")
    return data


def read_points(server, token, proxies):
    print("[余额] 请求 user.taurus.pointInfo")
    try:
        data = checked_data(api_post(server, POINT_URL, token, proxies, POINT_PAYLOAD))
        points = float(data["points"])
        print(f"[余额] 服务端返回 {points:g} 金币")
        return points
    except Exception as exc:
        print(f"⚠️ [余额] 查询失败：{exc}")
        return None


def box_stop_reason(data, now):
    count, limit = data.get("todayOpenCount"), data.get("maxDailyCount")
    if count is not None and limit is not None and int(count) >= int(limit):
        return "今日普通宝箱已达上限", 0
    if data.get("show") is False:
        return "当前入口未开放", 0
    timestamp = to_float(data.get("nextAvailableTime"))
    if timestamp > 1e12:
        timestamp /= 1000
    if timestamp > now:
        return f"冷却至 {datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')}", timestamp - now
    if data.get("status") != "AVAILABLE":
        return f"状态 {data.get('status', '未知')}，等待下次调度核对", 0
    # canContinue控制宝箱视频，并不控制普通宝箱，不能用它当冷却条件。
    return "", 0


def run_boxes(server, token, proxies):
    payload = dict(BOX_PAYLOAD, action="common.welfare.query.treasure.box")
    payload.pop("boxType", None)
    wait_budget = min(60, max(0, to_float(os.getenv("HELLO_BOX_WAIT_SECONDS", "0"))))
    deadline = time.monotonic() + wait_budget
    claimed, reward, previous_count = 0, 0.0, None
    seen = set()
    last = {}
    reason = ""
    request_error = False
    # 这是防异常响应的单轮保护，不是活动每日上限。
    for _ in range(100):
        try:
            last = checked_data(api_post(server, BASE_URL, token, proxies, payload))
            reason, wait = box_stop_reason(last, time.time())
            if reason:
                if wait > 0 and wait + 1 <= deadline - time.monotonic():
                    sleep(wait + 1)
                    continue
                break
            count = last.get("todayOpenCount")
            if count is None or last.get("maxDailyCount") is None:
                reason = "缺少今日进度或上限，停止领取"
                break
            count = int(count)
            if count in seen or (previous_count is not None and count <= previous_count):
                reason = "服务端进度未增加，停止重复领取"
                break
            seen.add(count)
            previous_count = count
            sleep(random.randint(2, 5))
            opened = checked_data(api_post(server, BOX_URL, token, proxies, BOX_PAYLOAD))
            if opened.get("success") is not True:
                reason = f"领取未成功：{opened.get('status') or opened.get('buttonText') or '返回未确认'}"
                request_error = True
                break
            if int(opened.get("todayOpenCount", count)) <= count:
                reason = "领取响应未确认进度增加，停止并以余额核对"
                break
            claimed += 1
            reward += to_float(opened.get("rewardAmount"))
            print(f"[普通宝箱] 本轮第 {claimed} 次 +{opened.get('rewardAmount', '?')} 金币")
        except Exception as exc:
            reason = f"查询/领取失败：{exc}；未重试领取"
            request_error = True
            break
    else:
        reason = "达到单轮保护次数，等待下次调度"
    progress = f"今日 {last.get('todayOpenCount', '?')}/{last.get('maxDailyCount', '?')}"
    video = f"宝箱视频剩余 {last.get('remainingVideoCount', '?')}/{last.get('maxVideoCount', '?')}（需小程序播放）"
    summary = f"本轮 {claimed} 次 +{reward:g} 金币；{progress}；{reason}；{video}"
    if request_error:
        raise RuntimeError(summary)
    return summary


def period_video_summary(data):
    watched, limit = data.get("watchedCount"), data.get("maxPeriodCount")
    if watched is None or limit is None:
        return "服务端未返回本期观看次数/上限，不能判定不限次数"
    watched, limit = int(watched), int(limit)
    remaining = max(0, limit - watched)
    # 与官方页面 earnedReward 一致：各阶段每次奖励之和，不作本轮收益。
    stages = data.get("stageList") or []
    earned = 0.0
    for stage in stages:
        start, end = int(stage["startCount"]), int(stage["endCount"])
        earned += min(max(watched - start + 1, 0), end - start + 1) * float(stage["rewardAmount"])
    expiration = to_float(data.get("expireTimestamp")) / 1000
    expires = datetime.fromtimestamp(expiration).strftime("%m-%d %H:%M") if expiration else "未知"
    if expiration and expiration <= time.time():
        state = "本期已结束"
    elif data.get("status") == "INIT":
        state = "尚未接取活动；脚本仅查询进度"
    elif remaining == 0:
        state = "本期已达上限"
    else:
        state = "未返回轮次冷却时间；脚本仅查询进度"
    reward = f"累计奖励 {earned:g} 金币（按阶段规则计算）" if stages else "累计奖励未知"
    return f"本期已看 {watched}/{limit} 次，剩余 {remaining}；{reward}；到期 {expires}；{state}"


def query_ride_video_progress(server, token, proxies):
    payload = dict(WELFARE_PAYLOAD, action="common.welfare.queryAlipayLightVideo")
    print("[上方视频] 请求 common.welfare.queryAlipayLightVideo，仅读取服务端真实进度")
    data = checked_data(api_post(server, BASE_URL, token, proxies, payload))
    summary = period_video_summary(data)
    print(f"[上方视频] 服务端结果：{summary}")
    return summary


def query_video_progress(server, token, proxies):
    payload = dict(WELFARE_PAYLOAD, action="common.welfare.new.signInfo", newWelfare=True)
    print("[下方视频] 请求 common.welfare.new.signInfo，仅读取服务端真实进度")
    data = checked_data(api_post(server, BASE_URL, token, proxies, payload))
    videos = [m for m in data.get("moduleInfo", []) if m.get("moduleKey") == "welfare_advertisement_video"]
    if not videos:
        summary = "当前未返回视频模块，不当作已完成"
        print(f"[下方视频] 服务端结果：{summary}")
        return summary
    summaries = []
    for module in videos:
        info = module.get("information") or {}
        stages = info.get("stageOffers") or []
        if not stages:
            summaries.append("服务端未返回奖励阶段，上限未知")
            continue
        done = sum(s.get("offerStatus") == 3 for s in stages)
        remaining = len(stages) - done
        earned = sum(to_float(s.get("totalAmount")) for s in stages if s.get("offerStatus") == 3)
        state = "今日阶段均已完成" if not remaining else "脚本仅查询进度"
        summaries.append(f"今日已领奖 {done}/{len(stages)} 阶段，剩余 {remaining}；已领奖阶段合计 {earned:g} 金币；{state}")
    summary = "；".join(summaries)
    print(f"[下方视频] 服务端结果：{summary}")
    return summary


def task_query_payload() -> Dict[str, Any]:
    return dict(
        WELFARE_PAYLOAD,
        action=TASK_QUERY_ACTION,
        channelId=2,
        sceneNameEn=TASK_SCENE,
        subSceneNameEn=TASK_SUB_SCENE,
        taskStatusList=["INIT", "RUNNING", "FINISHED"],
        queryUnrewardedTasks=True,
        newWelfare=True,
    )


def task_label(task: Dict[str, Any]) -> str:
    name = task.get("taskName") or task.get("mainTitle") or task.get("taskCode") or "未命名任务"
    status = task.get("taskStatus", "未知")
    current = task.get("currentProgress", "?")
    total = task.get("totalProgress", "?")
    return f"{name}[{status} {current}/{total}]"


def task_snapshot(tasks: List[Dict[str, Any]]) -> Tuple[Tuple[str, str, str, str], ...]:
    return tuple(sorted(
        (
            str(task.get("taskGuid") or task.get("taskCode") or task.get("taskName") or ""),
            str(task.get("taskStatus") or ""),
            str(task.get("currentProgress") or ""),
            str(task.get("totalProgress") or ""),
        )
        for task in tasks
    ))


def query_normal_tasks(server, token, proxies) -> List[Dict[str, Any]]:
    print(f"[普通任务] 请求 {TASK_QUERY_ACTION}，查询 INIT/RUNNING/FINISHED")
    data = checked_data(api_post(server, BASE_URL, token, proxies, task_query_payload()))
    groups = data.get("taskGroupInfoDTOS")
    if not isinstance(groups, list):
        raise RuntimeError("任务列表缺失，无法核验服务端状态")

    tasks = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for task in group.get("taskCouponList") or []:
            if isinstance(task, dict):
                tasks.append(task)

    print(f"[普通任务] 服务端返回 {len(tasks)} 项")
    for task in tasks:
        print(f"[普通任务] 进度：{task_label(task)}")
    return tasks


def task_action_outcome(response: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(response, dict):
        return False, "响应格式异常"
    code = response.get("code")
    if code not in (0, "0"):
        return False, f"code={code}，{response.get('msg') or response.get('message') or '请求失败'}"
    if response.get("success") is False:
        return False, f"success=false，{response.get('msg') or response.get('message') or '请求失败'}"
    data = response.get("data")
    if data is False or (isinstance(data, dict) and data.get("success") is False):
        return False, "服务端 data=false"
    return True, f"code={code}"


def task_action_delay() -> None:
    delay = random.randint(1, 2)
    print(f"[普通任务] 动作后等待 {delay}s，再复查服务端")
    sleep(delay)


def _task_by_guid(tasks: List[Dict[str, Any]], guid: str) -> Dict[str, Any] | None:
    return next((task for task in tasks if str(task.get("taskGuid")) == str(guid)), None)


def collect_finished_tasks(server, token, proxies):
    """领取真实 FINISHED 任务；INIT/RUNNING 只展示，不伪造业务动作。"""
    max_rounds = bounded_env_int("HELLO_TASK_MAX_ROUNDS", 20, 1, 50)
    tasks = query_normal_tasks(server, token, proxies)
    failed_guids = set()
    failures = []
    claimed_count = 0
    stop_reason = "未设置"

    for round_no in range(1, max_rounds + 1):
        finished = [
            task for task in tasks
            if task.get("taskStatus") == "FINISHED"
            and task.get("taskGuid")
            and task.get("taskGroupCode")
            and str(task.get("taskGuid")) not in failed_guids
        ]
        if finished:
            task = finished[0]
            guid = str(task["taskGuid"])
            before_points = read_points(server, token, proxies)
            if before_points is None:
                failures.append(f"领取 {task_label(task)} 前余额不可核验")
                failed_guids.add(guid)
                continue

            claim = dict(
                WELFARE_PAYLOAD,
                action=TASK_CLAIM_ACTION,
                channelId=2,
                taskGuid=guid,
                taskGroupCode=task["taskGroupCode"],
                version=WELFARE_PAYLOAD["version"],
            )
            print(f"[普通任务] 领取 {task_label(task)} -> {TASK_CLAIM_ACTION}")
            response = api_post(server, MARKETING_URL, token, proxies, claim)
            action_ok, action_msg = task_action_outcome(response)
            print(f"[普通任务] 领取服务端响应：{action_msg}")
            task_action_delay()

            try:
                after_tasks = query_normal_tasks(server, token, proxies)
                after_points = read_points(server, token, proxies)
            except Exception as exc:
                failures.append(f"领取 {task_label(task)} 后复查失败：{exc}")
                failed_guids.add(guid)
                stop_reason = "领取后无法同时复查任务和余额，停止避免假报"
                break

            after_task = _task_by_guid(after_tasks, guid)
            still_pending = after_task is not None and after_task.get("taskStatus") == "FINISHED"
            delta = None if after_points is None else after_points - before_points
            print(
                f"[普通任务] 领取后任务状态：{task_label(after_task) if after_task else '服务端列表已移除'}"
            )
            print(
                f"[普通任务] 领取后余额：{before_points:g} -> "
                f"{after_points:g}，变化 {delta:+g} 金币" if after_points is not None
                else "[普通任务] 领取后余额：查询失败，不能确认到账"
            )
            if not action_ok:
                failures.append(f"领取 {task_label(task)} 失败：{action_msg}")
                failed_guids.add(guid)
            elif still_pending:
                failures.append(f"领取 {task_label(task)} 后仍为 FINISHED，服务端未确认移出待领取")
                failed_guids.add(guid)
            elif after_points is None or delta <= 0:
                failures.append(f"领取 {task_label(task)} 后余额未增加，不能确认到账")
                failed_guids.add(guid)
            else:
                claimed_count += 1
                print(f"✅ [普通任务] 领取到账确认：+{delta:g} 金币")
            tasks = after_tasks
            continue

        init_count = sum(task.get("taskStatus") == "INIT" for task in tasks)
        running_count = sum(task.get("taskStatus") == "RUNNING" for task in tasks)
        failed_count = sum(
            task.get("taskStatus") == "FINISHED"
            and str(task.get("taskGuid")) in failed_guids
            for task in tasks
        )
        if init_count or running_count:
            stop_reason = (
                f"INIT {init_count} 项、RUNNING {running_count} 项；"
                "需要小程序真实完成对应业务动作"
            )
        elif failed_count:
            stop_reason = f"{failed_count} 项 FINISHED 奖励领取后未通过服务端复核"
        elif tasks:
            stop_reason = "服务端没有已完成待领取的普通任务"
        else:
            stop_reason = "服务端未返回普通任务"
        break
    else:
        stop_reason = f"达到单次安全上限 {max_rounds} 轮，停止避免死循环"

    pending_count = sum(
        1 for task in tasks
        if task.get("taskStatus") in ("INIT", "RUNNING", "FINISHED")
    )
    summary = (
        f"领取到账已确认 {claimed_count} 项；"
        f"当前服务端待处理 {pending_count} 项；停止原因：{stop_reason}"
    )
    print(f"[普通任务] {summary}")
    if failures:
        raise RuntimeError(summary + "；失败 " + str(len(failures)) + " 项：" + "；".join(failures))
    return summary


def sign_outcome(response):
    try:
        data = checked_data(response)
    except RuntimeError as exc:
        return False, f"签到失败：{exc}"
    if data.get("doSignThisTime") is True:
        return True, f"签到成功 +{data.get('bountyCountToday', '?')}"
    if data.get("didSignToday") is True:
        return True, "今日已签到"
    return False, "服务端未确认签到成功，等待核对"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": account_label(index, server),
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "boxMsg": "-",
        "points": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(account_label(index, server))
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    code = get_code(server)
    if not code:
        result["error"] = "获取 code 失败"
        return result

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        errors = []
        start_points = read_points(server, token, proxies)
        sign_resp = api_post(server, SIGN_URL, token, proxies, SIGN_PAYLOAD)
        sign_ok, result["signMsg"] = sign_outcome(sign_resp)
        if not sign_ok:
            errors.append(result["signMsg"])
        print(f"[签到] {result['signMsg']}")

        # 各模块单独报错，宝箱失败不影响查询视频及最终余额。
        for key, name, handler in [
            ("boxMsg", "普通宝箱", run_boxes),
            ("rideVideoMsg", "上方·免费骑车视频", query_ride_video_progress),
            ("videoMsg", "下方·奖励金视频", query_video_progress),
            ("taskMsg", "任务奖励", collect_finished_tasks),
        ]:
            try:
                result[key] = handler(server, token, proxies)
            except Exception as exc:
                result[key] = f"失败：{exc}"
                errors.append(f"{name}：{exc}")
            print(f"[{name}] {result[key]}")

        point_resp = api_post(server, POINT_URL, token, proxies, POINT_PAYLOAD)
        point_data = point_resp.get("data") or {}

        if point_resp.get("code") == 0 and point_resp.get("success") is not False and point_data.get("points") is not None:
            result["points"] = f"{point_data.get('points', 0)} 金币 ≈{point_data.get('amount', 0)} 元"
            print(f"💰 [金币] {result['points']}")
            if start_points is not None and point_data.get("points") is not None:
                end_points = float(point_data["points"])
                result["incomeMsg"] = f"{start_points:g} → {end_points:g}，余额净变化 {end_points-start_points:+g} 金币"
                print(f"[本轮收益] {result['incomeMsg']}")
        else:
            result["points"] = "查询失败"
            errors.append("结束余额查询失败")
            print(f"⚠️ [金币] {result['points']}")

        result["success"] = not errors
        result["error"] = "；".join(errors)
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🚲 哈啰出行签到任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 执行检查：{success_count} 正常 / {fail_count} 异常（视频仅查询）
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
📦 宝箱：{res["boxMsg"]}
🎬 免费骑车：{res.get("rideVideoMsg", "未执行")}
🎬 奖励金视频：{res.get("videoMsg", "未执行")}
🎁 任务奖励：{res.get("taskMsg", "未执行")}
💰 金币：{res["points"]}
📊 收益：{res.get("incomeMsg", "余额不足以核算")}
{icon} 执行结果：{"正常（视频未自动执行）" if res["success"] else "有异常，见原因"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    servers = SERVERS
    account_limit = os.getenv("HELLO_ACCOUNT_LIMIT", "").strip()
    if account_limit:
        try:
            limit = int(account_limit)
        except ValueError:
            limit = 0
        if limit > 0:
            servers = SERVERS[:limit]
            print(f"⚠️ [验证] HELLO_ACCOUNT_LIMIT={limit}，本次仅处理前 {len(servers)} 个账号")

    log_title(len(servers))

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(servers, 1):
        try:
            result = run_account(index, len(servers), server)
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
                "boxMsg": "-",
                "points": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(servers):
            delay = random.randint(1, 2)
            print(f"⏳ [间隔] 等待 {delay}s 后处理下一个账号")
            sleep(delay)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 哈啰出行任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🚲 哈啰出行签到任务完成", build_notify(results))


if __name__ == "__main__":
    main()
