#!/usr/bin/env python3
# name: ole超市
# cron: 16 6,18 * * *
import requests
import json
import os
import time

from yyb_account_guard import filter_accounts, mark_from_error, mark_ready


# ================= 配置 =================
APP_ID = "wx6c61aaeba1551439"
BASE_URL = "https://ole-app.crvole.com.cn"
COOKIE_FILE = "olecookie.json"

# 定位经纬度，用于自动匹配对应门店
LOCATION = "119.17437689887153,26.149126519097223"

UNIQUE = "weapp-cd343bf0-d546-ed7f-aace-59c826aed36b"

COMMON_HEADERS = {
    "appVersion": "1.10.32",
    "channel": "wxmini",
    "os": "android",
    "Tenant": "VGDT",
    "Tenant-Channel": "OLE",
    "content-type": "application/json"
}


# ================= cookie =================
def load_cookie():
    if not os.path.exists(COOKIE_FILE):
        return {}
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_cookie(data):
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================= YYB_SERVER 环境变量解析 =================
def parse_yyb_go():
    """解析 YYB_SERVER 环境变量，格式：地址@微信账号标识，多行换行"""
    raw = os.getenv("YYB_SERVER", "")
    accounts = []
    for idx, line in enumerate(raw.splitlines(), 1):
        value = str(line or "").strip()
        if not value:
            continue
        at_index = value.find("@")
        if at_index == -1:
            print(f"  [YYB_SERVER 第{idx}行] 格式错误，缺少 @ 分隔符：{value}")
            continue
        server = value[:at_index].strip()
        ref = value[at_index + 1 :].strip()
        # 去掉 http:// 或 https:// 前缀
        if server.startswith("http://"):
            server = server[7:]
        elif server.startswith("https://"):
            server = server[8:]
        server = server.rstrip("/")
        if not server or not ref:
            print(f"  [YYB_SERVER 第{idx}行] 地址或 ref 为空，已跳过")
            continue
        accounts.append(
            {
                "name": f"YYB_SERVER账号{idx}",
                "ref": ref,
                "server": server,
            }
        )
    return accounts


# ================= 微信8000 =================
def get_code(account):
    """使用指定账号的 server 和 ref 获取 code"""
    server = account.get("server", "")
    ref = account.get("ref", "")
    if not server or not ref:
        return None
    try:
        data = {
            "app_id": APP_ID,
            "ref": str(ref)
        }
        r = requests.post(
            "http://" + server + "/wxapp/getCode",
            json=data,
            timeout=15
        )
        res = r.json()

        if res.get("code") != 0:
            return None

        wxdata = res.get("data", {})
        result = wxdata.get("result", {})
        code = result.get("code")

        if not code:
            return None

        return {
            "code": code,
            "openid": wxdata.get("openid")
        }

    except Exception as e:
        print(f"获取code异常 ({server}): {e}")
        return None


# ================= OLE登录 =================
def login_by_code(code):
    url = BASE_URL + "/vgdt_app_api/v1/vgdt-fea-app-member/front_api/wechat_auths/code/mini_program"
    headers = {
        **COMMON_HEADERS,
        "unique": UNIQUE,
        "Device-Name": "666888",
        "traceId": str(int(time.time() * 1000000))
    }

    try:
        r = requests.post(
            url,
            headers=headers,
            json={"code": code},
            timeout=15
        )
        res = r.json()

        if res.get("state_code") != 200:
            return None

        data = res.get("data", {})
        session = data.get("user_session")
        openid = data.get("open_id")

        if not session or not openid:
            return None

        return {
            "sessionId": session,
            "oleWxOpenId": openid
        }

    except Exception as e:
        print("登录异常:", e)
        return None


# ================= 业务接口 =================
def build_headers(cookie):
    return {
        **COMMON_HEADERS,
        "unique": UNIQUE,
        "Device-Name": "666888",
        "sessionId": cookie["sessionId"],
        "oleWxOpenId": cookie["oleWxOpenId"],
        "traceId": str(int(time.time() * 1000000))
    }


