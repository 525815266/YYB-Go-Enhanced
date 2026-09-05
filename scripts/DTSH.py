#!/usr/bin/env python3
# name: DT生活
# cron: 48 15 * * *
import os
import random
import time
import json
import requests
from yyb_account_guard import filter_accounts, mark_from_error, mark_ready
from datetime import datetime

# ===================== 新增：彻底关闭InsecureRequestWarning警告 =====================
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# ==================================================================================

# ===================== 配置项 =====================
# 基础配置
APP_ID = "wx51a2021dd921f747"
PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")

# 从环境变量 YYB_SERVER 读取内网登录接口，多条换行分隔
CODE_URL_LIST = []
env_YYB_SERVER = os.getenv("YYB_SERVER", "")
if env_YYB_SERVER:
    # 兼容 \r\n 和 \n 换行，去除每行前后空格，过滤空行
    raw_lines = env_YYB_SERVER.splitlines()
    CODE_URL_LIST = [line.strip() for line in raw_lines if line.strip()]

# 校验是否存在有效服务地址
if len(CODE_URL_LIST) == 0:
    print("❌ 未配置环境变量 YYB_SERVER")
    print("格式：地址@微信账号标识，多账号换行分隔")
    print("http://192.168.1.7:8088/login")
    exit(1)

CODE_URL_LIST = filter_accounts(CODE_URL_LIST, app_id=APP_ID, log=print)
if not CODE_URL_LIST:
    print("⏭️ 公共缓存中没有可执行账号")
    raise SystemExit(0)

print(f"✅ 读取到 {len(CODE_URL_LIST)} 个 YYB Go 账号")
for item in CODE_URL_LIST:
    print(f" - {item}")
print("-" * 50)

# 品赞代理配置（青龙环境变量）
PROXY_API = os.getenv("PROXY_API", "")  # 代理提取API链接
PROXY_TYPE = os.getenv("PROXY_TYPE", "http")  # 代理类型: http 或 socks5
PROXY_RETRY_TIMES = 3  # 单个账号代理获取重试次数
PROXY_VALIDATE_URL = "http://httpbin.org/ip"  # 代理验证地址
# 核心开关：每个账号独立获取专属代理（True=每个账号一个新IP，False=所有账号共用一个IP）
ENABLE_PER_ACCOUNT_PROXY = True
# 账号间代理获取间隔（秒，避免频繁调用代理API被限流）
PROXY_FETCH_INTERVAL = 3
# 兜底开关：代理请求失败后，自动切换直连重试
ENABLE_DIRECT_FALLBACK = True

# 业务接口（固定）
LOGIN_URL = "https://ebeikeapi.ebeck.cn/api/v2/user/userLogin"
SIGN_URL = "https://ebeikeapi.ebeck.cn/api/v2/user/userSign"
TOTAL_POINTS_URL = "https://ebeikeapi.ebeck.cn/api/v2/user/userPointsGoldInfo"

# 随机UA池（防风控）
USER_AGENT_LIST = [
    "Mozilla/5.0 (Linux; Android 14; 2512BPNDAC Build/UKQ1.230917.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.153 Mobile Safari/537.36 XWEB/1460043 MMWEBSDK/20251006 MMWEBID/2089 MicroMessenger/8.0.66.2980(0x28004234) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android",
    "Mozilla/5.0 (Linux; Android 13; Redmi K60 Build/TKQ1.221114.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.6723.102 Mobile Safari/537.36 XWEB/1300003 MMWEBSDK/20250901 MiniProgramEnv/android",
    "Mozilla/5.0 (Linux; Android 12; MI 11 Build/SKQ1.211006.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/125.0.6422.111 Mobile Safari/537.36 XWEB/1250002 MMWEBSDK/20250801 MiniProgramEnv/android",
]
# ======================================================

