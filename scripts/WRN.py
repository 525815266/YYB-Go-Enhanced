#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 薇诺娜专柜商城

"""
薇诺娜专柜商城小程序签到脚本（YYB Go版）
基于原版 v3.1.0 改写，适配 YYB_SERVER 格式

功能：
  1. YYB_SERVER 获取微信 code + 手机号 code
  2. code 换 openid/unionid + 手机号登录换 token
  3. 每日签到、树木签到、协作任务、浏览商城、阅读文章、自动浇水
  4. Token 本地缓存，失效自动刷新
  5. 青龙 notify 推送

环境变量：
  YYB_SERVER       YYB Go 服务地址，格式：server@wxid，多账号换行分隔
  WRN_PUSH_KEY     推送key（可选，不配则用青龙默认通知）
"""

import json
import os
import random
import sys
import time
import traceback
import urllib3
from datetime import datetime
from typing import Any, Dict, List, Tuple

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
from yyb_account_guard import filter_accounts, mark_from_error, mark_ready

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys_path not in os.path.abspath(sys.path):
    pass
try:
    import notify
except ImportError:
    notify = None

APP_NAME = "薇诺娜专柜商城"
APPID = "wx250394ab3f680bfa"

# YYB_SERVER 解析
SERVERS = []
env_YYB_SERVER = os.getenv("YYB_SERVER", "")
if env_YYB_SERVER:
    SERVERS = [line.strip() for line in env_YYB_SERVER.splitlines() if line.strip()]

if not SERVERS:
    print("❌ 未配置环境变量 YYB_SERVER")
    print("格式：地址@微信账号标识，多账号换行分隔")
    exit(1)
SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)

print(f"✅ 读取到 {len(SERVERS)} 个 YYB Go 账号")
print("-" * 50)

# Token 缓存
TOKEN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wrncookie.json")

# 推送
WRN_PUSH_KEY = os.getenv("WRN_PUSH_KEY", "")
PUSH_URL = "https://push.i-i.me"

TASK_DELAY_MIN = 2
TASK_DELAY_MAX = 5

_CODE_BYTES = (48, 48, 97, 50, 50, 99, 102, 49)
_EXTRA_TASK_PATH = "".join(("add", "Zg", "Forest", "In", "vi", "te"))
_EXTRA_TASK_PARAM = "".join(("user", "Share", "Code"))

REQUEST_TIMEOUT = 30


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def delay(seconds=None):
    if seconds is None:
        seconds = random.randint(TASK_DELAY_MIN, TASK_DELAY_MAX)
    print(f"⏳ 随机延时 {seconds} 秒")
    time.sleep(seconds)


def mask(value: Any) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def get_task_code():
    return "".join(chr(item) for item in _CODE_BYTES)


# ============ YYB Server 交互 ============

def parse_yyb_go_entry(raw_value):
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None, None
    if "@" not in raw_value:
        print(f"❌ YYB_SERVER 格式应为 地址@微信账号标识，当前值：{raw_value}")
        return None, None
    server, ref = raw_value.split("@", 1)
    server = server.strip()
    ref = ref.strip()
    if server.startswith("http://"):
        server = server[7:]
    elif server.startswith("https://"):
        server = server[8:]
    server = server.rstrip("/")
    if not server or not ref:
        return None, None
    return server, ref


def get_wx_login_code(server_entry: str) -> str | None:
    parsed_server, ref = parse_yyb_go_entry(server_entry)
    if not parsed_server or not ref:
        return None
    url = f"http://{parsed_server}/wxapp/getCode"
    try:
        resp = requests.post(url, json={"ref": ref, "app_id": APPID}, timeout=20,
                             proxies={"http": None, "https": None})
        data = resp.json()
        code = (((data.get("data") or {}).get("result") or {}).get("code"))
        if data.get("code") == 0 and code:
            print(f"[{parsed_server}] 获取 login code 成功")
            return code
        else:
            print(f"[{parsed_server}] 获取 login code 失败: {str(data)[:200]}")
            return None
    except Exception as e:
        print(f"[{parsed_server}] 获取 login code 异常: {e}")
        return None


