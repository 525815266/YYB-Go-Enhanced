#!/usr/bin/env python3
# name: 三福
# cron: 0 9 * * *
import os
import time
import random
import requests
import asyncio
from yyb_account_guard import filter_accounts, update_from_result


# ===================== 强制全局禁用系统代理环境变量，避免干扰 =====================
for env_key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    if env_key in os.environ:
        del os.environ[env_key]

# ===================== 配置项 =====================
PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")

# 从环境变量 YYB_SERVER 读取内网服务器，多条换行分隔
SERVERS = []
env_YYB_SERVER = os.getenv("YYB_SERVER", "")
if env_YYB_SERVER:
    # 兼容 \r\n 和 \n 换行，去除每行前后空格，过滤空行
    raw_lines = env_YYB_SERVER.splitlines()
    SERVERS = [line.strip() for line in raw_lines if line.strip()]

# 校验是否存在有效服务地址
if len(SERVERS) == 0:
    print("❌ 未配置环境变量 YYB_SERVER")
    print("格式：地址@微信账号标识，多账号换行分隔")
    print("192.168.31.111:8088")
    exit(1)

print(f"✅ 读取到 {len(SERVERS)} 个 YYB Go 账号")
print("-" * 50)

PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http")
PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
ENABLE_PER_ACCOUNT_PROXY = True
PROXY_FETCH_INTERVAL = 3000
ENABLE_DIRECT_FALLBACK = True

APPID = "wxfe13a2a5df88b058"
SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)
USER_AGENT_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781 NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF",
    "Mozilla/5.0 (Linux; Android 14; 2512BPNDAC Build/UKQ1.230917.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.153 Mobile Safari/537.36 XWEB/1460043 MMWEBSDK/20251006 MiniProgramEnv/android",
    "Mozilla/5.0 (Linux; Android 13; Redmi K60 Build/TKQ1.221114.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.6723.102 Mobile Safari/537.36 XWEB/1300003 MMWEBSDK/20250901 MiniProgramEnv/android"
]
BASE_URL = "https://crm.sanfu.com"

# ===================== 工具函数 =====================
async def sleep(ms):
    await asyncio.sleep(ms / 1000)

def random_int(min_val, max_val):
    return random.randint(min_val, max_val)

def getUA():
    return random.choice(USER_AGENT_LIST)

# ====================== 品赞IP代理系统 ======================
def parseProxyResponse(text):
    text = text.strip()
    if not text:
        return None
    try:
        import json
        data = json.loads(text)
        proxyObj = None
        if data.get("data") and isinstance(data["data"], list) and len(data["data"]) > 0:
            proxyObj = data["data"][0]
        elif data.get("ip") and data.get("port"):
            proxyObj = data
        elif data.get("result") and data["result"].get("ip") and data["result"].get("port"):
            proxyObj = data["result"]
        
        if proxyObj:
            return {
                "host": proxyObj["ip"],
                "port": int(proxyObj["port"]),
                "username": proxyObj.get("user") or proxyObj.get("username") or "",
                "password": proxyObj.get("pass") or proxyObj.get("password") or ""
            }
    except:
        pass
    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            return {
                "host": parts[0],
                "port": int(parts[1]),
                "username": parts[2] if len(parts) > 2 else "",
                "password": parts[3] if len(parts) > 3 else ""
            }
    return None

def buildProxyDict(proxyInfo):
    if not proxyInfo:
        return None
    host = proxyInfo["host"]
    port = proxyInfo["port"]
    username = proxyInfo["username"]
    password = proxyInfo["password"]
    
    auth = ""
    if username and password:
        auth = f"{requests.utils.quote(username)}:{requests.utils.quote(password)}@"
    
    if PROXY_TYPE == "socks5":
        proxy_url = f"socks5://{auth}{host}:{port}"
    else:
        proxy_url = f"http://{auth}{host}:{port}"
    
    return {
        "http": proxy_url,
        "https": proxy_url
    }

def validateProxy(proxies):
    if not proxies:
        return False
    try:
        res = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15, verify=False)
        if res.status_code == 200:
            print(f"✅ 代理验证通过，出口IP：{res.json().get('origin', '未知')}")
            return True
        return False
    except Exception as e:
        print(f"⚠️ 代理验证失败，原因：{str(e)[:60]}")
        return False