# ====================== 品赞IP代理系统（每个账号独立获取）======================
def parse_proxy_response(text):
    """解析代理API响应（支持品赞等多种格式）"""
    text = text.strip()
    if not text:
        return None

    # 尝试JSON解析
    try:
        data = json.loads(text)
        proxy_obj = None
        
        # 品赞标准格式: {code: 0, data: [{ip: "x.x.x.x", port: 12345}]}
        if data.get("data") and isinstance(data["data"], list) and len(data["data"]) > 0:
            proxy_obj = data["data"][0]
        # 普通JSON格式: {ip: "x.x.x.x", port: 12345}
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        # 嵌套格式: {result: {ip: "x.x.x.x", port: 12345}}
        elif data.get("result") and data["result"].get("ip") and data["result"].get("port"):
            proxy_obj = data["result"]

        if proxy_obj:
            return {
                "host": proxy_obj.get("ip"),
                "port": proxy_obj.get("port"),
                "username": proxy_obj.get("user") or proxy_obj.get("username", ""),
                "password": proxy_obj.get("pass") or proxy_obj.get("password", "")
            }
    except json.JSONDecodeError:
        pass

    # 尝试纯文本解析 (ip:port 或 ip:port:user:pass)
    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            return {
                "host": parts[0].strip(),
                "port": int(parts[1]),
                "username": parts[2].strip() if len(parts) > 2 else "",
                "password": parts[3].strip() if len(parts) > 3 else ""
            }

    return None

def build_proxy_config(proxy_info):
    """生成requests库的代理配置（支持HTTP/SOCKS5）"""
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info["username"]
    password = proxy_info["password"]

    auth = ""
    if username and password:
        auth = f"{username}:{password}@"

    if PROXY_TYPE == "socks5":
        proxy_url = f"socks5://{auth}{host}:{port}"
        print(f"🔧 生成SOCKS5代理：socks5://{auth}{host}:{port}")
    else:
        proxy_url = f"http://{auth}{host}:{port}"
        print(f"🔧 生成HTTP代理：{proxy_url}")

    return {
        "http": proxy_url,
        "https": proxy_url
    }

def validate_proxy(proxy_config):
    """验证代理是否可用"""
    if not proxy_config:
        return False
    try:
        response = requests.get(
            PROXY_VALIDATE_URL,
            proxies=proxy_config,
            timeout=15,
            verify=False
        )
        is_success = response.status_code == 200
        if is_success:
            origin_ip = response.json().get("origin", "未知")
            print(f"✅ 代理验证通过，出口IP：{origin_ip}")
        return is_success
    except Exception as e:
        print(f"⚠️ 代理验证失败，原因：{str(e)}")
        return False

def get_valid_proxy(account_name):
    """获取有效代理（每个账号独立调用）"""
    if not PROXY_API:
        print(f"ℹ️ [{account_name}] 未配置代理API，使用直连")
        return None

    print(f"🔌 [{account_name}] 正在从品赞API获取专属代理 ({PROXY_TYPE})...")

    for i in range(PROXY_RETRY_TIMES):
        try:
            # 获取代理API用直连，避免循环依赖
            response = requests.get(
                PROXY_API,
                timeout=15,
                proxies={"http": None, "https": None},
                verify=False
            )
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                print(f"⚠️ [{account_name}] 第{i+1}次获取代理失败：响应格式无法解析")
                continue

            print(f"✅ [{account_name}] 提取到专属代理：{proxy_info['host']}:{proxy_info['port']}")

            # 生成代理配置并验证
            proxy_config = build_proxy_config(proxy_info)
            is_valid = validate_proxy(proxy_config)
            if is_valid:
                return proxy_config
            else:
                print(f"⚠️ [{account_name}] 第{i+1}次获取的代理不可用，正在重试...")

        except Exception as e:
            print(f"⚠️ [{account_name}] 第{i+1}次获取代理异常：{str(e)}")

        # 重试间隔
        if i < PROXY_RETRY_TIMES - 1:
            time.sleep(2)

    print(f"❌ [{account_name}] 连续多次获取代理失败，使用直连")
    return None
# ======================================================

def parse_yyb_go_entry(raw_value):
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


def get_wx_code(code_url):
    """通过YYB Go获取对应账号的微信Code【强制直连，不走代理】"""
    server, ref = parse_yyb_go_entry(code_url)
    if not server:
        print("❌ 获取Code失败：服务地址为空")
        return None
    if not ref:
        print(f"❌ 获取Code失败：{code_url} 缺少openid/ref")
        return None

    try:
        res = requests.post(
            f"http://{server}/wxapp/getCode",
            json={"ref": ref, "app_id": APP_ID},
            timeout=20,
            proxies={"http": None, "https": None}
        )
        data = res.json()
        code = (((data.get("data") or {}).get("result") or {}).get("code"))
        if data.get("code") == 0 and code:
            print(f"✅ {server} 获取Code成功")
            return code
        print(f"❌ 获取Code失败：{str(data)[:200]}")
    except Exception as e:
        print(f"❌ 获取Code失败：{str(e)}")
    return None