def get_shop_code(cookie):
    """调用门店接口自动获取shop_code"""
    url = BASE_URL + "/vgdt_app_api/v1/vgdt-fea-app-entershop/front_api/enter_shops/shop"
    payload = {
        "location": LOCATION,
        "address_longitude": "",
        "location_name": "",
        "province": "",
        "city": "",
        "district": "",
        "address": "",
        "house_number": "",
        "receive_mobile": "",
        "receive_name": "",
        "region_name": "",
        "region_id": "",
        "select": False
    }

    try:
        r = requests.post(
            url,
            headers=build_headers(cookie),
            json=payload,
            timeout=15
        )
        res = r.json()

        if res.get("state_code") == 200:
            shop_code = res.get("data", {}).get("shop_code")
            if shop_code:
                print(f"获取门店编码成功: {shop_code}")
                return shop_code
        print(f"获取门店编码失败: {res.get('message', '接口返回异常')}")

    except Exception as e:
        print(f"获取门店编码异常: {e}")

    return None


def check_sign(cookie):
    url = BASE_URL + "/vgdt_app_api/v1/vgdt-fea-app-member/front_api/member_sign"
    try:
        r = requests.get(
            url,
            headers=build_headers(cookie),
            timeout=15
        )
        return r.json()
    except:
        return {}


def do_sign(cookie):
    url = BASE_URL + "/vgdt_app_api/v1/vgdt-fea-app-member/front_api/member_sign"
    try:
        r = requests.post(
            url,
            headers=build_headers(cookie),
            json={
                "enter_shop_code": cookie.get("shop_code", "")
            },
            timeout=15
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ================= 主程序 =================
def main():
    old_cookie = load_cookie()
    accounts = parse_yyb_go()
    accounts = filter_accounts(accounts, lambda account: account.get("ref"), app_id=APP_ID, log=print)
    if not accounts:
        print("公共缓存中没有可执行账号")
        return

    print("账号数量:", len(accounts))
    update_cookie = {}

    for idx, account in enumerate(accounts, 1):
        try:
            aid = str(idx)
            nickname = account.get("name", aid)
            print(f"\n账号: {nickname}")

            cookie = old_cookie.get(aid)
            valid = False

            # 检查旧token有效性
            if cookie:
                result = check_sign(cookie)
                if result.get("state_code") == 200 and result.get("data"):
                    valid = True
                    print("token有效")

                    # 兼容旧缓存：无shop_code时自动补充
                    if "shop_code" not in cookie or not cookie["shop_code"]:
                        print("补充获取门店编码")
                        shop_code = get_shop_code(cookie)
                        if shop_code:
                            cookie["shop_code"] = shop_code
                            update_cookie[aid] = cookie
                        else:
                            print("获取门店编码失败，跳过当前账号")
                            continue

            # token无效则重新登录+获取门店
            if not valid:
                print("重新获取token")
                wx = get_code(account)
                if not wx:
                    print("未获取到小程序 code，跳过当前账号")
                    continue

                login = login_by_code(wx["code"])
                if not login:
                    print("登录失败，跳过")
                    mark_from_error(account.get("ref"), "登录失败", app_id=APP_ID)
                    continue

                # 登录成功后自动获取门店编码
                temp_cookie = {
                    "sessionId": login["sessionId"],
                    "oleWxOpenId": login["oleWxOpenId"]
                }
                shop_code = get_shop_code(temp_cookie)
                if not shop_code:
                    print("获取门店编码失败，跳过当前账号")
                    continue

                cookie = {
                    "id": aid,
                    "sessionId": login["sessionId"],
                    "oleWxOpenId": login["oleWxOpenId"],
                    "shop_code": shop_code
                }

            update_cookie[aid] = cookie

            # 查询签到状态
            status = check_sign(cookie)
            if status.get("state_code") != 200 or not status.get("data"):
                print("账号状态异常，跳过")
                continue

            data = status["data"]
            if data.get("sign_of_day") == "Y":
                print("今日已签到 积分:", data.get("total_integral"))
            else:
                result = do_sign(cookie)

                # 签到失败则刷新门店编码重试1次
                if result.get("state_code") != 200 or "error" in result:
                    print("首次签到失败，刷新门店编码后重试")
                    new_shop_code = get_shop_code(cookie)
                    if new_shop_code:
                        cookie["shop_code"] = new_shop_code
                        update_cookie[aid] = cookie  # 更新缓存待写入
                        result = do_sign(cookie)

            print("签到结果:", result)
            if result.get("state_code") == 200 and "error" not in result:
                mark_ready(account.get("ref"), app_id=APP_ID)

        except Exception as e:
            print("账号异常跳过:", e)
            mark_from_error(account.get("ref"), str(e), app_id=APP_ID)
            continue

    # 合并并保存最新cookie（含shop_code）
    old_cookie.update(update_cookie)
    save_cookie(old_cookie)

    print("\n全部执行完成")


if __name__ == "__main__":
    main()
