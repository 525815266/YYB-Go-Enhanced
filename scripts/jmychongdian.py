#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""金茂悦积分兑换 + 余额充电守护（青龙/YYB 多账号版）。

配置优先使用 ``JMY_ACCOUNTS_JSON``，每个账号可分别绑定 YYB 标识、积分
Bearer、充电 Cookie 以及扫码得到的设备参数。例如：

JMY_ACCOUNTS_JSON=[
  {"name":"账号1", "yyb_ref":"1", "graphql_token":"Bearer ...",
   "cookie":"mobile_user=...; XKSESSION=...", "dev_id":"123", "socket_id":"4"}
]

也支持单账号环境变量：JMY_GRAPHQL_TOKEN、JMY_COOKIE、JMY_DEV_ID、JMY_SOCKET_ID。
YYB_SERVER 的格式与其它脚本一致：地址@账号ID或OpenID，每行一个。

说明：金茂悦充电页使用公众号/H5 Cookie（XKSESSION/mobile_user），并不是
YYB 的 wx.login code。脚本会调用 YYB 获取 code 作为账号探测；只有配置
JMY_CODE_EXCHANGE_URL 时，才会把 code POST 到用户自己的换票接口并读取返回的
token/cookie。不要把真实 Token、Cookie 写进仓库。
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests


# 积分活动对应的小程序 AppID；wx273... 是充电 H5 的公众号 OAuth AppID，二者不是一回事。
YYB_APP_ID = os.getenv("JMY_YYB_APP_ID", "wx9939a74ee8a8522a")
GRAPHQL_URL = os.getenv("JMY_GRAPHQL_URL", "https://shd.luxingiot.com/graphql")
EXCHANGE_THRESHOLD = int(os.getenv("JMY_EXCHANGE_THRESHOLD", "1000"))
EXCHANGE_INDEX = int(os.getenv("JMY_EXCHANGE_INDEX", "0"))
AUTO_EXCHANGE = os.getenv("JMY_AUTO_EXCHANGE", "0").lower() in {"1", "true", "yes", "on"}
VENDER_ID = os.getenv("JMY_VENDER_ID", "505")
CODE_EXCHANGE_URL = os.getenv("JMY_CODE_EXCHANGE_URL", "").strip()
REQUEST_TIMEOUT = int(os.getenv("JMY_REQUEST_TIMEOUT", "30"))
DRY_RUN = os.getenv("JMY_DRY_RUN", "0").lower() in {"1", "true", "yes"}
CHARGE_ENABLED = os.getenv("JMY_CHARGE_ENABLED", "1").lower() not in {"0", "false", "no"}
CHARGE_AFTER_EXCHANGE_ONLY = os.getenv("JMY_CHARGE_AFTER_EXCHANGE_ONLY", "1").lower() not in {"0", "false", "no"}
CHARGE_ALL_ACCOUNTS = os.getenv("JMY_CHARGE_ALL_ACCOUNTS", "0").lower() in {"1", "true", "yes"}
RUN_ACCOUNT = os.getenv("JMY_RUN_ACCOUNT", "").strip()
RUN_DEV_ID = os.getenv("JMY_RUN_DEV_ID", "").strip()
RUN_SOCKET_ID = os.getenv("JMY_RUN_SOCKET_ID", "").strip()

SIGN_QUERY = """
mutation activity($activity_id: Int!) {
  activityPush(id: $activity_id) {
    code message
    reward_log { action reward_type amount balance }
  }
}
"""
EXCHANGE_QUERY = "mutation CouponExchange($index: Int!) { couponExchange(index: $index) }"


@dataclass
class Account:
    name: str
    yyb_server: str = ""
    yyb_ref: str = ""
    graphql_token: str = ""
    cookie: str = ""
    dev_id: str = ""
    socket_id: str = ""
    app_id: str = YYB_APP_ID
    charge_enabled: bool = True


def _bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).lower() not in {"0", "false", "no", "off"}


