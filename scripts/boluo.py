#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 菠萝充电-浪潮
# cron: 12 8 * * *

"""菠萝充电-浪潮福利任务（YYB 版）。

通过 YYB_SERVER 获取每个微信账号的一次性 wx.login code，然后调用菠萝充电
小程序的公开福利接口。脚本只执行已在抓包中确认的动作：每日签到一次、视频
任务最多三次，并在每次视频前重新读取服务端次数，避免超过当日限制。

青龙环境变量：
  YYB_SERVER  每行一个 ``YYB地址@账号ID或OpenID``，例如 ``yyb-go:8000@1``
  BOLUO_WECHAT_APP_ID  覆盖微信原始小程序 AppID；默认取 HAR 中的
                       ``wxc7548b3f7181e9d9``。后端请求 Header 仍使用
                       ``hichar.user.wxapp``。
  BOLUO_VIDEO_TIMES  每日视频次数，默认 3，最大不超过服务端 limitTimes
  BOLUO_VIDEO_DELAY  视频回调之间的等待秒数，默认 1

抽奖接口尚未出现在提供的 HAR 中，因此本版不猜测 endpoint，也不会伪造抽奖
请求；日志会显示当前积分，待补充抽奖页面抓包后再接入。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

from yyb_account_guard import filter_accounts, mark_from_error, mark_ready


# 菠萝后端的业务 Header AppID 与微信原始小程序 AppID 是两回事。
API_APP_ID = "hichar.user.wxapp"
WECHAT_APPID = os.getenv("BOLUO_WECHAT_APP_ID", "wxc7548b3f7181e9d9")
API_BASE = "https://apiv2.hichar.cn"
TIMEOUT = 30
DEFAULT_VIDEO_TIMES = 3
MAX_VIDEO_TIMES = 3
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.76 MiniProgramEnv/iOS"
)


class ScriptError(RuntimeError):
    pass


class AccountSkipped(ScriptError):
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
    text = re.sub(r"(?i)(token|authorization|openid|code)[=: ]+[^ ,}]+", r"\1=***", text)
    text = re.sub(r"(?<!\d)1\d{9}(\d)(?!\d)", r"1*********\1", text)
    return text[:260]


def parse_accounts() -> list[YybAccount]:
    accounts: list[YybAccount] = []
    for raw in os.getenv("YYB_SERVER", "").splitlines():
        line = raw.strip()
        if not line or "@" not in line or line == "[object Object]":
            continue
        server, ref = (part.strip() for part in line.split("@", 1))
        if not server or not ref:
            continue
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        accounts.append(YybAccount(len(accounts) + 1, server.rstrip("/"), ref))
    if not accounts:
        raise ScriptError("未配置 YYB_SERVER，格式：地址@账号ID或OpenID")
    return accounts


def response_json(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ScriptError(f"{action}返回非 JSON（HTTP {response.status_code}）") from exc
    if not isinstance(payload, dict):
        raise ScriptError(f"{action}返回格式异常")
    if not response.ok:
        message = payload.get("msg") or payload.get("message") or payload.get("error")
        raise ScriptError(f"{action}失败 HTTP {response.status_code}：{safe_text(message)}")
    return payload


def payload_ok(payload: dict[str, Any], action: str) -> None:
    # 菠萝接口统一以 code=0/success=true 表示成功。
    code = payload.get("code")
    if code not in (0, "0") or payload.get("success") is False:
        message = payload.get("msg") or payload.get("message") or payload.get("error")
        raise ScriptError(f"{action}失败：{safe_text(message or payload)}")


def load_remarks(accounts: list[YybAccount]) -> None:
    grouped: dict[str, list[YybAccount]] = {}
    for account in accounts:
        grouped.setdefault(account.server, []).append(account)
    for server, rows in grouped.items():
        try:
            response = requests.get(server + "/accounts", timeout=10)
            payload: Any = response.json()
            items = payload.get("data") if isinstance(payload, dict) else payload
            if not response.ok or not isinstance(items, list):
                continue
        except (requests.RequestException, ValueError):
            continue
        for account in rows:
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id")) == account.ref or str(item.get("openid")) == account.ref:
                    account.remark = str(
                        item.get("remark") or item.get("nickname") or item.get("alias") or ""
                    ).strip()
                    break


class BoluoClient:
    def __init__(self, account: YybAccount) -> None:
        self.account = account
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": f"https://servicewechat.com/{WECHAT_APPID}/414/page-frame.html",
                "User-Agent": USER_AGENT,
                "appId": API_APP_ID,
            }
        )
        self.token = ""
        self.user_id: int | None = None
        self.user: dict[str, Any] = {}

    def wx_code(self) -> str:
        try:
            response = self.session.post(
                self.account.server + "/wxapp/getCode",
                json={"ref": self.account.ref, "app_id": WECHAT_APPID},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ScriptError(f"YYB 获取微信 code 失败：{safe_text(exc)}") from exc
        payload = response_json(response, "YYB 获取微信 code")
        payload_ok(payload, "YYB 获取微信 code")
        data = payload.get("data") or {}
        if isinstance(data, dict) and isinstance(data.get("account"), dict):
            info = data["account"]
            self.account.remark = str(
                info.get("remark") or info.get("nickname") or info.get("alias") or self.account.remark
            ).strip()
        nested = data.get("data") if isinstance(data, dict) else None
        code = nested.get("code") if isinstance(nested, dict) else None
        code = code or (data.get("code") if isinstance(data, dict) else None)
        if not code:
            raise ScriptError("YYB 未返回 wx.login code")
        return str(code)

    def login(self) -> None:
        try:
            response = self.session.post(
                API_BASE + "/api/user/user/wechat-login",
                json={"code": self.wx_code()},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ScriptError(f"菠萝微信登录失败：{safe_text(exc)}") from exc
        payload = response_json(response, "菠萝微信登录")
        payload_ok(payload, "菠萝微信登录")
        data = payload.get("data") or {}
        user = data.get("user") if isinstance(data, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(user, dict) or not token or not user.get("id"):
            raise AccountSkipped("菠萝登录未返回完整用户凭证，可能尚未注册或授权该小程序")
        self.user = user
        self.user_id = int(user["id"])
        self.token = str(token)
        self.session.headers["token"] = self.token

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(method, API_BASE + path, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise ScriptError(f"{path} 请求失败：{safe_text(exc)}") from exc
        payload = response_json(response, path)
        payload_ok(payload, path)
        return payload

    def profile(self) -> dict[str, Any]:
        return (self.request("GET", "/api/user/user/userInfo").get("data") or {})

    def points(self) -> int | None:
        data = self.request(
            "GET", "/api/user/welfare/userWelfarePoints", params={"userId": self.user_id}
        ).get("data") or {}
        if isinstance(data, dict):
            value = data.get("points")
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None
        return None

    def tasks(self) -> list[dict[str, Any]]:
        data = self.request(
            "POST", "/api/user/welfare/welfareTaskList", json={"userId": self.user_id}
        ).get("data") or []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def sign_records(self) -> list[dict[str, Any]]:
        data = self.request(
            "GET", "/api/user/welfare/userSignInVo", params={"userId": self.user_id}
        ).get("data") or []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def signed_today(self) -> bool:
        today = date.today().isoformat()
        for item in self.sign_records():
            if str(item.get("signDate")) != today:
                continue
            # 该接口会预先返回未来奖励日程；只有 consecutiveDays>0 才表示今天已完成。
            value = item.get("consecutiveDays")
            try:
                return int(value or 0) > 0
            except (TypeError, ValueError):
                return str(value).strip().lower() in {"true", "yes", "signed", "已签到"}
        return False

    def sign(self) -> None:
        records = self.sign_records()
        today = date.today().isoformat()
        reward = next((item for item in records if str(item.get("signDate")) == today), {})
        points = reward.get("points", 0)
        self.request(
            "POST",
            "/api/user/welfare/userSign",
            json={
                "signDate": today,
                "points": points,
                "userId": self.user_id,
                "consecutiveDays": reward.get("consecutiveDays", 0),
            },
        )

    def video_once(self, task: dict[str, Any]) -> None:
        body = {
            "taskId": task.get("taskId", 1),
            "taskName": task.get("taskName", "观看视频"),
            "points": task.get("points", 0),
            "status": task.get("status", 0),
            "drawType": task.get("drawType", 0),
            "otherJson": task.get("otherJson"),
            "reachTimes": task.get("reachTimes", 0),
            "limitTimes": task.get("limitTimes", 0),
            "nowTimes": task.get("nowTimes", 0),
            "userId": self.user_id,
        }
        self.request("POST", "/api/user/welfare/downWelfareJob", json=body)
        self.request(
            "POST", "/api/user/welfare/userVideRecord",
            json={"reach": 1, "type": 2, "userId": self.user_id},
        )


def int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def run_account(account: YybAccount) -> None:
    print(f"\n================ {account.label} ================")
    client = BoluoClient(account)
    client.login()
    profile = client.profile()
    name = profile.get("nickName") or client.user.get("nickName") or "-"
    points_before = client.points()
    print(f"登录成功：{name}，会员ID={client.user_id}，当前积分={points_before if points_before is not None else '-'}")

    if client.signed_today():
        print("今日签到：已完成")
    else:
        client.sign()
        if not client.signed_today():
            raise ScriptError("签到请求成功，但重新查询未确认今日已签到")
        print("今日签到：成功")

    target = int_env("BOLUO_VIDEO_TIMES", DEFAULT_VIDEO_TIMES, 0, MAX_VIDEO_TIMES)
    delay = max(0, int_env("BOLUO_VIDEO_DELAY", 1, 0, 60))
    done = 0
    for _ in range(target):
        task = next((item for item in client.tasks() if str(item.get("taskId")) == "1"), None)
        if not task:
            print("视频任务：服务端未返回任务，停止")
            break
        try:
            now = int(task.get("nowTimes") or 0)
            limit = int(task.get("limitTimes") or 0)
        except (TypeError, ValueError):
            now, limit = 0, 0
        if limit > 0 and now >= limit:
            print(f"视频任务：今日已达到服务端上限（{now}/{limit}）")
            break
        client.video_once(task)
        done += 1
        print(f"视频任务：第 {done} 次完成" + (f"（{now + 1}/{limit}）" if limit else ""))
        if delay and done < target:
            time.sleep(delay)

    points_after = client.points()
    print(f"视频任务：本轮完成 {done} 次；当前积分={points_after if points_after is not None else '-'}")
    print("积分抽奖：HAR 未发现已确认接口，本版暂不调用")


def main() -> int:
    try:
        accounts = parse_accounts()
    except ScriptError as exc:
        print(f"配置错误：{exc}")
        return 1
    load_remarks(accounts)
    accounts = filter_accounts(accounts, lambda account: account.ref, app_id=WECHAT_APPID)
    if not accounts:
        print("本轮没有需要执行的 YYB 账号")
        return 0
    print(f"共读取 {len(accounts)} 个 YYB 账号")
    success = 0
    for account in accounts:
        try:
            run_account(account)
            mark_ready(account.ref, app_id=WECHAT_APPID)
            success += 1
        except AccountSkipped as exc:
            mark_from_error(account.ref, str(exc), app_id=WECHAT_APPID)
            print(f"{account.label}已跳过：{safe_text(exc)}")
        except (ScriptError, requests.RequestException) as exc:
            mark_from_error(account.ref, str(exc), app_id=WECHAT_APPID)
            print(f"{account.label}执行失败：{safe_text(exc)}")
    print(f"\n执行完成：成功 {success} / 总计 {len(accounts)}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
