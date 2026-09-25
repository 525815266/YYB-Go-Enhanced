#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""莱充积分任务，YYB 多账号版。

根据 HAR 使用 AppID wxa68db1dabe823e7e。
脚本自动登录，并领取服务端已经确认完成的任务奖励，不伪造视频播放或分享回调。

Environment variables:
  YYB_SERVER：每行一个 YYB_URL@账号标识
  LAICHONG_APP_ID: defaults to wxa68db1dabe823e7e
  LAICHONG_AUTO_SIGN: default 1
  LAICHONG_AUTO_BIND_PHONE：默认 1，使用 YYB 手机号授权包自动绑定
  LAICHONG_CLAIM_READY: default 1
  LAICHONG_TRY_VIDEO/LAICHONG_TRY_SHARE：显式调试开关，默认 0
  LAICHONG_DRY_RUN：默认 0
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

try:
    from yyb_account_guard import filter_accounts, mark_from_error, mark_ready, set_status
except ImportError:  # Allow standalone local execution.
    def filter_accounts(values, ref_getter=None, **_):  # type: ignore[no-untyped-def]
        return list(values)

    def mark_from_error(*_args, **_kwargs):
        return None

    def mark_ready(*_args, **_kwargs):
        return None

    def set_status(*_args, **_kwargs):
        return None


APP_ID = os.getenv("LAICHONG_APP_ID", "wxa68db1dabe823e7e").strip()
BASE_URL = os.getenv("LAICHONG_BASE_URL", "https://saas.laichon.com").rstrip("/")
TIMEOUT = max(5, int(os.getenv("LAICHONG_TIMEOUT", "30")))
AUTO_SIGN = os.getenv("LAICHONG_AUTO_SIGN", "1").strip().lower() not in {"0", "false", "no", "off"}
AUTO_BIND_PHONE = os.getenv("LAICHONG_AUTO_BIND_PHONE", "1").strip().lower() not in {"0", "false", "no", "off"}
CLAIM_READY = os.getenv("LAICHONG_CLAIM_READY", "1").strip().lower() not in {"0", "false", "no", "off"}
TRY_VIDEO = os.getenv("LAICHONG_TRY_VIDEO", "0").strip().lower() in {"1", "true", "yes", "on"}
TRY_SHARE = os.getenv("LAICHONG_TRY_SHARE", "0").strip().lower() in {"1", "true", "yes", "on"}
DRY_RUN = os.getenv("LAICHONG_DRY_RUN", "0").strip().lower() in {"1", "true", "yes", "on"}
USER_AGENT = os.getenv(
    "LAICHONG_USER_AGENT",
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Mobile MicroMessenger/8.0.78 MiniProgram",
)


class ScriptError(RuntimeError):
    pass


@dataclass
class Account:
    index: int
    server: str
    ref: str
    remark: str = ""

    @property
    def label(self) -> str:
        base = f"账号{self.index}({self.ref})"
        return f"{self.remark} [{base}]" if self.remark else base


def flag(value: Any) -> bool:
    return str(value or "").strip().lower() not in {"0", "false", "no", "off", ""}


def safe_text(value: Any, limit: int = 260) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"(?i)(token|authorization|auth_code|openid|code)[=: ]+[^ ,}]+", r"\1=***", text)
    return text[:limit]