def refresh_token(code_url, proxy_config, account_name):
    """通过对应接口刷新Token【支持代理+直连兜底】"""
    code = get_wx_code(code_url)
    if not code:
        return None, None

    # 每次随机UA
    headers = {
        "Content-Type": "application/json",
        "charset": "utf-8",
        "User-Agent": random.choice(USER_AGENT_LIST)
    }

    try:
        payload = {"code": code, "appId": APP_ID, "client": "wxmp", "version": "251", "pid": "", "channeltype": ""}
        
        # 优先代理请求
        if proxy_config:
            print(f"🌐 [{account_name}] 正在使用专属代理发起登录请求...")
            try:
                res = requests.post(
                    LOGIN_URL,
                    json=payload,
                    headers=headers,
                    proxies=proxy_config,
                    timeout=20,
                    verify=False
                )
            except Exception as e:
                if ENABLE_DIRECT_FALLBACK:
                    print(f"⚠️ [{account_name}] 代理登录失败，切换直连重试...")
                    res = requests.post(
                        LOGIN_URL,
                        json=payload,
                        headers=headers,
                        proxies={"http": None, "https": None},
                        timeout=20,
                        verify=False
                    )
                else:
                    raise e
        else:
            res = requests.post(
                LOGIN_URL,
                json=payload,
                headers=headers,
                proxies={"http": None, "https": None},
                timeout=20,
                verify=False
            )

        data = res.json()
        token = data.get("data", {}).get("token", "")
        if token:
            print("✅ Token获取成功")
            return token, headers
    except Exception as e:
        print(f"❌ Token获取异常：{str(e)}")
    
    print("❌ Token获取失败")
    return None, None

def get_user_info(token, headers, proxy_config, account_name):
    """获取用户信息+总积分【支持代理+直连兜底】"""
    try:
        payload = {"version": "251", "client": "wxmp", "token": token}
        req_headers = {**headers, "Authorization": f"Bearer {token}"}
        
        # 优先代理请求
        if proxy_config:
            try:
                res = requests.post(
                    TOTAL_POINTS_URL,
                    json=payload,
                    headers=req_headers,
                    proxies=proxy_config,
                    timeout=20,
                    verify=False
                )
            except Exception as e:
                if ENABLE_DIRECT_FALLBACK:
                    print(f"⚠️ [{account_name}] 代理查询用户信息失败，切换直连重试...")
                    res = requests.post(
                        TOTAL_POINTS_URL,
                        json=payload,
                        headers=req_headers,
                        proxies={"http": None, "https": None},
                        timeout=20,
                        verify=False
                    )
                else:
                    raise e
        else:
            res = requests.post(
                TOTAL_POINTS_URL,
                json=payload,
                headers=req_headers,
                proxies={"http": None, "https": None},
                timeout=20,
                verify=False
            )

        data = res.json()
        d = data.get("data", {})
        return d.get("nickname", "未知"), d.get("mobile", "未知"), d.get("points", 0)
    except Exception as e:
        print(f"❌ 查询用户信息异常：{str(e)}")
        return "未知", "未知", 0

def push_plusplus(title, content):
    """PlusPlus推送（带状态返回）"""
    if not PLUSPLUS_TOKEN:
        print("ℹ️ 未配置PLUSPLUS_TOKEN，不推送")
        return False
    try:
        data = {"token": PLUSPLUS_TOKEN, "title": title, "content": content}
        res = requests.post(
            "https://www.pushplus.plus/send",
            json=data,
            timeout=10,
            verify=False
        )
        result = res.json()
        return result.get("code") == 200
    except Exception as e:
        print(f"❌ PushPlus推送异常：{str(e)}")
        return False

def do_sign(token, headers, proxy_config, account_name):
    """执行签到【支持代理+直连兜底】"""
    try:
        payload = {"version": "251", "client": "wxmp", "token": token}
        
        # 优先代理请求
        if proxy_config:
            try:
                res = requests.post(
                    SIGN_URL,
                    json=payload,
                    headers=headers,
                    proxies=proxy_config,
                    timeout=20,
                    verify=False
                )
            except Exception as e:
                if ENABLE_DIRECT_FALLBACK:
                    print(f"⚠️ [{account_name}] 代理签到失败，切换直连重试...")
                    res = requests.post(
                        SIGN_URL,
                        json=payload,
                        headers=headers,
                        proxies={"http": None, "https": None},
                        timeout=20,
                        verify=False
                    )
                else:
                    raise e
        else:
            res = requests.post(
                SIGN_URL,
                json=payload,
                headers=headers,
                proxies={"http": None, "https": None},
                timeout=20,
                verify=False
            )

        return res.json()
    except Exception as e:
        print(f"❌ 签到异常：{str(e)}")
        return None

