#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: asdcb_auto_sign
"""
阿水大杯茶签到。

环境变量：
    YYB_SERVER=yyb-go:8000@账号ID或OpenID

    ASDCB_ENABLE_79_COUPON=1  周二尝试使用 10 积分兑换 7.9 折券（默认关闭）
    ASDCB_MEMBER_CLAIM_PAYLOAD 会员日动态领取参数 JSON（可选，见日志提示）

多账号使用换行分隔。脚本会缓存 Qmai token 和签到所需的会员资料，
仅在 token 失效时重新通过 YYB Go 获取 wx.login code。
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


NAME = "阿水大杯茶"
MINI_APP_ID = "wxf4b12e079bb99abc"
BASE_URL = "https://webapi.qmai.cn"
STORE_ID = "203192"
SCENE = "1027"
PAGE_VERSION = os.environ.get("ASDCB_PAGE_VERSION", "346").strip() or "346"
TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_3_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.63(0x18003f28) NetType/WIFI Language/zh_CN"
)

LOGIN_PATH = "/web/account-center/oauth/mini-app-login"
PROFILE_PATH = "/web/account-center/crm/query-person-info"
SIGN_DETAIL_PATH = "/web/catering/integral/sign/detail"
SIGN_RULE_PATH = "/web/catering/integral/sign/rule"
SIGN_PATH = "/web/catering/integral/sign/signIn"
POINTS_PATH = "/web/catering/crm/total-points"

# ===== 阿水会员日功能开关 =====
# 会员日券的领取接口还要求小程序前端动态生成 data/signature，不能硬编码抓包值。
ENABLE_MEMBER_DAY_COUPON = True
# ===== 阿水 7.9 折券开关（默认关闭，避免误扣 10 积分） =====
ENABLE_79_DISCOUNT_COUPON = False
_coupon_switch = os.environ.get("ASDCB_ENABLE_79_COUPON", "").strip().lower()
if _coupon_switch:
    ENABLE_79_DISCOUNT_COUPON = _coupon_switch in {"1", "true", "yes", "on"}

MEMBER_DAY_ACTIVITY_ID = "1038463189786820608"
DISCOUNT_COUPON_GOODS_ID = "1158413458987839488"
DISCOUNT_COUPON_POINTS = 10
MEMBER_DAY_INFO_URL = (
    "https://images.qmai.cn/cmkcenter/activity/203192/"
    f"{MEMBER_DAY_ACTIVITY_ID}.json"
)
MEMBER_DAY_CLAIM_PATH = "/web/cmk-center/receive/takePartInReceive"
COUPON_LIST_PATH = "/web/catering/crm/coupon/list"
DISCOUNT_GOODS_DETAIL_PATH = "/web/mall-apiserver/integral/item/goods/detail"
DISCOUNT_ORDER_CREATE_PATH = "/web/mall-apiserver/integral/order/create"
DISCOUNT_PAYMENT_PATH = "/web/mall-apiserver/integral/pay/payment-info"

api_session = requests.Session()
yyb_session = requests.Session()
yyb_session.trust_env = False


class ApiError(RuntimeError):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = str(code or "")


def success(payload):
    data = payload or {}
    return data.get("status") is True and str(data.get("code")) == "0"


def token_error(error):
    code = str(getattr(error, "code", "") or "")
    if code in {"401", "40101", "100401", "1004010"}:
        return True
    return bool(re.search(r"token|登录|登陆|未登录|请先登录|凭证|身份.*失效", str(error), re.I))


def safe_json(response):
    try:
        return response.json()
    except Exception:
        return {
            "status": False,
            "code": f"HTTP_{response.status_code}",
            "message": response.text[:300],
            "data": None,
        }


def build_headers(token="", include_scene=True):
    headers = {
        "Accept": "v=1.0",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Qm-From": "wechat",
        "Qm-From-Type": "catering",
        "store-id": STORE_ID,
        "qm-trace-store-id": STORE_ID,
        "Referer": f"https://servicewechat.com/{MINI_APP_ID}/{PAGE_VERSION}/page-frame.html",
        "Accept-Language": "zh-CN",
    }
    if token:
        headers["Qm-User-Token"] = token
    if include_scene:
        headers["scene"] = SCENE
    return headers


def parse_yyb_entry(raw):
    value = str(raw or "").strip()
    if "@" not in value:
        raise ValueError("YYB_SERVER 格式应为 地址@账号ID或OpenID")
    server, ref = value.split("@", 1)
    server, ref = server.strip().rstrip("/"), ref.strip()
    if not server or not ref:
        raise ValueError("YYB_SERVER 中地址或账号标识为空")
    if not re.match(r"^https?://", server, re.I):
        server = "http://" + server
    return server, ref


def get_wx_code(entry):
    server, ref = parse_yyb_entry(entry)
    response = yyb_session.post(
        f"{server}/wxapp/getCode",
        json={"ref": ref, "app_id": MINI_APP_ID},
        timeout=TIMEOUT,
    )
    payload = safe_json(response)
    code = (((payload.get("data") or {}).get("result") or {}).get("code"))
    if response.status_code != 200 or str(payload.get("code")) != "0" or not code:
        message = payload.get("message") or payload.get("msg") or payload.get("code")
        raise ApiError(f"YYB Go 获取 code 失败：{message}")
    return code


def _find_crypto_key(value):
    """从 YYB getLatestUserKey 的不同兼容包装中提取密钥。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    if isinstance(value, dict):
        key = value.get("encryptKey") or value.get("encrypt_key")
        iv = value.get("iv")
        if key and iv:
            return {
                "encryptKey": str(key),
                "iv": str(iv),
                "version": value.get("version") or value.get("keyVersion") or 8,
            }
        for item in value.values():
            found = _find_crypto_key(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_crypto_key(item)
            if found:
                return found
    return None


def _signed_uint64(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number - (1 << 64) if number >= (1 << 63) else number


def _wx_operation_error(payload):
    """解析 YYB 原始 operateWxData protobuf 包装中的业务错误。"""
    try:
        result = (payload.get("data") or {}).get("result") or {}
        detail = ((result.get("2") or {}).get("2") or {})
        code = _signed_uint64(detail.get("1"))
        message = str(detail.get("2") or result.get("5") or "").strip()
        if code not in (None, 0) or message:
            text = message or "微信能力调用失败"
            return f"{text}（code={code}）" if code is not None else text
    except (AttributeError, TypeError):
        pass
    return ""


def get_wx_latest_user_key(entry):
    server, ref = parse_yyb_entry(entry)
    response = yyb_session.post(
        f"{server}/wx/getlatestuserkey",
        json={
            "ref": ref,
            "app_id": MINI_APP_ID,
            "payload": {"api_name": "getLatestUserKey", "data": {}, "env": 1},
        },
        timeout=TIMEOUT,
    )
    payload = safe_json(response)
    key = _find_crypto_key(payload)
    if response.status_code != 200 or not key:
        operation_error = _wx_operation_error(payload)
        if operation_error:
            raise ApiError(
                "YYB Go 当前协议无法调用小程序本地 getLatestUserKey："
                + operation_error
            )
        message = payload.get("message") or payload.get("msg") or payload.get("code")
        raise ApiError(f"YYB Go 未返回 encryptKey/iv：{message}")
    return key


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def qmai_member_openid(openid):
    """阿水活动接口使用 Qmai openid 的 MD5，而非微信原始 openid。"""
    return hashlib.md5(str(openid).encode("utf-8")).hexdigest()


def member_day_signature(activity_id, seller_id, timestamp, user_id):
    values = {
        "activityId": str(activity_id),
        "sellerId": str(seller_id),
        "timestamp": str(timestamp),
        "userId": str(user_id),
    }
    canonical = "&".join(f"{name}={values[name]}" for name in sorted(values))
    canonical += f"&key={str(activity_id)[::-1]}"
    return hashlib.md5(canonical.encode("utf-8")).hexdigest().upper()


def qmai_encrypt_json(value, key, iv):
    cipher = AES.new(str(key).encode("utf-8"), AES.MODE_CBC, str(iv).encode("utf-8"))
    encrypted = cipher.encrypt(pad(value.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("ascii")


def cache_path():
    configured = os.environ.get("ASDCB_TOKEN_DIR", "").strip()
    if configured:
        folder = Path(configured)
    elif Path("/ql/data/config").is_dir():
        folder = Path("/ql/data/config/asdcb")
    else:
        folder = Path(__file__).resolve().parent / "token_caches"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "token_cache.json"


def read_cache():
    try:
        path = cache_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def write_cache(cache):
    try:
        cache_path().write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as error:
        print(f"写入 token 缓存失败：{error}")


def account_cache_key(entry):
    return hashlib.sha256(entry.encode("utf-8")).hexdigest()


class Account:
    def __init__(self, index, entry, dry_run=False):
        self.index = index
        self.entry = entry
        self.dry_run = dry_run
        self.cache_key = account_cache_key(entry)
        self.token = ""
        self.mobile = ""
        self.user_name = ""
        self.openid = ""
        self.user_id = ""

    def api_request(self, method, path, payload=None):
        response = api_session.request(
            method,
            f"{BASE_URL}{path}",
            headers=build_headers(self.token),
            json=payload,
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            raise ApiError(f"HTTP {response.status_code}")
        return safe_json(response)

    def api_post(self, path, payload):
        return self.api_request("POST", path, payload)

    def api_get(self, path, params=None):
        response = api_session.get(
            f"{BASE_URL}{path}",
            headers=build_headers(self.token),
            params=params,
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            raise ApiError(f"HTTP {response.status_code}")
        return safe_json(response)

    def ensure_success(self, payload, action):
        if not success(payload):
            raise ApiError(
                payload.get("message") or f"{action}失败",
                payload.get("code"),
            )
        return payload.get("data")

    def login(self):
        code = get_wx_code(self.entry)
        print(f"账号[{self.index}] YYB Go 获取 code 成功")
        response = api_session.post(
            f"{BASE_URL}{LOGIN_PATH}",
            headers=build_headers(include_scene=False),
            json={"code": code, "eVersion": "1.0", "appid": MINI_APP_ID},
            timeout=TIMEOUT,
        )
        payload = safe_json(response)
        data = self.ensure_success(payload, "登录") or {}
        self.token = str(data.get("token") or "")
        user = data.get("user") or {}
        self.mobile = str(user.get("mobile") or "")
        self.user_name = str(user.get("username") or user.get("nickname") or "")
        self.openid = str(user.get("openid") or "")
        self.user_id = str(user.get("id") or "")
        if not self.token:
            raise ApiError("登录响应未返回 token")
        self.save_cache()
        print(f"账号[{self.index}] 登录成功")

    def load_cache(self):
        item = read_cache().get(self.cache_key) or {}
        self.token = str(item.get("token") or "")
        self.mobile = str(item.get("mobile") or "")
        self.user_name = str(item.get("userName") or "")
        self.openid = str(item.get("openid") or "")
        self.user_id = str(item.get("userId") or "")
        return bool(self.token)

    def save_cache(self):
        if not self.token:
            return
        cache = read_cache()
        cache[self.cache_key] = {
            "token": self.token,
            "mobile": self.mobile,
            "userName": self.user_name,
            "openid": self.openid,
            "userId": self.user_id,
            "updatedAt": int(time.time()),
        }
        write_cache(cache)

    def clear_cache(self):
        cache = read_cache()
        if self.cache_key in cache:
            del cache[self.cache_key]
            write_cache(cache)
        self.token = ""
        self.mobile = ""
        self.user_name = ""
        self.openid = ""
        self.user_id = ""

    def query_profile(self):
        payload = self.api_post(PROFILE_PATH, {"appid": MINI_APP_ID})
        profile = self.ensure_success(payload, "会员资料查询") or {}
        self.mobile = str(profile.get("mobilePhone") or self.mobile or "")
        self.user_name = str(
            profile.get("name")
            or profile.get("nickName")
            or self.user_name
            or ""
        )
        self.user_id = str(profile.get("id") or profile.get("userId") or self.user_id or "")
        self.save_cache()
        return profile

    def validate_token(self):
        try:
            self.query_profile()
            return True
        except ApiError:
            return False

    def query_detail(self):
        payload = self.api_post(SIGN_DETAIL_PATH, {"appid": MINI_APP_ID})
        return self.ensure_success(payload, "签到详情查询") or {}

    def query_rule(self):
        payload = self.api_post(SIGN_RULE_PATH, {"appid": MINI_APP_ID})
        data = self.ensure_success(payload, "签到规则查询") or []
        if not isinstance(data, list) or not data:
            return {}
        return (data[0] or {}).get("detailInfo") or {}

    def query_points(self):
        payload = self.api_post(POINTS_PATH, {"appid": MINI_APP_ID})
        data = self.ensure_success(payload, "积分查询")
        return data

    def query_member_day(self):
        """读取周二会员日活动配置。该静态配置不代表个人领取状态。"""
        response = api_session.get(
            MEMBER_DAY_INFO_URL,
            headers=build_headers(self.token),
            params={"appid": MINI_APP_ID, "t": int(time.time())},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            raise ApiError(f"会员日活动配置 HTTP {response.status_code}")
        info = safe_json(response)
        if not isinstance(info, dict):
            info = {}
        return info

    def find_member_day_coupon(self, activity):
        """从个人券包核对本期会员日券，返回 (状态, 券模板)。"""
        rewards = activity.get("rewardList") or []
        template_ids = {
            str(item.get("entityId"))
            for item in rewards
            if isinstance(item, dict) and item.get("entityId")
        }
        reward_names = {
            str(item.get("rewardName"))
            for item in rewards
            if isinstance(item, dict) and item.get("rewardName")
        }
        status_names = {0: "未使用", 1: "已使用", 2: "已过期"}
        for use_status in (0, 1, 2):
            payload = self.api_post(
                COUPON_LIST_PATH,
                {"pageNo": 1, "pageSize": 1000, "useStatus": use_status},
            )
            data = self.ensure_success(payload, "个人券包查询") or {}
            for item in _walk_dicts(data):
                template = item.get("couponTemplate")
                if not isinstance(template, dict):
                    continue
                template_id = str(template.get("id") or "")
                template_name = str(template.get("name") or "")
                if template_id in template_ids or template_name in reward_names:
                    return status_names[use_status], template
        return None, None

    def run_member_day_coupon(self):
        if not ENABLE_MEMBER_DAY_COUPON:
            return
        # Python weekday: Monday=0, Tuesday=1.
        if time.localtime().tm_wday != 1:
            return
        try:
            activity = self.query_member_day()
        except ApiError as error:
            print(f"账号[{self.index}] 会员日活动状态查询失败：{error}")
            return
        activity_status = activity.get("activityStatus")
        receive_status = activity.get("receiveStatus")
        print(
            f"账号[{self.index}] 周二会员日活动配置：activityStatus={activity_status or '-'}，"
            f"receiveStatus={receive_status or '-'}（不代表个人是否已领取）"
        )

        try:
            coupon_status, coupon = self.find_member_day_coupon(activity)
        except ApiError as error:
            print(f"账号[{self.index}] 个人券包核对失败，继续尝试领取：{error}")
        else:
            if coupon:
                print(
                    f"账号[{self.index}] 会员日券已领取：{coupon.get('name') or '本期会员日券'}，"
                    f"状态={coupon_status}"
                )
                return
            print(f"账号[{self.index}] 个人券包未找到本期会员日券，开始尝试领取")

        if self.dry_run:
            print(f"账号[{self.index}] dry-run：跳过会员日券领取")
            return

        # 允许调试时覆盖完整参数；正常流程按小程序源码动态生成。
        raw_payload = os.environ.get("ASDCB_MEMBER_CLAIM_PAYLOAD", "").strip()
        if raw_payload:
            try:
                claim = json.loads(raw_payload)
            except json.JSONDecodeError as error:
                print(f"账号[{self.index}] 会员日券未领取：ASDCB_MEMBER_CLAIM_PAYLOAD 不是合法 JSON：{error}")
                return
            if not isinstance(claim, dict):
                print(f"账号[{self.index}] 会员日券未领取：领取参数必须是 JSON 对象")
                return
        else:
            if not self.openid or not self.user_id:
                self.query_profile()
            if not self.openid or not self.user_id:
                print(f"账号[{self.index}] 会员日券未领取：登录资料缺少 openid/userId")
                return
            try:
                key = get_wx_latest_user_key(self.entry)
                timestamp = str(int(time.time() * 1000))
                claim = {
                    "activityId": MEMBER_DAY_ACTIVITY_ID,
                    "qzGtd": "",
                    "gdtVid": "",
                    "deviceToken": "",
                    "openid": qmai_member_openid(self.openid),
                    "appid": MINI_APP_ID,
                    "timestamp": timestamp,
                    "signature": member_day_signature(
                        MEMBER_DAY_ACTIVITY_ID, STORE_ID, timestamp, self.user_id
                    ),
                    "v": 1,
                }
                plaintext = json.dumps(claim, ensure_ascii=False, separators=(",", ":"))
                claim["data"] = qmai_encrypt_json(
                    plaintext, key["encryptKey"], key["iv"]
                )
                claim["version"] = key.get("version") or 8
            except (ApiError, ValueError, TypeError, KeyError) as error:
                print(
                    f"账号[{self.index}] 会员日券未提交：动态参数生成失败：{error}；"
                    "这不是已领取结果"
                )
                return
        claim.setdefault("activityId", MEMBER_DAY_ACTIVITY_ID)
        claim.setdefault("appid", MINI_APP_ID)
        claim.setdefault("v", 1)
        try:
            payload = self.api_post(MEMBER_DAY_CLAIM_PATH, claim)
            data = self.ensure_success(payload, "会员日券领取") or {}
        except ApiError as error:
            print(f"账号[{self.index}] 会员日券未领取：{error}")
            return
        print(
            f"账号[{self.index}] 会员日券领取成功：activityId={data.get('activityId', MEMBER_DAY_ACTIVITY_ID)}"
        )

    def run_79_discount_coupon(self):
        if not ENABLE_79_DISCOUNT_COUPON:
            print(f"账号[{self.index}] 7.9 折券兑换：开关关闭（ASDCB_ENABLE_79_COUPON=1 开启）")
            return
        if time.localtime().tm_wday != 1:
            print(f"账号[{self.index}] 7.9 折券兑换：今天不是周二，跳过")
            return
        goods_payload = self.api_post(
            DISCOUNT_GOODS_DETAIL_PATH,
            {"goodsId": DISCOUNT_COUPON_GOODS_ID, "appid": MINI_APP_ID},
        )
        goods = self.ensure_success(goods_payload, "7.9 折券商品查询") or {}
        price = float(goods.get("pointsPrice") or 0)
        stock = int(goods.get("remainStocks") or 0)
        sale = (goods.get("timeCycleExtraVo") or {}).get("saleIng")
        daily_limit = int(goods.get("dailyOrderLimit") or 0)
        user_times = int(goods.get("userOrderTimes") or 0)
        points = float(self.query_points() or 0)
        if price != DISCOUNT_COUPON_POINTS:
            print(f"账号[{self.index}] 7.9 折券跳过：服务端积分价格为 {price:g}，不是预期的 10")
            return
        if sale is False or stock <= 0:
            print(f"账号[{self.index}] 7.9 折券跳过：活动未开始或库存不足（库存={stock}）")
            return
        if daily_limit and user_times >= daily_limit:
            print(f"账号[{self.index}] 7.9 折券跳过：今日已兑换（{user_times}/{daily_limit}）")
            return
        if points < price:
            print(f"账号[{self.index}] 7.9 折券跳过：积分不足（当前 {points:g}，需要 {price:g}）")
            return
        if self.dry_run:
            print(f"账号[{self.index}] dry-run：可兑换 7.9 折券，将消耗 {price:g} 积分")
            return
        order_payload = self.api_post(
            DISCOUNT_ORDER_CREATE_PATH,
            {"goodsId": DISCOUNT_COUPON_GOODS_ID, "goodsNum": 1, "channelCode": "", "appid": MINI_APP_ID},
        )
        order = self.ensure_success(order_payload, "7.9 折券创建兑换订单") or {}
        order_no = str(order.get("orderNo") or "")
        if not order_no:
            raise ApiError("7.9 折券创建订单未返回 orderNo")
        payment_payload = self.api_post(
            DISCOUNT_PAYMENT_PATH,
            {"orderNo": order_no, "orderSubType": 0, "appid": MINI_APP_ID},
        )
        payment = self.ensure_success(payment_payload, "7.9 折券兑换确认") or {}
        if int(payment.get("payComplete") or 0) != 1:
            raise ApiError(f"7.9 折券兑换未完成（orderNo={order_no}）")
        print(f"账号[{self.index}] 7.9 折券兑换成功：订单={order_no}，消耗 {price:g} 积分")

    @staticmethod
    def reward_progress(detail, rule):
        role = int(rule.get("signInCalculationRole") or 0)
        day_key = "totalDays" if role == 1 else "continuityTotal"
        day_label = "累计" if role == 1 else "连续"
        current_day = int(detail.get(day_key) or 0)
        rewards = sorted(
            [item for item in (rule.get("signInRoleList") or []) if item.get("signInDays")],
            key=lambda item: int(item.get("signInDays") or 0),
        )
        return day_label, current_day, rewards

    def print_reward_progress(self, detail, rule):
        if not rule:
            return
        day_label, current_day, rewards = self.reward_progress(detail, rule)
        base_reward = rule.get("signInBaseRewards")
        next_reward = next(
            (item for item in rewards if int(item.get("signInDays") or 0) > current_day),
            None,
        )
        message = f"账号[{self.index}] 奖励进度：{day_label}{current_day}天"
        if base_reward is not None:
            message += f"，每日{base_reward}积分"
        if next_reward:
            target = int(next_reward.get("signInDays") or 0)
            points = next_reward.get("presentIntegral") or 0
            message += f"，下一档{target}天奖励{points}积分（还差{target - current_day}天）"
        else:
            message += "，本活动没有尚未达到的奖励档位"
        print(message)

    def run_sign(self):
        detail = self.query_detail()
        rule = self.query_rule()
        activity_id = str(detail.get("activityId") or "")
        signed = int(detail.get("intraDay") or 0) == 1
        points_before = self.query_points()
        print(
            f"账号[{self.index}] 签到前：积分={points_before if points_before is not None else '-'}，"
            f"今日={'已签到' if signed else '未签到'}"
        )
        self.print_reward_progress(detail, rule)
        if signed:
            return
        if self.dry_run:
            print(f"账号[{self.index}] dry-run：跳过签到")
            return
        if not activity_id:
            raise ApiError("签到详情未返回 activityId")
        if not self.mobile or not self.user_name:
            self.query_profile()
        if not self.mobile:
            raise ApiError("账号未绑定手机号，请先在阿水大杯茶小程序完成会员资料")

        payload = self.api_post(
            SIGN_PATH,
            {
                "activityId": activity_id,
                "mobilePhone": self.mobile,
                "userName": self.user_name,
                "appid": MINI_APP_ID,
            },
        )
        self.ensure_success(payload, "签到")
        points_after = self.query_points()
        detail_after = self.query_detail()
        try:
            points_gain = float(points_after) - float(points_before)
        except (TypeError, ValueError):
            points_gain = None
        print(
            f"账号[{self.index}] 签到成功：积分={points_after if points_after is not None else '-'}，"
            f"连续签到={detail_after.get('continuityTotal', '-')}天"
        )
        if points_gain is not None:
            print(f"账号[{self.index}] 本次签到实际到账：{points_gain:g}积分")

        day_label, current_day, rewards = self.reward_progress(detail_after, rule)
        milestone = next(
            (
                item
                for item in rewards
                if int(item.get("signInDays") or 0) == current_day and current_day > 0
            ),
            None,
        )
        if milestone:
            reward_points = float(milestone.get("presentIntegral") or 0)
            base_reward = float(rule.get("signInBaseRewards") or 0)
            if points_gain is not None and points_gain > base_reward:
                print(
                    f"账号[{self.index}] {day_label}{current_day}天奖励已由签到接口自动发放，"
                    f"本次共到账{points_gain:g}积分"
                )
            else:
                print(
                    f"账号[{self.index}] 已达到{day_label}{current_day}天奖励档位"
                    f"（规则奖励{reward_points:g}积分），但服务端本次未额外发放；"
                    "该奖励整个活动仅可领取一次，可能此前已领取"
                )

    def run(self):
        if self.load_cache():
            print(f"账号[{self.index}] 使用持久化 token 缓存")
            if not self.validate_token():
                self.clear_cache()
                print(f"账号[{self.index}] 缓存 token 已失效，重新登录")
            elif not self.openid or not self.user_id:
                # 旧版缓存没有保存会员身份字段，重新登录一次补齐动态签名所需数据。
                self.clear_cache()
                print(f"账号[{self.index}] 缓存缺少会员身份字段，重新登录补齐")
        if not self.token:
            self.login()

        try:
            self.run_sign()
            self.run_member_day_coupon()
            self.run_79_discount_coupon()
        except ApiError as error:
            if not token_error(error):
                raise
            self.clear_cache()
            print(f"账号[{self.index}] 登录已失效，获取新 code 后重试一次")
            self.login()
            self.run_sign()
            self.run_member_day_coupon()
            self.run_79_discount_coupon()


def load_accounts():
    raw = os.environ.get("YYB_SERVER", "").strip()
    if not raw:
        print("请设置 YYB_SERVER，格式：yyb-go:8000@账号ID或OpenID，多账号换行")
        sys.exit(1)
    entries = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and line.strip() != "[object Object]"
    ]
    if not entries:
        print("YYB_SERVER 中没有有效账号")
        sys.exit(1)
    print(f"YYB_SERVER 共加载 {len(entries)} 个账号")
    return entries


def main():
    parser = argparse.ArgumentParser(description="阿水大杯茶 YYB 多账号签到")
    parser.add_argument("--dry-run", action="store_true", help="只查询，不执行签到")
    args = parser.parse_args()

    accounts = load_accounts()
    mode = "查询模式（--dry-run，不执行签到/领券/兑换）" if args.dry_run else "执行模式"
    print(f"========== {NAME}签到启动｜{mode} ==========")
    for index, entry in enumerate(accounts, 1):
        print(f"\n----------- 账号【{index}/{len(accounts)}】-----------")
        try:
            Account(index, entry, args.dry_run).run()
        except Exception as error:
            print(f"账号[{index}] 执行失败：{error}")
    print("\n========== 执行结束 ==========")


if __name__ == "__main__":
    main()
