#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 毛豆充
# cron: 12 8 * * *

"""毛豆充福利任务 YYB 版。

参考菠萝充电脚本的任务编排，但使用毛豆充 HAR 中确认的接口：动态微信
登录、积分查询、每日签到和观看视频。视频每天默认最多执行五次，并在每次
提交前重新读取服务端次数。

环境变量：
  YYB_SERVER：每行 ``YYB地址@账号ID或OpenID``，例如 yyb-go:8000@1
  MAODOUCHONG_WECHAT_APP_ID：覆盖微信 AppID，默认使用 HAR 的
      wxc7548b3f7181e9d9（业务请求 Header 仍为 hichar.user.wxapp）
  MAODOUCHONG_VIDEO_TIMES：视频次数，默认 5；实际执行不超过服务端上限
  MAODOUCHONG_VIDEO_DELAY：视频请求间隔秒数，默认 1
  MAODOUCHONG_LOTTERY：是否执行积分抽奖，默认 1；设为 0 可关闭
  MAODOUCHONG_LOTTERY_TIMES：每轮最多抽奖次数，默认 0 表示只要积分够就抽

旧的 ``MAOMAOCHONG_*`` 变量仍兼容一段时间。

抽奖接口已由 HAR 确认；脚本按可用抽奖积分计算次数，默认抽完本轮可用次数。
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


API_APP_ID = "hichar.user.wxapp"
WECHAT_APP_ID = os.getenv(
    "MAODOUCHONG_WECHAT_APP_ID",
    os.getenv("MAOMAOCHONG_WECHAT_APP_ID", "wxc7548b3f7181e9d9"),
)
API_BASE = "https://apiv2.hichar.cn"
TIMEOUT = 30
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


def json_response(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ScriptError(f"{action}返回非 JSON（HTTP {response.status_code}）") from exc
    if not isinstance(payload, dict):
        raise ScriptError(f"{action}返回格式异常")
    if not response.ok:
        raise ScriptError(f"{action}失败 HTTP {response.status_code}：{safe_text(payload)}")
    return payload


def ensure_ok(payload: dict[str, Any], action: str) -> None:
    if payload.get("code") not in (0, "0") or payload.get("success") is False:
        message = payload.get("msg") or payload.get("message") or payload.get("error")
        raise ScriptError(f"{action}失败：{safe_text(message or payload)}")


def yyb_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the operation result from current and legacy YYB envelopes.

    Current YYB responses wrap the wx.login result as ``data.result``. Older
    deployments may use ``data.data.result`` or return the result fields
    directly under ``data``. Keeping this normalization here prevents a valid
    code from being mistaken for an empty response when YYB is upgraded.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    result = data.get("result")
    if isinstance(result, dict):
        return result
    nested = data.get("data")
    if isinstance(nested, dict):
        result = nested.get("result")
        if isinstance(result, dict):
            return result
        return nested
    return data


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
                if isinstance(item, dict) and (
                    str(item.get("id")) == account.ref or str(item.get("openid")) == account.ref
                ):
                    account.remark = str(
                        item.get("remark") or item.get("nickname") or item.get("alias") or ""
                    ).strip()
                    break


class MaomaochongClient:
    def __init__(self, account: YybAccount) -> None:
        self.account = account
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": f"https://servicewechat.com/{WECHAT_APP_ID}/414/page-frame.html",
                "User-Agent": USER_AGENT,
                "appId": API_APP_ID,
            }
        )
        self.user_id: int | None = None
        self.user: dict[str, Any] = {}

    def get_code(self) -> str:
        try:
            response = self.session.post(
                self.account.server + "/wxapp/getCode",
                json={"ref": self.account.ref, "app_id": WECHAT_APP_ID},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ScriptError(f"YYB 获取微信 code 失败：{safe_text(exc)}") from exc
        payload = json_response(response, "YYB 获取微信 code")
        ensure_ok(payload, "YYB 获取微信 code")
        result = yyb_result(payload)
        code = result.get("code")
        if not code:
            # Include only a redacted, bounded response so diagnostics remain
            # useful without leaking OpenID or credentials into QingLong logs.
            raise ScriptError(f"YYB 未返回 wx.login code：{safe_text(payload)}")
        return str(code)

    def login(self) -> None:
        try:
            response = self.session.post(
                API_BASE + "/api/user/user/wechat-login",
                json={"code": self.get_code()},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ScriptError(f"毛豆充登录失败：{safe_text(exc)}") from exc
        payload = json_response(response, "毛豆充登录")
        ensure_ok(payload, "毛豆充登录")
        data = payload.get("data") or {}
        user = data.get("user") if isinstance(data, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(user, dict) or not token or not user.get("id"):
            raise AccountSkipped("毛豆充未返回完整用户凭证，可能尚未注册或授权小程序")
        self.user = user
        self.user_id = int(user["id"])
        self.session.headers["token"] = str(token)

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(method, API_BASE + path, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise ScriptError(f"{path} 请求失败：{safe_text(exc)}") from exc
        payload = json_response(response, path)
        ensure_ok(payload, path)
        return payload

    def profile(self) -> dict[str, Any]:
        data = self.request("GET", "/api/user/user/userInfo").get("data") or {}
        return data if isinstance(data, dict) else {}

    def points(self) -> int | None:
        data = self.request(
            "GET", "/api/user/welfare/userWelfarePoints", params={"userId": self.user_id}
        ).get("data") or {}
        try:
            return int(data.get("points")) if isinstance(data, dict) and data.get("points") is not None else None
        except (TypeError, ValueError):
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
        item = next((row for row in self.sign_records() if str(row.get("signDate")) == today), None)
        if not item:
            return False
        try:
            return int(item.get("consecutiveDays") or 0) > 0
        except (TypeError, ValueError):
            return str(item.get("consecutiveDays")).lower() in {"true", "yes", "已签到"}

    def sign(self) -> None:
        today = date.today().isoformat()
        item = next((row for row in self.sign_records() if str(row.get("signDate")) == today), {})
        self.request(
            "POST",
            "/api/user/welfare/userSign",
            json={
                "signDate": today,
                "points": item.get("points", 0),
                "userId": self.user_id,
                "consecutiveDays": item.get("consecutiveDays", 0),
            },
        )

    def video_once(self, task: dict[str, Any]) -> None:
        body = {key: task.get(key) for key in (
            "taskId", "taskName", "points", "status", "drawType", "otherJson",
            "reachTimes", "limitTimes", "nowTimes"
        )}
        body["userId"] = self.user_id
        self.request("POST", "/api/user/welfare/downWelfareJob", json=body)
        self.request(
            "POST", "/api/user/welfare/userVideRecord",
            json={"reach": 1, "type": 2, "userId": self.user_id},
        )

    def lottery_balance(self) -> int:
        """Return points currently available for the lottery draw."""
        data = self.request("GET", "/api/user/welfare/getUserPoints").get("data")
        try:
            return int(data or 0)
        except (TypeError, ValueError):
            return 0

    def draw_lottery(self) -> dict[str, Any]:
        """Perform the confirmed one-draw request and return the prize record."""
        data = self.request("POST", "/api/user/welfare/draw", json={}).get("data") or {}
        return data if isinstance(data, dict) else {}


def env_int(name: str, default: int | str, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        try:
            value = int(default)
        except (TypeError, ValueError):
            value = minimum
    return max(minimum, min(maximum, value))


def run_account(account: YybAccount) -> None:
    print(f"\n================ {account.label} ================")
    client = MaomaochongClient(account)
    client.login()
    profile = client.profile()
    name = profile.get("nickName") or client.user.get("nickName") or "-"
    start_points = client.points()
    print(
        f"登录成功：{name}，会员ID={client.user_id}，开始积分="
        f"{start_points if start_points is not None else '未知'}"
    )
    if client.signed_today():
        print("今日签到：已完成")
    else:
        client.sign()
        if not client.signed_today():
            raise ScriptError("签到请求成功，但未确认当天签到记录")
        print("今日签到：成功")

    target = env_int(
        "MAODOUCHONG_VIDEO_TIMES",
        os.getenv("MAOMAOCHONG_VIDEO_TIMES", "5"),
        0,
        20,
    )
    delay = env_int(
        "MAODOUCHONG_VIDEO_DELAY",
        os.getenv("MAOMAOCHONG_VIDEO_DELAY", "1"),
        0,
        60,
    )
    completed = 0
    for _ in range(target):
        task = next((row for row in client.tasks() if str(row.get("taskId")) == "1"), None)
        if not task:
            print("视频任务：服务端未返回 taskId=1，停止")
            break
        try:
            now, limit = int(task.get("nowTimes") or 0), int(task.get("limitTimes") or 0)
        except (TypeError, ValueError):
            now, limit = 0, 0
        if limit and now >= limit:
            print(f"视频任务：已达到服务端上限（{now}/{limit}）")
            break
        client.video_once(task)
        completed += 1
        print(f"视频任务：第 {completed} 次完成" + (f"（{now + 1}/{limit}）" if limit else ""))
        if delay and completed < target:
            time.sleep(delay)
    task_end_points = client.points()
    print(
        f"视频任务：本轮完成 {completed} 次；任务后积分="
        f"{task_end_points if task_end_points is not None else '未知'}"
    )
    lottery_enabled = os.getenv("MAODOUCHONG_LOTTERY", os.getenv("MAOMAOCHONG_LOTTERY", "1"))
    prizes: list[dict[str, Any]] = []
    if lottery_enabled.strip().lower() not in {"0", "false", "no", "off"}:
        lottery_points = client.points()
        available_draws = (lottery_points or 0) // 1000
        configured_draws = env_int(
            "MAODOUCHONG_LOTTERY_TIMES",
            os.getenv("MAOMAOCHONG_LOTTERY_TIMES", "0"),
            0,
            50,
        )
        draw_count = min(available_draws, configured_draws) if configured_draws else available_draws
        if draw_count <= 0:
            print(f"积分抽奖：当前积分 {lottery_points if lottery_points is not None else '未知'}，不足 1000，跳过")
        else:
            print(f"积分抽奖：当前积分 {lottery_points}，预计可抽 {available_draws} 次，本轮最多执行 {draw_count} 次")
            for draw_index in range(draw_count):
                current_points = client.points()
                if current_points is None or current_points < 1000:
                    print(f"积分抽奖：实时积分 {current_points if current_points is not None else '未知'}，停止")
                    break
                try:
                    prize = client.draw_lottery()
                except ScriptError as exc:
                    print(f"积分抽奖：第 {draw_index + 1} 次失败，停止后续抽奖：{safe_text(exc)}")
                    break
                prizes.append(prize)
                prize_name = prize.get("name") or prize.get("goodsName") or "已完成"
                print(
                    f"积分抽奖：第 {draw_index + 1}/{draw_count} 次，"
                    f"{safe_text(prize_name)}（消耗 1000 抽奖积分）"
                )
    else:
        print("积分抽奖：已通过 MAODOUCHONG_LOTTERY 关闭")

    end_points = client.points()
    if start_points is not None and task_end_points is not None and end_points is not None:
        task_gain = task_end_points - start_points
        net_gain = end_points - start_points
        print(f"积分统计：开始 {start_points} → 任务后 {task_end_points} → 结束 {end_points}")
        print(f"积分收益：任务赚取 {task_gain:+d}，本轮净收益 {net_gain:+d}")
    else:
        print("积分统计：部分积分接口未返回数值，无法计算本轮收益")
    if prizes:
        names = []
        for prize in prizes:
            prize_name = prize.get("name") or prize.get("goodsName") or "未知奖品"
            prize_id = prize.get("id") or "-"
            goods_id = prize.get("goodsId") or "-"
            goods_type = prize.get("goodsType") or "-"
            chance = prize.get("chance")
            chance_text = f", chance={chance}" if chance is not None else ""
            names.append(
                f"{safe_text(prize_name)}(id={prize_id}, goodsId={goods_id}, "
                f"goodsType={goods_type}{chance_text})"
            )
        print(f"抽奖收益：{len(prizes)} 次，{'; '.join(names)}；共消耗 {len(prizes) * 1000} 抽奖积分")


def main() -> int:
    try:
        accounts = parse_accounts()
    except ScriptError as exc:
        print(f"配置错误：{exc}")
        return 1
    load_remarks(accounts)
    accounts = filter_accounts(accounts, lambda account: account.ref, app_id=WECHAT_APP_ID)
    if not accounts:
        print("本轮没有需要执行的 YYB 账号")
        return 0
    print(f"共读取 {len(accounts)} 个 YYB 账号")
    success = 0
    for account in accounts:
        try:
            run_account(account)
            mark_ready(account.ref, app_id=WECHAT_APP_ID)
            success += 1
        except AccountSkipped as exc:
            mark_from_error(account.ref, str(exc), app_id=WECHAT_APP_ID)
            print(f"{account.label}已跳过：{safe_text(exc)}")
        except (ScriptError, requests.RequestException) as exc:
            mark_from_error(account.ref, str(exc), app_id=WECHAT_APP_ID)
            print(f"{account.label}执行失败：{safe_text(exc)}")
    print(f"\n执行完成：成功 {success} / 总计 {len(accounts)}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