def parse_accounts() -> list[Account]:
    rows: list[Account] = []
    for raw in os.getenv("YYB_SERVER", "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "@" not in line or line == "[object Object]":
            continue
        endpoint, ref = line.rsplit("@", 1)
        endpoint, ref = endpoint.strip(), ref.strip()
        if not endpoint or not ref:
            continue
        if not endpoint.startswith(("http://", "https://")):
            endpoint = "http://" + endpoint
        rows.append(Account(len(rows) + 1, endpoint.rstrip("/"), ref))
    if not rows:
        raise ScriptError("未配置 YYB_SERVER，请按每行 YYB_URL@账号标识填写")
    return rows


def load_remarks(accounts: list[Account]) -> None:
    grouped: dict[str, list[Account]] = {}
    for account in accounts:
        grouped.setdefault(account.server, []).append(account)
    for server, rows in grouped.items():
        try:
            response = requests.get(server + "/accounts", timeout=10)
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else payload
            if not response.ok or not isinstance(data, list):
                continue
        except (requests.RequestException, ValueError, TypeError):
            continue
        for account in rows:
            for item in data:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id")) == account.ref or str(item.get("openid")) == account.ref:
                    account.remark = str(item.get("remark") or item.get("nickname") or "").strip()
                    break


def response_json(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ScriptError(f"{action}返回非 JSON，HTTP {response.status_code}") from exc
    if not isinstance(body, dict):
        raise ScriptError(f"{action}返回格式无效")
    if response.status_code >= 400:
        raise ScriptError(f"{action}失败，HTTP {response.status_code}：{safe_text(body)}")
    return body


def find_code(value: Any) -> str:
    """兼容不同 YYB 版本返回的 getCode 包装格式。"""
    if isinstance(value, dict):
        for key in ("wx_code", "login_code", "auth_code"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate and candidate not in {"0", "invalid", "null"}:
                return candidate
        for key in ("data", "result", "payload"):
            candidate = find_code(value.get(key))
            if candidate:
                return candidate
        candidate = value.get("code")
        if isinstance(candidate, str) and candidate and candidate not in {"0", "invalid", "null"}:
            return candidate
    elif isinstance(value, list):
        for item in value:
            candidate = find_code(item)
            if candidate:
                return candidate
    return ""


def json_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


class Client:
    def __init__(self, account: Account) -> None:
        self.account = account
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Referer": f"https://servicewechat.com/{APP_ID}/656/page-frame.html",
            }
        )
        self.token = ""
        self.user_id = ""
        self.is_bind: Any = None
        self.phone = ""

    def get_wx_code(self) -> str:
        try:
            response = self.session.post(
                self.account.server + "/wxapp/getCode",
                json={"ref": self.account.ref, "app_id": APP_ID},
                timeout=TIMEOUT,
            )
            payload = response_json(response, "YYB 获取登录 code")
        except requests.RequestException as exc:
            raise ScriptError(f"YYB 获取登录 code 网络异常：{safe_text(exc)}") from exc
        code = find_code(payload)
        if not code:
            raise ScriptError(f"YYB 未返回 wx.login code：{safe_text(payload)}")
        return code

    def get_phone_payload(self) -> dict[str, Any]:
        """从 YYB 获取一次性的 wx.getPhoneNumber 授权包。"""
        try:
            response = self.session.post(
                self.account.server + "/wxapp/getPhoneNumber",
                json={"ref": self.account.ref, "app_id": APP_ID},
                timeout=TIMEOUT,
            )
            payload = response_json(response, "YYB 获取手机号授权包")
        except requests.RequestException as exc:
            raise ScriptError(f"YYB 获取手机号授权包网络异常: {safe_text(exc)}") from exc
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ScriptError("YYB 未返回手机号授权包")
        result = data.get("result")
        if isinstance(result, dict):
            return result
        return data

    def bind_phone(self) -> None:
        """使用 YYB 返回的真实手机号授权 code 完成莱充绑定。"""
        phone_payload = self.get_phone_payload()
        phone_code = str(phone_payload.get("code") or "").strip()
        if not phone_code:
            raise ScriptError("请先在微信小程序内完成手机号授权，YYB 未返回可用授权 code")
        payload = self.post(
            "/api/app/auth/v1/getPhoneNumber",
            {"auth_code": phone_code, "source": 4},
        )
        data = json_data(payload)
        self.is_bind = data.get("is_bind", 1)
        self.phone = str(data.get("phone") or "")
        if str(self.is_bind) != "1":
            raise ScriptError("莱充手机号授权未生效，请先在微信小程序内重新授权手机号")

    def login(self, code: str) -> None:
        response = self.session.post(
            BASE_URL + "/api/app/auth/wxAppThirdLogin",
            json={"auth_code": code},
            timeout=TIMEOUT,
        )
        payload = response_json(response, "莱充登录")
        data = json_data(payload)
        token = str(data.get("token") or "").strip()
        if payload.get("code") not in (0, "0", None) or not token:
            raise ScriptError(f"莱充登录未返回 token：{safe_text(payload)}")
        self.token = token
        self.user_id = str(data.get("id") or "")
        self.is_bind = data.get("is_bind")
        self.phone = str(data.get("phone") or "")
        self.session.headers["Authorization"] = "Bearer " + token
        if AUTO_BIND_PHONE and str(self.is_bind) != "1":
            self.bind_phone()

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.post(BASE_URL + path, json=body or {}, timeout=TIMEOUT)
        payload = response_json(response, path)
        code = payload.get("code")
        if code not in (0, "0", None):
            raise ScriptError(f"{path}业务错误：{safe_text(payload.get('msg') or payload)}")
        return payload

    def points(self) -> int | None:
        payload = self.post("/api/app/task/pointsInfo")
        value = json_data(payload).get("points")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def sign_info(self) -> dict[str, Any]:
        return json_data(self.post("/api/app/task/signInfo"))

    def tasks(self) -> list[dict[str, Any]]:
        data = json_data(self.post("/api/app/task/list"))
        values = data.get("doing_tasks")
        return values if isinstance(values, list) else []

    def complete(self, period_id: Any) -> dict[str, Any]:
        return json_data(self.post("/api/app/task/complete", {"period_id": period_id}))

    def claim(self, period_id: Any) -> dict[str, Any]:
        return json_data(self.post("/api/app/task/claim", {"period_id": period_id}))


def task_name(item: dict[str, Any]) -> str:
    return str(((item.get("task_instance") or {}).get("template_data") or {}).get("name") or item.get("task_type") or "task")


def task_type(item: dict[str, Any]) -> int:
    try:
        return int(item.get("task_type") or 0)
    except (TypeError, ValueError):
        return 0


def claim_pending(client: Client, items: list[dict[str, Any]], claimed: list[str]) -> int:
    """领取服务端已经确认的奖励，包括分享奖励。"""
    count = 0
    if not CLAIM_READY or DRY_RUN:
        return count
    for item in items:
        period_id = item.get("id")
        if not period_id:
            continue
        try:
            pending = int(item.get("unclaimed_points") or 0)
        except (TypeError, ValueError):
            pending = 0
        if pending <= 0:
            continue
        name = task_name(item)
        client.claim(period_id)
        claimed.append(f"{name}+{pending}")
        count += 1
    return count


def run_account(account: Account) -> dict[str, Any]:
    client = Client(account)
    before = after = None
    claimed: list[str] = []
    actions: list[str] = []
    try:
        client.login(client.get_wx_code())
        before = client.points()
        sign = client.sign_info()
        progress = int(sign.get("current_progress") or 0)
        target = int(sign.get("target_progress") or 1)
        if AUTO_SIGN and not DRY_RUN and progress == 0 and sign.get("id"):
            client.complete(sign["id"])
            actions.append("签到完成")
        elif progress > 0:
            actions.append("今日已签到")

        items = client.tasks()
        claim_pending(client, items, claimed)
        for item in items:
            period_id = item.get("id")
            if not period_id:
                continue
            current = int(item.get("current_progress") or 0)
            target_count = int(item.get("target_progress") or 0)
            pending = int(item.get("unclaimed_points") or 0)
            kind = task_type(item)
            name = task_name(item)

            # 服务端已确认完成时领取奖励。
            if pending > 0:
                continue

            # 只有显式开启调试开关时才提交完成请求；任务必须先由客户端真实完成。
            if not DRY_RUN and current < target_count and ((kind == 5 and TRY_VIDEO) or (kind == 8 and TRY_SHARE)):
                result = client.complete(period_id)
                pending_after = int(result.get("unclaimed_points") or 0)
                if pending_after > 0 and CLAIM_READY:
                    client.claim(period_id)
                    claimed.append(f"{name}+{pending_after}")
                else:
                    actions.append(f"{name}已提交完成")

        # 分享/视频回调可能晚于 complete 返回，补刷一次避免本轮漏领。
        if CLAIM_READY and not DRY_RUN:
            time.sleep(0.3)
            refreshed = client.tasks()
            claim_pending(client, refreshed, claimed)
            if refreshed:
                items = refreshed

        after = client.points()
        mark_ready(account.ref, app_id=APP_ID)
        delta = None if before is None or after is None else after - before
        return {"success": True, "label": account.label, "before": before, "after": after, "delta": delta, "actions": actions, "claimed": claimed, "video": next((f"{x.get('current_progress', 0)}/{x.get('target_progress', 0)}" for x in items if task_type(x) == 5), "未知"), "share": next((f"{x.get('current_progress', 0)}/{x.get('target_progress', 0)}" for x in items if task_type(x) == 8), "未知")}
    except (requests.RequestException, ScriptError, ValueError) as exc:
        message = safe_text(exc)
        if "\u6ca1\u6709\u6743\u9650" in message or "no permission" in message.lower():
            set_status(account.ref, "unregistered", message, app_id=APP_ID)
        mark_from_error(account.ref, message, app_id=APP_ID)
        return {"success": False, "label": account.label, "before": before, "after": after, "error": message}


def notify(title: str, content: str) -> None:
    try:
        import notify  # type: ignore
        notify.send(title, content)
    except Exception:
        return


def main() -> None:
    accounts = parse_accounts()
    load_remarks(accounts)
    accounts = filter_accounts(accounts, lambda item: item.ref, app_id=APP_ID, log=print)
    print(f"莱充积分任务：共 {len(accounts)} 个 YYB 账号，AppID={APP_ID}")
    results = [run_account(account) for account in accounts]
    lines = ["莱充积分任务结果"]
    for result in results:
        if result.get("success"):
            delta = result.get("delta")
            delta_text = "未知" if delta is None else (f"+{delta}" if delta >= 0 else str(delta))
            lines.append(f"{result['label']}：积分 {result.get('before')} -> {result.get('after')}（{delta_text}）；操作={','.join(result.get('actions') or []) or '无'}；已领取={','.join(result.get('claimed') or []) or '无'}；视频={result.get('video')}；分享={result.get('share')}")
        else:
            lines.append(f"{result['label']}：失败，{result.get('error')}")
    text = "\n".join(lines)
    print(text)
    if flag(os.getenv("LAICHONG_NOTIFY", "1")):
        notify("莱充积分任务", text)


if __name__ == "__main__":
    main()
