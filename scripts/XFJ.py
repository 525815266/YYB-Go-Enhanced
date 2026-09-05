#!/usr/bin/env python3
# name: 小福家
# cron: 30 7 * * *
# -*- coding: utf-8 -*-

"""
小福家小程序登录 - 纯 YYB Go 版
code + 手机号加密数据均从 YYB Go 获取

环境变量：
  YYB_SERVER    必填：YYB Go 服务地址@微信账号标识，多行换行

依赖：
  pip install requests
"""

import os
import sys
import json
import time
import hashlib
import requests
from yyb_account_guard import filter_accounts, mark_from_error, mark_ready

APPID = "wxe6ba46e6100e68e9"
APPKEY = "b98b1abf926b44e3998e5573b42f101f"
APPSECRET = "e5e3333fbb7448c7813281c68bad7f57"
API_HOST = "api.xiaofujia.com"
API_BASE = f"https://{API_HOST}"
PLATFORM = 12

env_YYB_SERVER = os.getenv("YYB_SERVER", "")
if env_YYB_SERVER:
    raw_lines = env_YYB_SERVER.splitlines()
else:
    print("❌ 未配置环境变量 YYB_SERVER")
    print("格式：地址@微信账号标识，多账号换行分隔")
    sys.exit(1)

SERVERS = [l.strip() for l in raw_lines if l.strip() and "@" in l.strip()]
SERVERS = filter_accounts(SERVERS, app_id=APPID, log=print)
if not SERVERS:
    print("❌ YYB_SERVER 无有效账号")
    sys.exit(1)

print(f"✅ 读取到 {len(SERVERS)} 个 YYB Go 账号")


def parse_yyb_entry(raw: str) -> dict | None:
    value = raw.strip()
    at_idx = value.index("@")
    server = value[:at_idx].strip()
    ref = value[at_idx + 1:].strip()
    if server.startswith("http://"):
        server = server[7:]
    elif server.startswith("https://"):
        server = server[8:]
    server = server.rstrip("/")
    return {"server": server, "ref": ref} if server and ref else None


def yyb_post(entry: str, endpoint: str) -> dict | None:
    """通用 YYB Go 请求"""
    parsed = parse_yyb_entry(entry)
    if not parsed:
        return None
    server, ref = parsed["server"], parsed["ref"]
    url = f"http://{server}{endpoint}"
    try:
        resp = requests.post(url, json={"ref": ref, "app_id": APPID}, timeout=20)
        data = resp.json()
        if data.get("code") != 0:
            print(f"[{server}] {endpoint} 失败: {data.get('msg', '')}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"[{server}] {endpoint} 异常: {e}")
        return None


def get_code(entry: str) -> str | None:
    data = yyb_post(entry, "/wxapp/getCode")
    if not data:
        return None
    code = (data.get("result") or {}).get("code")
    if code:
        print(f"获取code成功: {code[:8]}****")
    return code


def get_phone_data(entry: str) -> dict | None:
    """从 YYB Go /wxapp/getPhoneNumber 获取手机号加密数据"""
    data = yyb_post(entry, "/wxapp/getPhoneNumber")
    if not data:
        return None
    result = data.get("result") or {}
    encrypted = result.get("encryptedData")
    iv = result.get("iv")
    if not encrypted or not iv:
        # 可能返回的是完整手机号，尝试其他字段
        encrypted = result.get("encrypted_data") or data.get("encryptedData")
        iv = result.get("iv") or data.get("iv")
    if not encrypted or not iv:
        print(f"手机号数据不完整: {json.dumps(result, ensure_ascii=False)[:200]}")
        return None
    print("获取手机号加密数据成功")
    return {"encryptedData": encrypted, "iv": iv}


def xiaofujia_login(code: str, mobile: dict) -> dict | None:
    print("→ 小福家登录...")
    login_url = f"{API_BASE}/familychat/user/login"
    
    auth_token = json.dumps({
        "code": code,
        "mobile_encrypt_data": mobile["encryptedData"],
        "mobile_iv": mobile["iv"]
    })
    
    body = {
        "auth_type": 2,
        "auth_token": auth_token,
        "platform": PLATFORM,
        "did": "nfPQXpkJaxRQ8BQw4B66KWtWBFXC22SH",
        "metadata": {"launch_mnp_scene": 0}
    }
    
    t = int(time.time())
    params = {"time": t, "appkey": APPKEY}
    sign_str = "".join(f"{k}{params[k]}" for k in sorted(params)) + APPSECRET
    sign = hashlib.md5(sign_str.encode()).hexdigest()
    full_url = f"{login_url}?time={t}&appkey={APPKEY}&sign={sign}"
    
    headers = {
        "content-type": "application/json;charset=UTF-8",
        "Host": API_HOST,
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003121) NetType/WIFI Language/zh_CN",
        "Referer": "https://servicewechat.com/wxe6ba46e6100e68e9/116/page-frame.html"
    }
    
    try:
        resp = requests.post(full_url, json=body, headers=headers, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            print(f"✗ 登录失败: {data.get('msg', '未知错误')}")
            return None
        token = data.get("data", {}).get("access_token", "")
        print(f"✓ 登录成功！access_token: {token[:15]}...")
        return {"access_token": token}
    except Exception as e:
        print(f"✗ 登录异常: {e}")
        return None


def main():
    print("┌─────────────────────────────┐")
    print("│ 小福家小程序登录 │")
    print("└─────────────────────────────┘")
    
    for i, entry in enumerate(SERVERS):
        parsed = parse_yyb_entry(entry)
        if not parsed:
            print(f"✗ 第{i+1}行格式无效，跳过")
            continue
        
        print(f"\n========== 账号[{i+1}] {parsed['ref']} ==========")
        
        code = get_code(entry)
        if not code:
            print(f"✗ 获取code失败，跳过")
            continue
        
        mobile = get_phone_data(entry)
        if not mobile:
            print(f"✗ 获取手机号失败，跳过")
            continue
        
        result = xiaofujia_login(code, mobile)
        if result:
            print(f"✓ 账号[{i+1}] ACCESS_TOKEN={result['access_token']}")
            mark_ready(parsed['ref'], app_id=APPID)
        else:
            print(f"✗ 账号[{i+1}] 登录失败")
            mark_from_error(parsed['ref'], "登录失败", app_id=APPID)
        
        if i < len(SERVERS) - 1:
            time.sleep(3)
    
    print("\n┌─────────────────────────────┐")
    print("│ 所有账号处理完成 │")
    print("└─────────────────────────────┘")


if __name__ == "__main__":
    main()