async def getValidProxy(accountName):
    if not PROXY_API:
        print(f"ℹ️ [{accountName}] 未配置代理API，使用直连")
        return None
    print(f"🔌 [{accountName}] 正在从品赞API获取专属代理 ({PROXY_TYPE})...")
    
    for i in range(PROXY_RETRY_TIMES):
        try:
            res = requests.get(PROXY_API, timeout=15, proxies={})
            proxyInfo = parseProxyResponse(res.text)
            
            if not proxyInfo:
                print(f"⚠️ [{accountName}] 第{i+1}次获取代理失败：响应格式无法解析")
                continue
            print(f"✅ [{accountName}] 提取到专属代理：{proxyInfo['host']}:{proxyInfo['port']}")
            
            proxies = buildProxyDict(proxyInfo)
            if validateProxy(proxies):
                return proxies
            else:
                print(f"⚠️ [{accountName}] 第{i+1}次获取的代理不可用，正在重试...")
        except Exception as e:
            print(f"⚠️ [{accountName}] 第{i+1}次获取代理异常：{str(e)[:60]}")
        
        if i < PROXY_RETRY_TIMES - 1:
            await sleep(2000)
    print(f"❌ [{accountName}] 连续多次获取代理失败，使用直连")
    return None

# ===================== PushPlus通知 =====================
def sendPlusPlusNotification(title, content):
    if not PLUSPLUS_TOKEN:
        return
    try:
        url = "https://www.pushplus.plus/send"
        data = {
            "token": PLUSPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": "txt"
        }
        requests.post(url, json=data, timeout=5)
        print("✅ 通知推送成功")
    except Exception as e:
        print(f"❌ 通知推送失败：{str(e)}")

# ===================== 业务函数 =====================
def parse_yyb_go_entry(raw_value):
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None, None

    if "@" not in raw_value:
        print(f"❌ 配置错误：YYB_SERVER 格式应为 地址@微信账号标识，当前值：{raw_value}")
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
        print(f"❌ 配置错误：YYB_SERVER 缺少地址或微信账号标识，当前值：{raw_value}")
        return None, None

    return server, ref

def getCode(server):
    parsed_server, ref = parse_yyb_go_entry(server)
    if not parsed_server or not ref:
        return None

    url = f"http://{parsed_server}/wxapp/getCode"
    payload = {"ref": ref, "app_id": APPID}
    print(f"[{parsed_server}] 请求YYB Go获取code：{url}")

    try:
        res = requests.post(
            url,
            json=payload,
            timeout=20,
            proxies={"http": None, "https": None},
        )
        data = res.json()
        code = (((data.get("data") or {}).get("result") or {}).get("code"))
        if data.get("code") != 0 or not code:
            print(f"[{parsed_server}] 获取code失败：{data}")
            return None

        print(f"[{parsed_server}] 获取code成功")
        return code
    except Exception as exc:
        print(f"[{parsed_server}] 获取code异常：{exc}")
        return None

def wxLogin(jsCode, UA, proxies, server):
    headers = {
        "Host": "crm.sanfu.com",
        "Content-Type": "application/json",
        "User-Agent": UA,
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/385/page-frame.html",
        "Accept": "*/*"
    }
    login_url = f"{BASE_URL}/ms-sanfu-wechat-customer-core/customer/core/wxMiniAppLogin"
    payload = {
        "code": jsCode,
        "appid": APPID,
        "shoId": "",
        "userId": "",
        "sourceWxsceneid": 1027,
        "sourceUrl": "pages/ucenter_index/ucenter_index"
    }
    
    try:
        response = None
        if proxies:
            print(f"🌐 [{server}] 正在使用专属代理发起登录请求...")
            try:
                response = requests.post(login_url, json=payload, headers=headers, proxies=proxies, timeout=20)
            except Exception as e:
                print(f"⚠️ [{server}] 代理登录失败，切换直连重试...")
                response = requests.post(login_url, json=payload, headers=headers, proxies={}, timeout=20)
        else:
            response = requests.post(login_url, json=payload, headers=headers, proxies={}, timeout=20)
        
        print(f"[{server}] 登录接口返回：{response.text[:300]}")
        return response.json()
    except Exception as e:
        print(f"❌ [{server}] 登录异常: {str(e)[:60]}")
        return None

# 核心修复：改用sid鉴权，删除无效token头
def commonRequest(url, method="GET", body=None, sid="", UA="", proxies=None, server=""):
    headers = {
        "Host": "crm.sanfu.com",
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Referer": f"https://servicewechat.com/{APPID}/385/page-frame.html"
    }
    req_url = f"{BASE_URL}{url}"
    if body is None:
        body = {}
    # 自动带入sid做登录鉴权
    body["sid"] = sid

    try:
        response = None
        if proxies:
            try:
                if method.upper() == "POST":
                    response = requests.post(req_url, json=body, headers=headers, proxies=proxies, timeout=20)
                else:
                    response = requests.get(req_url, params=body, headers=headers, proxies=proxies, timeout=20)
            except Exception as e:
                print(f"⚠️ [{server}] 代理请求失败，切换直连重试...")
                if method.upper() == "POST":
                    response = requests.post(req_url, json=body, headers=headers, proxies={}, timeout=20)
                else:
                    response = requests.get(req_url, params=body, headers=headers, proxies={}, timeout=20)
        else:
            if method.upper() == "POST":
                response = requests.post(req_url, json=body, headers=headers, proxies={}, timeout=20)
            else:
                response = requests.get(req_url, params=body, headers=headers, proxies={}, timeout=20)
        return response.json()
    except Exception as e:
        print(f"❌ [{server}] 请求异常: {str(e)[:60]}")
        return None