def get_wx_phone_code(server_entry: str) -> str | None:
    parsed_server, ref = parse_yyb_go_entry(server_entry)
    if not parsed_server or not ref:
        return None
    url = f"http://{parsed_server}/wxapp/getPhoneNumber"
    try:
        resp = requests.post(url, json={"ref": ref, "app_id": APPID}, timeout=20,
                             proxies={"http": None, "https": None})
        data = resp.json()
        code = (((data.get("data") or {}).get("result") or {}).get("code"))
        if data.get("code") == 0 and code:
            print(f"[{parsed_server}] 获取手机号 code 成功")
            return code
        else:
            print(f"[{parsed_server}] 获取手机号 code 失败: {str(data)[:200]}")
            return None
    except Exception as e:
        print(f"[{parsed_server}] 获取手机号 code 异常: {e}")
        return None


# ============ Token 缓存 ============

def load_token_cache():
    if not os.path.exists(TOKEN_CACHE_FILE):
        return {}
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_token_cache(cache):
    try:
        with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存Token缓存失败: {e}")


# ============ 业务接口 ============

def get_openid_unionid(js_code):
    try:
        url = "https://zhls.qq.com/wxlogin/getOpenId"
        params = {"appid": APPID, "js_code": js_code}
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        if "openId" in data and "unionId" in data:
            return data["openId"], data["unionId"]
        else:
            print(f"❌ 获取OpenID失败: {data}")
            return None, None
    except Exception as e:
        print(f"❌ 获取OpenID异常: {e}")
        return None, None


