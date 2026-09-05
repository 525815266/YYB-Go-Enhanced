#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 极兔速递签到
# cron: 35 8 * * *

"""极兔速递微信小程序签到，支持 YYB_SERVER 多账号。

环境变量：
  YYB_SERVER  每行一个“YYB地址@账号ID或OpenID”，例如 yyb-go:8000@1

依赖：requests
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from yyb_account_guard import filter_accounts, mark_from_error, mark_ready


APP_ID = "wxe37801988179d0a5"
API_BASE = "https://applets.jtexpress.com.cn/applets"
SIGN_KEY = "APPLETS_KEY"
TIMEOUT = 30
LOGIN_EXPIRED_CODE = 135010037
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.75 MiniProgramEnv/iOS"
)


class ScriptError(RuntimeError):
    pass


class LoginExpired(ScriptError):
    pass


@dataclass
class YybAccount:
    index: int
    server: str
    ref: str
    remark: str = ""

    @property
    def label(self) -> str:
        base = f"账号 {self.ref}" if self.ref.isdigit() else f"账号 {self.index}"
        return f"{self.remark}（{base}）" if self.remark else base


def safe_text(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"(?i)(token|authorization|authToken)[=: ]+[^ ,}]+", r"\1=***", text)
    text = re.sub(r"(?<!\d)1\d{9}(\d)(?!\d)", r"1*********\1", text)
    return text[:240]


def parse_accounts() -> list[YybAccount]:
    accounts: list[YybAccount] = []
    for line in os.getenv("YYB_SERVER", "").splitlines():
        line = line.strip()
        if not line or "@" not in line or line == "[object Object]":
            continue
        server, ref = (part.strip() for part in line.split("@", 1))
        if not server or not ref:
            continue
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        accounts.append(YybAccount(len(accounts) + 1, server.rstrip("/"), ref))
    if not accounts:
        raise ScriptError("未配置 YYB_SERVER，格式：yyb-go:8000@账号ID或OpenID")
    return accounts


def response_json(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ScriptError(f"{action}返回非 JSON，HTTP {response.status_code}") from exc
    if not response.ok:
        message = payload.get("msg") or payload.get("message") or ""
        raise ScriptError(f"{action}失败，HTTP {response.status_code} {safe_text(message)}")
    if not isinstance(payload, dict):
        raise ScriptError(f"{action}返回格式异常")
    return payload


def yyb_result(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    nested = data.get("data") if isinstance(data, dict) else {}
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict) and isinstance(nested, dict):
        result = nested.get("result")
    return result if isinstance(result, dict) else (data if isinstance(data, dict) else {})


def load_remarks(accounts: list[YybAccount]) -> None:
    by_server: dict[str, list[YybAccount]] = {}
    for account in accounts:
        by_server.setdefault(account.server, []).append(account)
    for server, server_accounts in by_server.items():
        try:
            response = requests.get(server + "/accounts", timeout=10, proxies={"http": None, "https": None})
            payload: Any = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not response.ok or not isinstance(rows, list):
                continue
        except (requests.RequestException, ValueError):
            continue
        for account in server_accounts:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                matches = str(row.get("id")) == account.ref or row.get("openid") == account.ref
                if matches:
                    account.remark = str(
                        row.get("remark") or row.get("nickname") or row.get("alias") or ""
                    ).strip()
                    break


class JtClient:
    def __init__(self, account: YybAccount) -> None:
        self.account = account
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept": "*/*",
                "Content-Type": "application/json;charset=utf-8",
                "Referer": f"https://servicewechat.com/{APP_ID}/450/page-frame.html",
                "User-Agent": USER_AGENT,
            }
        )
        self.token = ""
        self.user_info: dict[str, Any] = {}

    def _yyb(self, endpoint: str) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.account.server + endpoint,
                json={"ref": self.account.ref, "app_id": APP_ID},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ScriptError(f"YYB {endpoint} 请求失败：{safe_text(exc)}") from exc
        payload = response_json(response, f"YYB {endpoint}")
        if payload.get("code") != 0:
            raise ScriptError(f"YYB {endpoint} 失败：{safe_text(payload.get('msg'))}")
        return yyb_result(payload)

    def _wx_login(self) -> dict[str, Any]:
        code = str(self._yyb("/wxapp/getCode").get("code") or "")
        if not code:
            raise ScriptError("YYB 未返回 wx.login code")
        try:
            response = self.session.post(
                API_BASE + "/wx/login", json={"code": code}, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            raise ScriptError(f"极兔微信登录失败：{safe_text(exc)}") from exc
        payload = response_json(response, "极兔微信登录")
        data = payload.get("data") or {}
        if payload.get("code") != 1 or not isinstance(data, dict) or not data.get("openid"):
            raise ScriptError(payload.get("msg") or "极兔微信登录未返回用户身份")
        return data

    def authenticate(self) -> None:
        info = self._wx_login()
        token = str(info.get("token") or "")
        if not token:
            phone_code = str(self._yyb("/wxapp/getPhoneNumber").get("code") or "")
            if not phone_code:
                raise ScriptError(
                    "该账号未绑定极兔手机号，且 YYB 未返回手机号动态 code；"
                    "请先确认此微信账号支持手机号授权"
                )
            timestamp = int(time.time() * 1000)
            raw = f"{info['openid']}{timestamp}{SIGN_KEY}"
            sign = hashlib.md5(hashlib.md5(raw.encode()).hexdigest().encode()).hexdigest()
            body = {
                "code": phone_code,
                "uuid": info.get("uuid") or "",
                "openid": info["openid"],
                "times": timestamp,
                "sign": sign,
                "unionid": info.get("unionid") or "",
            }
            try:
                response = self.session.post(API_BASE + "/v2/bindPhone", json=body, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise ScriptError(f"极兔手机号绑定失败：{safe_text(exc)}") from exc
            payload = response_json(response, "极兔手机号绑定")
            info = payload.get("data") or {}
            token = str(info.get("token") or "") if isinstance(info, dict) else ""
            if payload.get("code") != 1 or not token:
                raise ScriptError(payload.get("msg") or "极兔手机号绑定未返回 token")
            print("首次授权手机号成功")
        self.token = token
        self.user_info = info
        self.session.headers["authToken"] = token

    def request(self, method: str, path: str, *, retry: bool = True, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(method, API_BASE + path, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise ScriptError(f"{path} 请求失败：{safe_text(exc)}") from exc
        payload = response_json(response, path)
        message = str(payload.get("msg") or payload.get("message") or "")
        expired = payload.get("code") == LOGIN_EXPIRED_CODE or any(
            word in message for word in ("登录已失效", "登录已过期", "请重新登录")
        )
        if expired:
            if not retry:
                raise LoginExpired(message or "登录已失效")
            self.authenticate()
            return self.request(method, path, retry=False, **kwargs)
        return payload

    def profile(self) -> dict[str, Any]:
        payload = self.request("GET", "/user/qureyMyselfGrow")
        data = payload.get("data") or {}
        if payload.get("code") != 1 or not isinstance(data, dict):
            raise ScriptError(payload.get("msg") or "会员信息查询失败")
        return data

    def is_signed(self) -> tuple[Optional[bool], Any]:
        payload = self.request("GET", "/user/isSign")
        if payload.get("code") != 1:
            raise ScriptError(payload.get("msg") or "签到状态查询失败")
        data = payload.get("data")
        value: Any = data
        if isinstance(data, dict):
            value = next(
                (
                    data[key]
                    for key in ("isSign", "isSigned", "signed", "status", "flag")
                    if key in data
                ),
                None,
            )
        if isinstance(value, bool):
            return value, data
        if isinstance(value, (int, float)):
            return value != 0, data
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "signed", "已签到"}:
                return True, data
            if normalized in {"false", "0", "no", "unsigned", "未签到", ""}:
                return False, data
        return None, data

    def sign(self) -> tuple[dict[str, Any], str]:
        payload = self.request("POST", "/user/sign")
        if payload.get("code") != 1:
            raise ScriptError(payload.get("msg") or "签到失败")
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        return data, str(payload.get("msg") or payload.get("message") or "")

    def tasks(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/user/qureyGrowConfig")
        data = payload.get("data") or []
        return data if payload.get("code") == 1 and isinstance(data, list) else []

    def sent_orders(self) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            "/orderMerge/expressDelivery/page",
            params={"current": 1, "size": 20, "searchType": 1, "type": ""},
        )
        data = payload.get("data") or {}
        records = data.get("records") if isinstance(data, dict) else []
        return records if payload.get("code") == 1 and isinstance(records, list) else []

    def receive_shared_order(self, sender: "JtClient", order_id: Any) -> None:
        receiver_openid = self.user_info.get("openid")
        sender_member_id = sender.user_info.get("numberId")
        if not receiver_openid or not sender_member_id or not order_id:
            raise ScriptError("分享运单缺少接收者openid、分享者memberId或orderId")
        payload = self.request(
            "POST",
            "/user/shareOrder",
            json={
                "beSharedOpenid": receiver_openid,
                "orderId": order_id,
                "memberId": sender_member_id,
                "timestamp": int(time.time() * 1000),
            },
        )
        if payload.get("code") != 1:
            raise ScriptError(payload.get("msg") or "分享运单回调失败")


def mask_phone(value: Any) -> str:
    phone = str(value or "")
    return phone[:3] + "****" + phone[-4:] if len(phone) == 11 else "-"


def task_summary(tasks: list[dict[str, Any]]) -> str:
    names = {"sign": "签到", "share": "分享运单", "order": "每日首寄", "register": "注册", "login": "登录"}
    output: list[str] = []
    for item in tasks:
        action = str(item.get("memberAction") or "")
        name = names.get(action, action or "未知任务")
        state = "已完成" if item.get("isLimit") else "未完成"
        reward = item.get("growValue")
        output.append(f"{name}:{state}" + (f"(+{reward})" if reward not in (None, "") else ""))
    return "；".join(output) if output else "未返回任务数据"


def task_completed(tasks: list[dict[str, Any]], action: str) -> bool:
    for item in tasks:
        if str(item.get("memberAction") or "") == action:
            value = item.get("isLimit")
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes"}
            return bool(value)
    return False


def signed_status_text(status: Optional[bool], raw: Any) -> str:
    if status is True:
        return "已签到"
    if status is False:
        return "未签到"
    return "无法解析" + (f"（原始值：{safe_text(raw)}）" if raw is not None else "")


def run_account(account: YybAccount) -> JtClient:
    print(f"\n================ {account.label} ================")
    client = JtClient(account)
    client.authenticate()
    before = client.profile()
    print(
        f"登录成功：手机号 {mask_phone(before.get('mobile'))}，"
        f"当前成长值 {before.get('growValue', '-')}"
    )
    signed_before, raw_before = client.is_signed()
    print(f"签到前状态：{signed_status_text(signed_before, raw_before)}")
    if signed_before is True:
        print("服务端确认今日已签到，本轮无需重复签到")
    else:
        print("签到前未确认已签到，正在调用官方签到接口 /user/sign")
        result, message = client.sign()
        reward = result.get("growValue") or result.get("grow") or result.get("score")
        day = result.get("day")
        details = []
        if reward not in (None, "", 0, "0"):
            details.append(f"成长值 +{reward}")
        if day not in (None, ""):
            details.append(f"连续 {day} 天")
        if message:
            details.append(message)
        print("签到接口返回成功" + ("：" + "，".join(details) if details else ""))

    signed_after, raw_after = client.is_signed()
    after = client.profile()
    tasks = client.tasks()
    completed = signed_after is True or task_completed(tasks, "sign")
    print(f"签到后状态：{signed_status_text(signed_after, raw_after)}")
    print(f"签到后成长值：{after.get('growValue', '-')}")
    print("任务状态：" + task_summary(tasks))
    if not completed:
        raise ScriptError("签到接口调用后，签到状态和任务列表均未确认完成")
    print("签到结果校验：已完成")
    return client


def complete_share_tasks(clients: list[JtClient]) -> None:
    print("\n================ 运单任务处理 ================")
    for sender in clients:
        tasks = sender.tasks()
        if task_completed(tasks, "order"):
            print(f"{sender.account.label} 每日首寄：已完成")
        else:
            print(f"{sender.account.label} 每日首寄：未完成（需真实寄件下单，脚本不会创建订单）")

        if task_completed(tasks, "share"):
            print(f"{sender.account.label} 分享运单：已完成")
            continue
        orders = [item for item in sender.sent_orders() if item.get("orderId")]
        receiver = next(
            (
                item
                for item in clients
                if item is not sender
                and item.user_info.get("openid")
                and item.user_info.get("openid") != sender.user_info.get("openid")
            ),
            None,
        )
        if not orders:
            print(f"{sender.account.label} 分享运单：跳过（账号没有可分享的真实寄件单）")
            continue
        if receiver is None:
            print(f"{sender.account.label} 分享运单：跳过（没有其他可用 YYB 账号接收分享）")
            continue
        order = orders[0]
        print(
            f"{sender.account.label} 分享运单：由 {receiver.account.label} 接收，"
            f"orderId={safe_text(order.get('orderId'))}"
        )
        try:
            receiver.receive_shared_order(sender, order.get("orderId"))
            time.sleep(1)
            if not task_completed(sender.tasks(), "share"):
                raise ScriptError("分享回调已发送，但发送方任务状态仍未完成")
            print(f"{sender.account.label} 分享运单：已完成并通过任务状态校验")
        except ScriptError as exc:
            print(f"{sender.account.label} 分享运单失败：{safe_text(exc)}")


def main() -> int:
    try:
        accounts = parse_accounts()
    except ScriptError as exc:
        print(f"配置错误：{exc}")
        return 1
    load_remarks(accounts)
    accounts = filter_accounts(accounts, lambda account: account.ref, app_id=APP_ID)
    if not accounts:
        print("本轮没有需要执行的 YYB 账号")
        return 0
    print(f"共读取 {len(accounts)} 个 YYB 账号")
    success = 0
    clients: list[JtClient] = []
    for account in accounts:
        try:
            clients.append(run_account(account))
            mark_ready(account.ref, app_id=APP_ID)
            success += 1
        except (ScriptError, requests.RequestException) as exc:
            mark_from_error(account.ref, str(exc), app_id=APP_ID)
            print(f"{account.label}执行失败：{safe_text(exc)}")
    if clients:
        complete_share_tasks(clients)
    print(f"\n执行完成：成功 {success} / 总计 {len(accounts)}")
    return 0 if success == len(accounts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