def run_account(code_url, index, global_proxy_config):
    """执行单个账号（对应接口）"""
    account_name = f"账号{index}"
    print(f"\n=======================================================")
    print(f"🚀 开始执行 {account_name} | 接口：{code_url}")
    print("=======================================================")

    # 核心逻辑：每个账号独立获取专属代理
    proxy_config = global_proxy_config
    proxy_status = "未使用代理"
    if ENABLE_PER_ACCOUNT_PROXY:
        proxy_config = get_valid_proxy(account_name)
        proxy_status = "使用专属代理" if proxy_config else "使用直连"
        # 代理获取后加间隔，避免频繁请求
        time.sleep(PROXY_FETCH_INTERVAL)

    token, headers = refresh_token(code_url, proxy_config, account_name)
    if not token:
        return {
            "account": account_name,
            "success": False,
            "proxy_status": proxy_status,
            "error": "Token获取失败"
        }

    # 随机延迟 3~8 秒
    delay = random.uniform(3, 8)
    print(f"⏳ 签到前等待：{delay:.1f}秒")
    time.sleep(delay)

    try:
        # 执行签到
        data = do_sign(token, headers, proxy_config, account_name)
        if not data:
            return {
                "account": account_name,
                "success": False,
                "proxy_status": proxy_status,
                "error": "签到请求异常"
            }

        sign_msg = data.get("msg", "完成")
        get_points = data.get("data", {}).get("points", 0)
        sign_num = data.get("data", {}).get("sign_num", 0)

        # 获取用户信息
        nickname, uid, total_points = get_user_info(token, headers, proxy_config, account_name)

        # 控制台简洁展示
        print(f"👤 账户昵称：{nickname}")
        print(f"🆔 账号UID：{uid}")
        print(f"📊 签到结果：{sign_msg}")
        print(f"📅 累计签到：{sign_num} 次")
        print(f"🎁 本次获得：{get_points} 积分")
        print(f"💰 账户总积分：{total_points} 分")

        # 带Emoji图标的推送内容
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        push_title = f"✅ DT生活签到 {account_name}"
        push_content = (
            f"🕒 执行时间：{now}\n\n"
            f"🔌 代理状态：{proxy_status}\n"
            f"👤 账户昵称：{nickname}\n"
            f"🆔 账号UID：{uid}\n"
            f"📊 签到结果：{sign_msg}\n"
            f"📅 累计签到：{sign_num} 次\n"
            f"🎁 本次获得：{get_points} 积分\n"
            f"💰 账户总积分：{total_points} 分"
        )

        # 推送并显示状态
        push_ok = push_plusplus(push_title, push_content)
        print("✅ 推送成功" if push_ok else "❌ 推送失败")

        return {
            "account": account_name,
            "success": True,
            "proxy_status": proxy_status,
            "nickname": nickname,
            "uid": uid,
            "sign_msg": sign_msg,
            "sign_num": sign_num,
            "get_points": get_points,
            "total_points": total_points
        }

    except Exception as e:
        print(f"❌ 签到异常：{str(e)}")
        return {
            "account": account_name,
            "success": False,
            "proxy_status": proxy_status,
            "error": str(e)
        }

if __name__ == "__main__":
    print('===== DT生活签到（环境变量YYB_SERVER读取内网多服务+独立代理版）=====\n')

    # 兼容旧逻辑：如果关闭了单账号代理，就全局获取一个共用代理
    global_proxy_config = None
    if not ENABLE_PER_ACCOUNT_PROXY:
        global_proxy_config = get_valid_proxy("全局共用")

    # 循环执行所有内网服务账号
    all_results = []
    for i, url in enumerate(CODE_URL_LIST, 1):
        result = run_account(url, i, global_proxy_config)
        all_results.append(result)
        ref = url.rsplit("@", 1)[-1].strip()
        if result.get("success"):
            mark_ready(ref, app_id=APP_ID)
        else:
            mark_from_error(ref, result.get("error", ""), app_id=APP_ID)
        # 账号间间隔2秒
        time.sleep(2)

    # 汇总结果
    print("\n🎉 所有账号执行完毕！")
