#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 旧衣客签到
# cron: 22 9 * * *
"""JYK (旧衣客) check-in script — adapted for YYB-GO.

Flow:
1. YYB /wxapp/getCode gets a WeChat mini-program code.
2. /api/index/get_openid exchanges code for access_token.
3. /api/checkin/* completes normal and ad check-in flows.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from yyb_account_guard import filter_accounts, mark_from_error, mark_ready

# ==================== top config ====================

TARGET_APPID = "wx3f0209cc35a953a4"
TARGET_VERSION = "68"
BASE_URL = "https://jyk.scjyx.com"
PAYCONFIG_ID = "2"

# YYB server from env (YYB-GO standard)
YYB_WX_SERVER = ""
YYB_REFS: List[str] = []  # Empty means auto fetch all refs from YYB /accounts.
for entry in os.environ.get("YYB_SERVER", "").splitlines():
    entry = entry.strip()
    if not entry:
        continue
    server, separator, ref = entry.rpartition("@")
    if not separator:
        server = entry
    server = server.strip().rstrip("/")
    if not YYB_WX_SERVER:
        YYB_WX_SERVER = server
    if separator and server == YYB_WX_SERVER and ref.strip():
        YYB_REFS.append(ref.strip())

MANUAL_ACCESS_TOKENS: List[str] = []

DO_NORMAL_CHECKIN = True
DO_AD_CHECKIN = True
AD_CHECKIN_TIMES = 7
SIGN_DELAY_SECONDS = 0

PID = ""
DEVICE_INFO = "android_MEIZU_22_370"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; MEIZU 22 "
    "Build/BQ2A.251110.001-BP2A.250605.031.A3; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/146.0.7680.178 Mobile Safari/537.36 "
    "MicroMessenger/8.0.60.2860(0x28003C37) WeChat/arm64 "
    "Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)

REQUEST_TIMEOUT = 30
RETRY_TIMES = 3
RETRY_BASE_DELAY = 3


# ==================== helpers ====================


def log(msg: str) -> None:
    print(msg, flush=True)


def mask(value: Any) -> str:
    text = str(value or "")
    if len(text) <= 12:
        return text
    return f"{text[:6]}...{text[-4:]}"


def normalize_server(server: str) -> str:
    server = str(server or "").strip().rstrip("/")
    if not server:
        return ""
    if server.startswith("http://") or server.startswith("https://"):
        return server
    return f"http://{server}"


def json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text[:300]


def extract_code(data: Any) -> str:
    if not isinstance(data, dict):
        return ""

    direct = data.get("code") or data.get("wx_code")
    if direct not in (None, "", 0):
        return str(direct)

    nested = data.get("Data") or data.get("data") or {}
    if isinstance(nested, dict):
        nested_code = nested.get("code") or nested.get("wx_code")
        if nested_code not in (None, "", 0):
            return str(nested_code)
        result = nested.get("result") or {}
        if isinstance(result, dict):
            result_code = result.get("code") or result.get("wx_code")
            if result_code not in (None, "", 0):
                return str(result_code)

    return ""


def extract_accounts_payload(data: Any) -> List[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("accounts", "data", "list", "rows", "result"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_accounts_payload(value)
            if nested:
                return nested
    return []


def extract_ref(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in ("ref", "openid", "openId", "wxid", "wx_id", "id", "account"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def extract_label(item: Any, fallback: str) -> str:
    if not isinstance(item, dict):
        return fallback
    for key in ("remark", "name", "nickname", "nickName", "label", "wxid", "openid", "ref"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


# ==================== YYB code provider ====================


@dataclass
class CodeAccount:
    label: str
    ref: str


class YybCodeProvider:
    def __init__(self, server: str):
        self.server = normalize_server(server)
        self.session = requests.Session()
        self.session.verify = False

    def list_accounts(self) -> List[CodeAccount]:
        refs = [str(x).strip() for x in YYB_REFS if str(x).strip()]
        if refs:
            return [CodeAccount(label=x, ref=x) for x in refs]

        if not self.server:
            log("未配置 YYB_SERVER，且 YYB_REFS 为空")
            return []

        url = f"{self.server}/accounts"
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            data = json_or_text(response)
            items = extract_accounts_payload(data)
        except Exception as exc:
            log(f"读取 YYB /accounts 异常：{str(exc)[:120]}")
            return []

        accounts: List[CodeAccount] = []
        seen = set()
        for item in items:
            ref = extract_ref(item)
            if not ref or ref in seen:
                continue
            seen.add(ref)
            accounts.append(CodeAccount(label=extract_label(item, ref), ref=ref))

        log(f"YYB /accounts 解析到 {len(accounts)} 个账号")
        return accounts

    def get_code(self, account: CodeAccount) -> Optional[str]:
        if not self.server:
            log("未配置 YYB_SERVER，无法请求 /wxapp/getCode")
            return None

        url = f"{self.server}/wxapp/getCode"
        payload = {"app_id": TARGET_APPID, "ref": account.ref}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 MicroMessenger/8.0.50",
        }

        for attempt in range(1, RETRY_TIMES + 1):
            try:
                response = self.session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
                data = json_or_text(response)
                code = extract_code(data)
                if code:
                    log(f"{mask(account.label)} 获取 wx code 成功")
                    return code
                log(f"{mask(account.label)} 获取 code 为空 resp={str(data)[:160]}")
            except Exception as exc:
                log(f"{mask(account.label)} 获取 code 异常：{str(exc)[:120]}")

            if attempt < RETRY_TIMES:
                time.sleep(RETRY_BASE_DELAY * attempt)

        return None


# ==================== JYK client ====================


@dataclass
class BusinessAccount:
    label: str
    access_token: str
    uid: str = ""


class JykClient:
    def __init__(self, account: BusinessAccount):
        self.account = account
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update(self.common_headers(account.access_token))

    @staticmethod
    def common_headers(access_token: str = "") -> Dict[str, str]:
        headers = {
            "content-type": "application/json",
            "X-Payconfig-Id": PAYCONFIG_ID,
            "Referer": f"https://servicewechat.com/{TARGET_APPID}/{TARGET_VERSION}/page-frame.html",
            "User-Agent": USER_AGENT,
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    @staticmethod
    def exchange_code(wx_code: str, label: str) -> Optional[BusinessAccount]:
        url = f"{BASE_URL}/api/index/get_openid"
        payload = {"code": wx_code, "pid": PID, "device_info": DEVICE_INFO}
        session = requests.Session()
        session.verify = False
        try:
            response = session.post(
                url,
                json=payload,
                headers=JykClient.common_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            data = json_or_text(response)
        except Exception as exc:
            log(f"{mask(label)} code 换 token 异常：{str(exc)[:120]}")
            return None

        if not isinstance(data, dict) or data.get("errno") != 0:
            log(f"{mask(label)} code 换 token 失败 resp={str(data)[:200]}")
            return None

        info = data.get("data") or {}
        token = str(info.get("access_token") or "")
        if not token:
            log(f"{mask(label)} 响应缺少 access_token")
            return None

        user = info.get("userInfo") or {}
        nickname = user.get("nickname") or label
        uid = str(info.get("uid") or user.get("uid") or "")
        log(f"{mask(label)} 登录成功 uid={uid or '-'} nick={mask(nickname)}")
        return BusinessAccount(label=str(nickname or label), access_token=token, uid=uid)

    def request_json(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = f"{BASE_URL}{path}"
        try:
            if method.upper() == "GET":
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            else:
                response = self.session.post(url, json=payload or {}, timeout=REQUEST_TIMEOUT)
            data = json_or_text(response)
        except Exception as exc:
            log(f"{self.account.label} 请求异常 {path}: {str(exc)[:120]}")
            return None

        if not isinstance(data, dict):
            log(f"{self.account.label} 响应非 JSON {path}: {str(data)[:160]}")
            return None
        return data

    def home(self) -> Dict[str, Any]:
        data = self.request_json("GET", "/api/checkin/home") or {}
        if data.get("errno") != 0:
            log(f"{self.account.label} home失败 resp={str(data)[:200]}")
        return data.get("data") or {}

    def prepare_and_sign(self, prepare_path: str, sign_path: str, token_label: str) -> bool:
        prepared = self.request_json("POST", prepare_path) or {}
        if prepared.get("errno") != 0:
            log(f"{self.account.label} {token_label} prepare失败 resp={str(prepared)[:200]}")
            return False

        body = prepared.get("data") or {}
        ad_token = str(body.get("ad_token") or "")
        if not ad_token:
            log(f"{self.account.label} {token_label} prepare未返回ad_token")
            return False

        if SIGN_DELAY_SECONDS > 0:
            log(f"{self.account.label} {token_label} 等待 {SIGN_DELAY_SECONDS}s 后提交")
            time.sleep(SIGN_DELAY_SECONDS)

        signed = self.request_json("POST", sign_path, {"ad_token": ad_token}) or {}
        if signed.get("errno") != 0:
            log(f"{self.account.label} {token_label} sign失败 resp={str(signed)[:240]}")
            return False

        result = signed.get("data") or {}
        reward = result.get("reward") or {}
        amount = reward.get("amount")
        snapshot = result.get("snapshot") or {}
        log(f"{self.account.label} {token_label} 成功，奖励={amount}，状态={json.dumps(snapshot, ensure_ascii=False)}")
        return True

    def run_normal_checkin(self, home_data: Dict[str, Any]) -> bool:
        if not DO_NORMAL_CHECKIN:
            return False
        if not home_data.get("enabled"):
            log(f"{self.account.label} 普通签到未开启")
            return False
        if home_data.get("today_signed") or not home_data.get("can_sign"):
            log(f"{self.account.label} 普通签到：今日已完成或不可签")
            return False
        return self.prepare_and_sign(
            "/api/checkin/prepare",
            "/api/checkin/sign",
            "普通签到",
        )

    def run_ad_checkin(self, home_data: Dict[str, Any]) -> int:
        if not DO_AD_CHECKIN or AD_CHECKIN_TIMES <= 0:
            return 0

        ad = home_data.get("ad") or {}
        if not ad.get("enabled"):
            log(f"{self.account.label} 广告签到未开启")
            return 0

        done = 0
        for _ in range(AD_CHECKIN_TIMES):
            current_home = self.home() if done else home_data
            current_ad = current_home.get("ad") or {}
            today_count = int(current_ad.get("today_count") or 0)
            daily_limit = int(current_ad.get("daily_limit") or 0)
            if daily_limit and today_count >= daily_limit:
                log(f"{self.account.label} 广告签到：已达上限 {today_count}/{daily_limit}")
                break
            if not current_ad.get("can_sign"):
                log(f"{self.account.label} 广告签到：当前不可签 today_count={today_count}")
                break

            if self.prepare_and_sign(
                "/api/checkin/ad/prepare",
                "/api/checkin/ad/sign",
                f"广告签到({today_count + 1}/{daily_limit or '?'})",
            ):
                done += 1
                time.sleep(1)
            else:
                break

        return done

    def run(self) -> None:
        home_data = self.home()
        integral_before = home_data.get("integral", "-")
        log(f"{self.account.label} 初始积分={integral_before} streak={home_data.get('streak_days', '-')}")

        self.run_normal_checkin(home_data)
        after_normal = self.home()
        self.run_ad_checkin(after_normal)

        final_home = self.home()
        ad = final_home.get("ad") or {}
        log(
            f"{self.account.label} 完成：积分={final_home.get('integral', '-')} "
            f"普通已签={final_home.get('today_signed')} "
            f"广告={ad.get('today_count', '-')}/{ad.get('daily_limit', '-')}"
        )


# ==================== account loading / main ====================


def load_manual_accounts() -> List[BusinessAccount]:
    accounts: List[BusinessAccount] = []
    for index, token in enumerate(MANUAL_ACCESS_TOKENS, 1):
        value = str(token or "").strip()
        if value:
            accounts.append(BusinessAccount(label=f"manual-{index}", access_token=value))
    return accounts


def auto_fetch_accounts() -> List[BusinessAccount]:
    provider = YybCodeProvider(YYB_WX_SERVER)
    code_accounts = provider.list_accounts()
    business_accounts: List[BusinessAccount] = []

    for index, code_account in enumerate(code_accounts, 1):
        log(f"处理账号[{index}/{len(code_accounts)}] {mask(code_account.label)}")
        wx_code = provider.get_code(code_account)
        if not wx_code:
            continue
        business = JykClient.exchange_code(wx_code, code_account.label)
        if business:
            business_accounts.append(business)
        if index < len(code_accounts):
            time.sleep(2)

    log(f"业务账号获取成功 {len(business_accounts)} / {len(code_accounts)}")
    return business_accounts


def main() -> int:
    accounts = load_manual_accounts() or auto_fetch_accounts()
    accounts = filter_accounts(accounts, lambda account: account.ref, app_id=TARGET_APPID, log=log)
    if not accounts:
        log("未获取到业务账号。请检查 YYB_SERVER、YYB_REFS 或 MANUAL_ACCESS_TOKENS。")
        return 1

    for index, account in enumerate(accounts, 1):
        log(f"\n===== 账号[{index}/{len(accounts)}] {mask(account.label)} =====")
        try:
            JykClient(account).run()
            mark_ready(account.ref, app_id=TARGET_APPID)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f"{account.label} 执行异常：{str(exc)[:160]}")
            mark_from_error(account.ref, str(exc), app_id=TARGET_APPID)
        if index < len(accounts):
            time.sleep(2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