def parse_yyb_server_lines(raw: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "@" not in line:
            continue
        endpoint, ref = line.rsplit("@", 1)
        endpoint = endpoint.strip().rstrip("/")
        ref = ref.strip()
        if endpoint and ref:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = "http://" + endpoint
            result.append((endpoint, ref))
    return result


def load_accounts() -> list[Account]:
    raw_json = os.getenv("JMY_ACCOUNTS_JSON", "").strip()
    default_token = os.getenv("JMY_GRAPHQL_TOKEN", "").strip()
    default_cookie = os.getenv("JMY_COOKIE", "").strip()
    default_dev_id = os.getenv("JMY_DEV_ID", "").strip()
    default_socket_id = os.getenv("JMY_SOCKET_ID", "").strip()
    if raw_json:
        try:
            values = json.loads(raw_json)
            if not isinstance(values, list):
                raise ValueError("JMY_ACCOUNTS_JSON 必须是数组")
            accounts: list[Account] = []
            for index, item in enumerate(values, 1):
                if not isinstance(item, dict):
                    raise ValueError(f"第 {index} 项不是对象")
                accounts.append(Account(
                    name=str(item.get("name") or f"账号{index}"),
                    yyb_server=str(item.get("yyb_server") or ""),
                    yyb_ref=str(item.get("yyb_ref") or ""),
                    graphql_token=str(item.get("graphql_token") or default_token),
                    cookie=str(item.get("cookie") or default_cookie),
                    dev_id=str(item.get("dev_id") or default_dev_id),
                    socket_id=str(item.get("socket_id") or default_socket_id),
                    app_id=str(item.get("app_id") or YYB_APP_ID),
                    charge_enabled=_bool(item.get("charge_enabled"), True),
                ))
            return accounts
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"JMY_ACCOUNTS_JSON 配置错误：{exc}") from exc

    yyb = parse_yyb_server_lines(os.getenv("YYB_SERVER", ""))
    if yyb:
        return [Account(name=f"账号{i}", yyb_server=server, yyb_ref=ref,
                        graphql_token=default_token, cookie=default_cookie,
                        dev_id=default_dev_id, socket_id=default_socket_id)
                for i, (server, ref) in enumerate(yyb, 1)]
    return [Account(
        name=os.getenv("JMY_ACCOUNT_NAME", "默认账号"),
        yyb_server=os.getenv("JMY_YYB_SERVER", "").strip(),
        yyb_ref=os.getenv("JMY_YYB_REF", "").strip(),
        graphql_token=os.getenv("JMY_GRAPHQL_TOKEN", "").strip(),
        cookie=os.getenv("JMY_COOKIE", "").strip(),
        dev_id=os.getenv("JMY_DEV_ID", "").strip(),
        socket_id=os.getenv("JMY_SOCKET_ID", "").strip(),
        app_id=os.getenv("JMY_APP_ID", YYB_APP_ID),
        charge_enabled=_bool(os.getenv("JMY_CHARGE_ENABLED"), True),
    )]