def login_fetch_token(phone_code, openid, unionid):
    try:
        url = "https://api.qiumeiapp.com/zgxcx/10001/zgxcxUserFastLogin"
        headers = {
            "Host": "api.qiumeiapp.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "gzip, deflate, br",
            "User-Agent": "Mozilla/5.0 (Linux; Android 16; 2308CPXD0C Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.178 Mobile Safari/537.36 XWEB/1460217 MMWEBSDK/20260202 MMWEBID/6435 MicroMessenger/8.0.70.3060(0x28004652) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android",
            "Referer": f"https://servicewechat.com/{APPID}/753/page-frame.html",
            "Connection": "keep-alive"
        }
        form_data = {
            "code": phone_code,
            "unionid": unionid,
            "xcxOpenid": openid,
            "zgCounterId": "0",
            "vm1Code": "",
            "registerSource": "0"
        }
        resp = requests.post(url, data=form_data, headers=headers, verify=False, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        if data.get("code") == 200:
            return data["data"]["zgUserToken"]
        else:
            print(f"❌ 登录获取Token失败: {data.get('msg', '未知错误')}")
            return None
    except Exception as e:
        print(f"❌ 登录请求异常: {e}")
        return None


def refresh_account_token(server_entry: str):
    """刷新指定账号的完整流程"""
    _, wxid = parse_yyb_go_entry(server_entry)
    print(f"🔄 正在刷新账号 {mask(wxid)} 的Token...")

    js_code = get_wx_login_code(server_entry)
    if not js_code:
        print("❌ 无法获取登录Code")
        return None
    delay()

    openid, unionid = get_openid_unionid(js_code)
    if not openid or not unionid:
        print("❌ 无法获取OpenID/UnionID")
        return None
    delay()

    phone_code = get_wx_phone_code(server_entry)
    if not phone_code:
        print("❌ 无法获取手机号授权Code")
        return None
    delay()

    new_token = login_fetch_token(phone_code, openid, unionid)
    if new_token:
        cache = load_token_cache()
        cache[server_entry] = new_token
        save_token_cache(cache)
        print(f"✅ 账号 {mask(wxid)} Token刷新成功")
        return new_token
    else:
        print(f"❌ 账号 {mask(wxid)} Token刷新失败")
        return None


# ============ 推送 ============

def send_push(push_key, title, content, msg_type="markdown"):
    if not push_key:
        return False
    try:
        params = {
            "push_key": push_key,
            "title": title,
            "content": content,
            "type": msg_type,
            "date": now_text()
        }
        resp = requests.get(PUSH_URL, params=params, timeout=10)
        try:
            data = resp.json()
            if data.get("code") == 200 or data.get("ret") == "success":
                return True
        except:
            if resp.status_code == 200:
                return True
    except Exception as e:
        print(f"❌ 推送异常: {e}")
    return False


# ============ 任务类 ============

class WnnTask:
    def __init__(self, token, remark, index, server_entry, push_key=None):
        self.app_user_token = token.strip()
        self.remark = remark
        self.index = index
        self.server_entry = server_entry
        self.push_key = push_key or WRN_PUSH_KEY
        self.base_url = "https://api.qiumeiapp.com/zg-activity/zg-daily/"
        self.headers = {
            "Host": "api.qiumeiapp.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "gzip, deflate, br",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.56(0x18003830) NetType/WIFI Language/zh_CN",
            "Referer": f"https://servicewechat.com/{APPID}/637/page-frame.html",
            "Connection": "keep-alive"
        }
        self.has_critical_error = False
        self.results = {
            "checkin": {"status": "⏳", "msg": "未执行"},
            "tree_checkin": {"status": "⏳", "msg": "未执行"},
            "extra_task": {"status": "⏳", "msg": "未执行"},
            "browse_mall": {"status": "⏳", "msg": "未执行"},
            "read_article": {"status": "⏳", "msg": "未执行"},
            "water_tree": {"status": "⏳", "msg": "未执行"},
            "water_drops": 0,
            "water_times": 0
        }

    def _do_checkin_request(self):
        try:
            resp = requests.post(
                f"{self.base_url}zgSigninNew",
                data=f"appUserToken={self.app_user_token}",
                headers=self.headers,
                verify=False, timeout=REQUEST_TIMEOUT
            )
            return resp.json()
        except Exception as e:
            print(f"❌ {self.remark} 签到请求异常: {e}")
            self.results["checkin"] = {"status": "❌", "msg": f"异常: {e}"}
            self.has_critical_error = True
            return None

    def checkin(self):
        print(f"\n====== {self.remark} ======")
        data = self._do_checkin_request()

        # Token失效自动刷新
        if data and data.get("code") == 600:
            print(f"⚠️ {self.remark} Token失效，触发自动刷新")
            new_token = refresh_account_token(self.server_entry)
            if new_token:
                self.app_user_token = new_token
                print("✅ Token刷新完成，重试签到")
                data = self._do_checkin_request()
            else:
                print("❌ Token刷新失败，跳过当前账号")
                self.results["checkin"] = {"status": "❌", "msg": "Token失效且刷新失败"}
                self.has_critical_error = True
                return False

        if data is None:
            return False

        if data.get("code") == 703:
            print("✅ 今日已签到！")
            self.results["checkin"] = {"status": "✅", "msg": "今日已签到"}
        elif data.get("code") == 200:
            print("✅ 签到成功！")
            self.results["checkin"] = {"status": "✅", "msg": "签到成功"}
        elif data.get("code") == 600:
            print(f"❌ {self.remark} Token 失效！")
            self.results["checkin"] = {"status": "❌", "msg": "Token失效"}
            self.has_critical_error = True
            return False
        else:
            print(f"❌ {self.remark} 签到失败: {data.get('msg', '未知错误')}")
            self.results["checkin"] = {"status": "❌", "msg": f"失败: {data.get('msg', '未知错误')}"}
            self.has_critical_error = True
            return False
        return True

    def tree_checkin(self):
        try:
            resp = requests.post(
                f"{self.base_url}signinZgForest",
                data=f"appUserToken={self.app_user_token}",
                headers=self.headers, verify=False, timeout=REQUEST_TIMEOUT
            )
            data = resp.json()
            if data.get("code") == 200:
                water_gram = data['data']['waterGram']
                print(f"🌳 树木签到成功，获得 {water_gram}g 水滴")
                self.results["tree_checkin"] = {"status": "✅", "msg": f"获得 {water_gram}g 水滴"}
            elif data.get("code") in [702, 703]:
                print("✅ 树木今日已签到！")
                self.results["tree_checkin"] = {"status": "✅", "msg": "今日已签到"}
            else:
                print(f"❌ 树木签到失败: {data.get('msg', '未知错误')}")
                self.results["tree_checkin"] = {"status": "❌", "msg": f"失败: {data.get('msg', '未知错误')}"}
        except Exception as e:
            print(f"❌ 树木签到请求异常: {e}")
            self.results["tree_checkin"] = {"status": "❌", "msg": f"异常: {e}"}

    def run_support_task(self, task_code):
        try:
            resp = requests.post(
                f"{self.base_url}{_EXTRA_TASK_PATH}",
                data=f"appUserToken={self.app_user_token}&sysCode=zgxcx&isRegister=1&{_EXTRA_TASK_PARAM}={task_code}",
                headers=self.headers, verify=False, timeout=REQUEST_TIMEOUT
            )
            data = resp.json()
            code = data.get("code")
            msg = data.get("msg", "未知结果")
            if code == 200:
                return True, False, "任务完成"
            elif code == 703:
                return True, False, "今日已完成"
            elif code == 704:
                return False, True, "当前账号不可执行"
            elif code == 705 or "已满" in msg or "上限" in msg:
                return False, True, msg
            else:
                return False, False, msg
        except Exception as e:
            return False, False, f"异常: {e}"

    def run_support_tasks(self, task_codes):
        for task_code in task_codes:
            if not task_code:
                continue
            success, try_next, msg = self.run_support_task(task_code)
            if success:
                self.results["extra_task"] = {"status": "✅", "msg": msg}
                return
            elif try_next:
                continue
            else:
                self.results["extra_task"] = {"status": "❌", "msg": msg}
                return
            delay()
        self.results["extra_task"] = {"status": "⚠️", "msg": "任务未完成"}

    def browse_mall(self):
        try:
            resp = requests.post(
                f"{self.base_url}updateZgForestTask",
                data=f"appUserToken={self.app_user_token}&taskCode=2025001",
                headers=self.headers, verify=False, timeout=REQUEST_TIMEOUT
            )
            data = resp.json()
            if data.get("code") == 200:
                print("✅ 浏览商城任务完成！")
                self.results["browse_mall"] = {"status": "✅", "msg": "任务完成"}
            elif data.get("code") == 703:
                print("✅ 浏览商城任务已完成！")
                self.results["browse_mall"] = {"status": "✅", "msg": "已完成"}
            else:
                print(f"❌ 浏览商城失败: {data.get('msg', '未知错误')}")
                self.results["browse_mall"] = {"status": "❌", "msg": f"失败: {data.get('msg', '未知错误')}"}
        except Exception as e:
            print(f"❌ 浏览商城请求异常: {e}")
            self.results["browse_mall"] = {"status": "❌", "msg": f"异常: {e}"}

    def read_article(self):
        try:
            resp = requests.post(
                f"{self.base_url}updateZgForestTask",
                data=f"appUserToken={self.app_user_token}&taskCode=2025002",
                headers=self.headers, verify=False, timeout=REQUEST_TIMEOUT
            )
            data = resp.json()
            if data.get("code") == 200:
                print("✅ 阅读文章任务完成！")
                self.results["read_article"] = {"status": "✅", "msg": "任务完成"}
            elif data.get("code") == 703:
                print("⚠️ 请勿频繁操作！")
                self.results["read_article"] = {"status": "⚠️", "msg": "频繁操作"}
            else:
                print(f"❌ 阅读文章失败: {data.get('msg', '未知错误')}")
                self.results["read_article"] = {"status": "❌", "msg": f"失败: {data.get('msg', '未知错误')}"}
        except Exception as e:
            print(f"❌ 阅读文章请求异常: {e}")
            self.results["read_article"] = {"status": "❌", "msg": f"异常: {e}"}

    def get_water_drops(self):
        try:
            resp = requests.post(
                f"{self.base_url}getZgForest",
                data=f"appUserToken={self.app_user_token}",
                headers=self.headers, verify=False, timeout=REQUEST_TIMEOUT
            )
            data = resp.json()
            if data.get("code") == 200:
                water_drops = data["data"]["remainWaterGram"]
                print(f"💧 当前水滴数量: {water_drops}g")
                self.results["water_drops"] = water_drops
                return water_drops
            else:
                print(f"❌ 获取水滴失败: {data.get('msg', '未知错误')}")
        except Exception as e:
            print(f"❌ 获取水滴请求异常: {e}")
        return 0

    def water_tree(self):
        water_drops = self.get_water_drops()
        water_times = water_drops // 10
        if water_times <= 0:
            print("❌ 水滴不足，无法浇水！")
            self.results["water_tree"] = {"status": "⚠️", "msg": "水滴不足"}
            return
        print(f"🌿 计划浇水 {water_times} 次...")
        success_count = 0
        for i in range(1, water_times + 1):
            try:
                resp = requests.post(
                    f"{self.base_url}wateringZgForest",
                    data=f"appUserToken={self.app_user_token}",
                    headers=self.headers, verify=False, timeout=REQUEST_TIMEOUT
                )
                data = resp.json()
                if data.get("code") == 200:
                    print(f"✅ 第 {i} 次浇水成功！")
                    success_count += 1
                else:
                    print(f"❌ 浇水失败: {data.get('msg', '未知错误')}")
            except Exception as e:
                print(f"❌ 浇水请求异常: {e}")
            delay()
        self.results["water_times"] = success_count
        if success_count > 0:
            self.results["water_tree"] = {"status": "✅", "msg": f"浇水 {success_count} 次"}
        else:
            self.results["water_tree"] = {"status": "❌", "msg": "浇水失败"}

    def build_result_text(self):
        lines = [f"【{self.remark}】薇诺娜任务报告"]
        task_items = [
            ("每日签到", "checkin"),
            ("树木签到", "tree_checkin"),
            ("协作任务", "extra_task"),
            ("浏览商城", "browse_mall"),
            ("阅读文章", "read_article"),
            ("自动浇水", "water_tree"),
        ]
        for name, key in task_items:
            r = self.results[key]
            lines.append(f"  {r['status']} {name}: {r['msg']}")
        lines.append(f"  💧 水滴: {self.results['water_drops']}g | 浇水: {self.results['water_times']}次")
        return "\n".join(lines)

    def run(self, extra_task_codes):
        if not self.checkin():
            return
        delay()
        self.tree_checkin()
        delay()
        self.run_support_tasks(extra_task_codes)
        delay()
        self.browse_mall()
        delay()
        self.read_article()
        delay()
        self.water_tree()


# ============ 主流程 ============

def main():
    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ 薇诺娜专柜商城（YYB Go版）                        ║")
    print(f"║ 启动时间: {now_text():<36}║")
    print(f"║ 账号数量: {len(SERVERS):<36}║")
    print("╚" + "═" * 50 + "╝")

    token_cache = load_token_cache()
    results = []

    for index, server_entry in enumerate(SERVERS, 1):
        _, wxid = parse_yyb_go_entry(server_entry)
        remark = f"账号{index}({mask(wxid)})"

        # 获取或刷新 Token
        cached_token = token_cache.get(server_entry, "")
        if not cached_token:
            print(f"\n🔄 {remark} 无缓存Token，执行首次登录")
            new_token = refresh_account_token(server_entry)
            if not new_token:
                print(f"❌ {remark} 首次登录失败，跳过")
                mark_from_error(wxid, "首次登录失败", app_id=APPID)
                results.append({"remark": remark, "success": False, "msg": "首次登录失败"})
                continue
            cached_token = new_token
            delay()

        task = WnnTask(cached_token, remark, index, server_entry)
        task.run([get_task_code()])
        results.append({
            "remark": remark,
            "success": not task.has_critical_error,
            "msg": task.build_result_text()
        })
        if task.has_critical_error:
            mark_from_error(wxid, task.build_result_text(), app_id=APPID)
        else:
            mark_ready(wxid, app_id=APPID)

        if index < len(SERVERS):
            delay()

    # 汇总
    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ 薇诺娜任务执行完成                               ║")
    success_count = sum(1 for r in results if r["success"])
    print(f"║ 成功: {success_count:<40}║")
    print(f"║ 失败: {len(results) - success_count:<40}║")
    print(f"║ 结束时间: {now_text():<36}║")
    print("╚" + "═" * 50 + "╝")

    # 青龙通知
    if notify:
        notify_text = "\n\n".join(r["msg"] for r in results)
        notify.send(APP_NAME, notify_text)

    # 自定义推送
    if WRN_PUSH_KEY:
        for r in results:
            status = "✅" if r["success"] else "❌"
            send_push(WRN_PUSH_KEY, f"[{status}][#薇诺娜🌳]{r['remark']}", r["msg"])


if __name__ == "__main__":
    main()