# ===================== 单个账号执行 =====================
async def runAccount(server, globalProxyAgent):
    result = {
        "server": server,
        "success": False,
        "signMsg": "",
        "scoreMsg": "",
        "error": "",
        "proxyStatus": "未使用代理"
    }
    print(f"\n===== 三福 - {server} 账号 =====")
    UA = getUA()
    proxyAgent = globalProxyAgent
    if ENABLE_PER_ACCOUNT_PROXY:
        proxyAgent = await getValidProxy(server)
        result["proxyStatus"] = "使用专属代理" if proxyAgent else "使用直连"
        await sleep(PROXY_FETCH_INTERVAL)
    
    try:
        startDelay = random_int(2000, 6000)
        print(f"⏳ [{server}] 启动延迟 {startDelay / 1000}s")
        await sleep(startDelay)
        
        # 1. 获取code
        code = getCode(server)
        if not code:
            result["error"] = "获取code失败"
            print(f"❌ [{server}] 获取code失败")
            return result
        
        # 2. 登录获取sid
        login_data = wxLogin(code, UA, proxyAgent, server)
        if not login_data or login_data.get("code") != 200:
            result["error"] = login_data.get("msg", "登录失败") if login_data else "登录无响应"
            print(f"❌ [{server}] 登录失败：{result['error']}")
            return result
        sid = login_data["data"].get("sid", "")
        if not sid:
            result["error"] = "未获取到sid，无法继续"
            print(f"❌ [{server}] 未获取到sid，无法继续")
            return result
        print(f"✅ [{server}] 登录成功获取sid")
        await sleep(random_int(3000, 8000))
        
        # 3. 每日签到
        sign_data = commonRequest(
            "/ms-sanfu-wechat-common/customer/onSign",
            method="POST",
            body={"signWay": 0},
            sid=sid,
            UA=UA,
            proxies=proxyAgent,
            server=server
        )
        if sign_data and sign_data.get("code") == 200:
            fubi = sign_data["data"].get("fubi", 0)
            keep_day = sign_data["data"].get("onKeepSignDay", 0)
            result["signMsg"] = f"签到成功！连续签到{keep_day}天，获得{fubi}福币"
            print(f"✅ [{server}] {result['signMsg']}")
        else:
            msg = sign_data.get("msg", "未知错误") if sign_data else "接口无响应"
            result["signMsg"] = f"签到失败：{msg}"
            print(f"❌ [{server}] {result['signMsg']}")
        await sleep(random_int(2000, 5000))
        
        # 4. 查询福币
        info_data = commonRequest(
            "/ms-sanfu-wechat-customer/customer/index/baseInfo",
            method="GET",
            body={},
            sid=sid,
            UA=UA,
            proxies=proxyAgent,
            server=server
        )
        if info_data and info_data.get("code") == 200:
            cur_fubi = info_data["data"].get("fubi", 0)
            result["scoreMsg"] = f"当前账号总福币：{cur_fubi}个"
            print(f"🎯 [{server}] {result['scoreMsg']}")
        
        result["success"] = True
        print(f"✅ [{server}] 账号执行完成")
    except Exception as e:
        result["error"] = str(e)
        print(f"❌ [{server}] 执行异常：{str(e)[:60]}")
    return result

# ===================== 主程序 =====================
async def main():
    print('===== 三福动态code签到（环境变量YYB_SERVER多内网+品赞代理+sid鉴权修复版）=====\n')
    globalProxyAgent = None
    if not ENABLE_PER_ACCOUNT_PROXY:
        globalProxyAgent = await getValidProxy("全局共用")
    
    results = []
    for server in SERVERS:
        res = await runAccount(server, globalProxyAgent)
        update_from_result(server, res, app_id=APPID)
        results.append(res)
        await sleep(2000)
    
    notifyContent = "### 三福多账号任务执行结果\n"
    for res in results:
        notifyContent += f"\n#### {res['server']}\n"
        notifyContent += f"- 代理状态：{res['proxyStatus']}\n"
        notifyContent += f"- 执行状态：{'成功' if res['success'] else '失败'}\n"
        if res["success"]:
            notifyContent += f"- 签到结果：{res['signMsg']}\n"
            notifyContent += f"- 福币信息：{res['scoreMsg']}\n"
        else:
            notifyContent += f"- 失败原因：{res['error']}\n"
    
    sendPlusPlusNotification("三福多账号任务完成", notifyContent)
    print('\n===== 所有账号执行完成 =====')

if __name__ == "__main__":
    asyncio.run(main())