def yyb_code(account: Account) -> Optional[str]:
    if not account.yyb_server or not account.yyb_ref:
        print(f"[{account.name}] 未配置 YYB 标识，跳过 wx.login code 获取")
        return None
    url = account.yyb_server.rstrip("/") + "/wxapp/getCode"
    try:
        response = requests.post(url, json={"ref": account.yyb_ref, "app_id": account.app_id},
                                 timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        code = data.get("code") if isinstance(data, dict) else None
        if not code:
            raise RuntimeError(f"YYB 返回未包含 code：{str(data)[:300]}")
        print(f"[{account.name}] YYB 获取 code 成功")
        return str(code)
    except Exception as exc:
        print(f"[{account.name}] YYB 获取 code 失败：{exc}")
        return None


def apply_code_exchange(account: Account, code: Optional[str]) -> None:
    """可选的用户自建 code->token 适配口，避免把未知登录协议硬编码。"""
    if not CODE_EXCHANGE_URL or not code:
        return
    try:
        response = requests.post(CODE_EXCHANGE_URL, json={"code": code, "app_id": account.app_id,
                                                            "vender_id": VENDER_ID},
                                 timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            account.graphql_token = str(data.get("graphql_token") or data.get("token") or account.graphql_token)
            account.cookie = str(data.get("cookie") or account.cookie)
            account.dev_id = str(data.get("dev_id") or account.dev_id)
            account.socket_id = str(data.get("socket_id") or account.socket_id)
        print(f"[{account.name}] code 换会话接口返回成功")
    except Exception as exc:
        print(f"[{account.name}] code 换会话失败：{exc}")


def gql(account: Account, query: str, variables: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not account.graphql_token:
        print(f"[{account.name}] 未配置 JMY_GRAPHQL_TOKEN，跳过积分接口")
        return None
    headers = {
        "Content-Type": "application/json",
        "Authorization": account.graphql_token if account.graphql_token.lower().startswith("bearer ")
        else "Bearer " + account.graphql_token,
        "User-Agent": os.getenv("JMY_USER_AGENT", "Mozilla/5.0 MicroMessenger/8.0.43 NetType/WIFI Language/zh_CN"),
        "Referer": os.getenv("JMY_GRAPHQL_REFERER", "https://servicewechat.com/wx9939a74ee8a8522a/9/page-frame.html"),
        "x-provider-id": os.getenv("JMY_PROVIDER_ID", "wx9939a74ee8a8522a"),
    }
    payload = {"query": query, "variables": variables}
    for attempt in range(1, 4):
        try:
            response = requests.post(GRAPHQL_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if data.get("errors"):
                print(f"[{account.name}] GraphQL 错误：{str(data['errors'])[:500]}")
            return data
        except (requests.RequestException, ValueError) as exc:
            print(f"[{account.name}] GraphQL 第 {attempt}/3 次失败：{exc}")
            if attempt < 3:
                time.sleep(2 * attempt)
    return None


def run_points(account: Account) -> Optional[int]:
    """执行已知积分活动，返回最后一次返回的余额。"""
    if DRY_RUN:
        print(f"[{account.name}] [DRY RUN] 跳过积分签到/视频请求")
        return None
    if not account.graphql_token:
        print(f"[{account.name}] 未配置 JMY_GRAPHQL_TOKEN，跳过积分签到/兑换")
        return None
    balance: Optional[int] = None
    for index, activity_id in enumerate((1, 2, 2, 2), 1):
        if index > 1:
            wait_s = random.randint(5, 10)
            print(f"[{account.name}] 视频步骤等待 {wait_s} 秒")
            if not DRY_RUN:
                time.sleep(wait_s)
        result = gql(account, SIGN_QUERY, {"activity_id": activity_id})
        activity = (result or {}).get("data", {}).get("activityPush") if result else None
        if not isinstance(activity, dict):
            continue
        reward = activity.get("reward_log") or {}
        if reward.get("balance") is not None:
            try:
                balance = int(reward["balance"])
            except (TypeError, ValueError):
                pass
        print(f"[{account.name}] 积分步骤 {index}: {activity.get('message') or activity.get('code')}，余额={balance}")
    return balance


def exchange_one_yuan(account: Account, balance: Optional[int]) -> bool:
    if balance is None:
        return False
    if balance < EXCHANGE_THRESHOLD:
        print(f"[{account.name}] 当前积分 {balance}，未达到兑换阈值 {EXCHANGE_THRESHOLD}")
        return False
    if DRY_RUN:
        print(f"[{account.name}] [DRY RUN] 将兑换 index={EXCHANGE_INDEX} 的 1 元额度")
        return True
    result = gql(account, EXCHANGE_QUERY, {"index": EXCHANGE_INDEX})
    value = (result or {}).get("data", {}).get("couponExchange") if result else None
    ok = value in (1, True, "1", "true")
    print(f"[{account.name}] 1 元额度兑换{'成功' if ok else '失败'}：{value}")
    return ok


def run_charge_guard(account: Account) -> None:
    if not account.charge_enabled or not CHARGE_ENABLED:
        print(f"[{account.name}] 已关闭充电步骤")
        return
    if not account.cookie:
        print(f"[{account.name}] 未配置充电 Cookie（JMY_COOKIE），无法启动 H5 充电")
        return
    # 复用同目录的完整功率监控、异常停止和重启逻辑。
    try:
        import charge_guard_fixed as guard
    except ImportError as exc:
        raise RuntimeError("缺少 charge_guard_fixed.py，请将它与本脚本放在同一青龙脚本目录") from exc
    guard.COOKIE = account.cookie
    guard.DEFAULT_DEV_ID = account.dev_id or None
    guard.DEFAULT_SOCKET_ID = account.socket_id or None
    guard.CHARGE_LEN = int(os.getenv("JMY_CHARGE_LEN", str(guard.CHARGE_LEN)))
    guard.CACHE_FILE = Path(os.getenv("JMY_CHARGE_CACHE", f"./jmy_charge_{account.name}.json"))
    if os.getenv("JMY_BARK_URL"):
        guard.BARK_URL = os.getenv("JMY_BARK_URL")
    if DRY_RUN:
        print(f"[{account.name}] [DRY RUN] 跳过 charge_save 和功率监控")
        return
    session = guard.build_session()
    guard.unified_run(session)
    guard.monitor_until_stop(
        session,
        recover_rounds=int(os.getenv("JMY_RECOVER_ROUNDS", str(guard.AUTO_RECOVER_MAX_ROUNDS))),
        max_checks=int(os.getenv("JMY_MAX_CHECKS", "0")),
    )


def apply_runtime_overrides(accounts: list[Account]) -> None:
    """将二维码/人工选端口得到的一次性参数注入目标账号，不改持久化账号配置。"""
    if not (RUN_DEV_ID or RUN_SOCKET_ID or RUN_ACCOUNT):
        return
    target: Optional[Account] = None
    if RUN_ACCOUNT:
        for index, account in enumerate(accounts, 1):
            if RUN_ACCOUNT in {str(index), account.name, account.yyb_ref}:
                target = account
                break
    if target is None:
        target = accounts[0]
    if RUN_DEV_ID:
        target.dev_id = RUN_DEV_ID
    if RUN_SOCKET_ID:
        target.socket_id = RUN_SOCKET_ID
    print(f"[{target.name}] 已应用本次运行设备参数：dev_id={target.dev_id or '-'}，socket_id={target.socket_id or '-'}")


def main() -> int:
    try:
        accounts = load_accounts()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not accounts:
        print("没有可用账号")
        return 2
    apply_runtime_overrides(accounts)
    print(f"共加载 {len(accounts)} 个金茂悦账号")
    for index, account in enumerate(accounts, 1):
        print(f"\n================ {account.name} ================")
        code = yyb_code(account)
        apply_code_exchange(account, code)
        balance = run_points(account)
        if AUTO_EXCHANGE:
            exchanged = exchange_one_yuan(account, balance)
        else:
            exchanged = False
            print(f"[{account.name}] 自动兑换已关闭，仅记录积分余额 {balance}")
        if exchanged:
            wait_s = int(os.getenv("JMY_EXCHANGE_SETTLE_SECONDS", "2"))
            print(f"[{account.name}] 等待兑换到账 {wait_s} 秒后进入充电")
            if not DRY_RUN:
                time.sleep(max(0, wait_s))
        # 关闭自动兑换时，仍允许按现有余额/额度进入充电；只有开启自动兑换
        # 时才要求本轮兑换成功后再启动，避免“关了兑换也永远不充电”。
        exchange_gate_passed = exchanged or not AUTO_EXCHANGE
        if CHARGE_AFTER_EXCHANGE_ONLY and not exchange_gate_passed:
            print(f"[{account.name}] 未完成 1 元额度兑换，按安全策略跳过充电")
            continue
        if not CHARGE_ALL_ACCOUNTS and index > 1:
            print(f"[{account.name}] 默认只对第一个账号执行充电；如需多账号请设置 JMY_CHARGE_ALL_ACCOUNTS=1")
            continue
        run_charge_guard(account)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
