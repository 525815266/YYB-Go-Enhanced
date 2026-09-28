#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 響Livehouse会员日抽奖
# cron: 10 16 * * 0,1,2,3
"""汇翼云会员日抽奖，适配 YYB Go 和青龙。

YYB_SERVER 每行一个：YYB地址@账号ID或OpenID

环境变量：
  HYY_TURNTABLE_ID       抽奖活动 ID，默认 1027
  HYY_SHOP_UID           活动 shop_uid，默认 9
  HYY_STORE_ID           门店 ID，默认 1677
  HYY_ACCESS_TOKEN       单账号兼容配置；不建议多账号共用
  HYY_ACCESS_TOKENS      多账号映射，每行 ref=JWT，也支持账号序号=JWT
  HYY_ACCESS_TOKEN_FILE   JSON 映射文件或 ref=JWT 文本文件
  HYY_EXTERNAL_USERID    单账号兼容配置，直接跳过小程序 token 映射
  HYY_EXTERNAL_USERIDS   多账号映射，每行 ref=external_userid
  HYY_EXTERNAL_USERID_FILE 映射文件
  HYY_CACHE_FILE          自动缓存 external_userid；青龙默认写入
                          /ql/data/config/hyy_turntable_accounts.json
  HYY_MAX_DRAWS          本轮最多抽奖次数，默认 5
  HYY_DRAW               是否真正抽奖，默认 1；设为 0 只查询
  HYY_DELAY               每次抽奖间隔秒数，默认 1.5
  HYY_NOTIFY              是否调用青龙 sendNotify，默认 1

说明：/wxbook/login 只能用 YYB code 换出 openid/session_key；该小程序
的 access_token 是汇翼云自己的 JWT，来自小程序已有登录态/手机号授权，
并不由 wx.login 直接返回。因此脚本支持按账号注入该 JWT，拒绝猜测或
复用其他账号的身份。拿到 JWT 后可长期放在青龙环境变量或映射文件中。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


APP_ID = "wxab79cb37a805c1ca"
ISAVEU_BASE = "https://www.isaveu.cn"
HYY_BASE = "https://api.huiyielec.cn"
TIMEOUT = 30


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
        shown = self.ref if self.ref.isdigit() else str(self.index)
        return f"{self.remark}（账号{shown}）" if self.remark else f"账号{shown}"


def flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def safe(value: Any, limit: int = 260) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"(?i)(token|authorization|openid|unionid|session_key|code)\s*[=:]\s*[^ ,;}]+", r"\1=***", text)
    return text[:limit]


def mask_id(value: Any) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "***" if text else "-"
    return text[:4] + "***" + text[-4:]


def json_response(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ScriptError(f"{action}返回非 JSON（HTTP {response.status_code}）") from exc
    if not isinstance(payload, dict):
        raise ScriptError(f"{action}返回格式异常")
    if not response.ok:
        msg = payload.get("msg") or payload.get("message") or payload.get("error")
        raise ScriptError(f"{action}失败 HTTP {response.status_code}: {safe(msg)}")
    return payload


def parse_accounts() -> list[Account]:
    accounts: list[Account] = []
    for raw in os.getenv("YYB_SERVER", "").splitlines():
        line = raw.strip()
        if not line or "@" not in line or line == "[object Object]":
            continue
        server, ref = (part.strip() for part in line.split("@", 1))
        if not server or not ref:
            continue
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        accounts.append(Account(len(accounts) + 1, server.rstrip("/"), ref))
    if not accounts:
        raise ScriptError("未配置 YYB_SERVER，格式：yyb-go:8000@账号ID或OpenID")
    return accounts


def load_remarks(accounts: list[Account]) -> None:
    grouped: dict[str, list[Account]] = {}
    for account in accounts:
        grouped.setdefault(account.server, []).append(account)
    for server, rows in grouped.items():
        try:
            response = requests.get(server + "/accounts", timeout=10)
            payload = response.json()
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
                    account.remark = str(item.get("remark") or item.get("nickname") or "").strip()
                    break


def parse_mapping_text(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif "@" in line:
            key, value = line.split("@", 1)
        else:
            continue
        key, value = key.strip(), value.strip()
        if key and value:
            result[key] = value
    return result


def load_mapping(env_name: str, file_env_name: str) -> dict[str, str]:
    result = parse_mapping_text(os.getenv(env_name, ""))
    filename = os.getenv(file_env_name, "").strip()
    if not filename:
        return result
    path = Path(filename)
    if not path.is_file():
        print(f"⚠️ 映射文件不存在：{path}")
        return result
    try:
        text = path.read_text(encoding="utf-8-sig")
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            result.update({str(k): str(v) for k, v in parsed.items() if v})
        else:
            result.update(parse_mapping_text(text))
    except OSError as exc:
        print(f"⚠️ 读取映射文件失败：{safe(exc)}")
    return result


def cache_path() -> Path:
    configured = os.getenv("HYY_CACHE_FILE", "").strip()
    if configured:
        return Path(configured)
    ql_config = Path("/ql/data/config")
    if ql_config.is_dir():
        return ql_config / "hyy_turntable_accounts.json"
    return Path(__file__).with_name(".hyy_turntable_accounts.json")


def load_cache() -> dict[str, str]:
    path = cache_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            rows = payload.get("accounts", payload)
            if isinstance(rows, dict):
                return {str(k): str(v) for k, v in rows.items() if v}
    except (OSError, ValueError) as exc:
        print(f"⚠️ 读取 external_userid 缓存失败：{safe(exc)}")
    return {}


def save_cache(mapping: dict[str, str]) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"appid": APP_ID, "accounts": mapping}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        print(f"⚠️ 保存 external_userid 缓存失败：{safe(exc)}")


def mapped_value(
    account: Account, mapping: dict[str, str], single_name: str, allow_single: bool
) -> str:
    single = os.getenv(single_name, "").strip()
    if single and allow_single:
        return single
    for key in (account.ref, str(account.index), account.remark):
        if key and mapping.get(key):
            return mapping[key].strip()
    return ""


def unwrap_yyb_result(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            return result
        nested = data.get("data")
        if isinstance(nested, dict) and isinstance(nested.get("result"), dict):
            return nested["result"]
        return data
    return {}


class HyyClient:
    def __init__(self, account: Account) -> None:
        self.account = account
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept": "*/*",
                "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 MicroMessenger MiniProgram",
                "Referer": f"https://servicewechat.com/{APP_ID}/60/page-frame.html",
            }
        )
        self.access_token = ""
        self.mini_openid = ""
        self.external_userid = ""

    def get_code(self) -> str:
        try:
            response = self.session.post(
                self.account.server + "/wxapp/getCode",
                json={"ref": self.account.ref, "app_id": APP_ID},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ScriptError(f"YYB 获取 code 失败：{safe(exc)}") from exc
        payload = json_response(response, "YYB 获取 code")
        if payload.get("code") not in (0, "0", None):
            raise ScriptError(f"YYB 获取 code 失败：{safe(payload.get('msg') or payload.get('message'))}")
        code = str(unwrap_yyb_result(payload).get("code") or "")
        if not code:
            raise ScriptError("YYB 未返回 wx.login code")
        return code

    def mini_login(self, code: str) -> dict[str, Any]:
        body = {
            "appid": APP_ID,
            "code": code,
            "access_token": "",
            "shop_id": os.getenv("HYY_STORE_ID", "1677").strip(),
            "mini_source": 1,
        }
        try:
            response = self.session.post(
                ISAVEU_BASE + "/wxbook/login",
                json=body,
                headers={"Content-Type": "application/json", "api-token": ""},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ScriptError(f"汇翼云小程序登录请求失败：{safe(exc)}") from exc
        payload = json_response(response, "汇翼云小程序登录")
        data = payload.get("data")
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise ScriptError(f"汇翼云小程序登录失败：{safe(payload.get('msg') or payload.get('message'))}")
        self.mini_openid = str(data.get("openid") or "")
        return data

    def get_external_userid(self, token: str) -> str:
        self.access_token = token.strip()
        body = {
            "turntable_id": os.getenv("HYY_TURNTABLE_ID", "1027").strip(),
            "shop_uid": os.getenv("HYY_SHOP_UID", "9").strip(),
            "access_token": self.access_token,
            "appid": APP_ID,
            "shop_id": os.getenv("HYY_STORE_ID", "1677").strip(),
            "mini_source": 1,
        }
        try:
            response = self.session.post(
                ISAVEU_BASE + "/wxbook/vip/hyy_turntable_is_mini",
                json=body,
                headers={"Content-Type": "application/json", "api-token": self.access_token},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ScriptError(f"获取会员日入口失败：{safe(exc)}") from exc
        payload = json_response(response, "获取会员日入口")
        if payload.get("code") != 0:
            raise ScriptError(f"获取会员日入口失败：{safe(payload.get('msg') or payload.get('message'))}")
        jump_url = str(payload.get("data") or "")
        parsed = urlparse(jump_url)
        query = parsed.query
        if not query and "?" in parsed.fragment:
            query = parsed.fragment.split("?", 1)[1]
        external = parse_qs(query).get("external_userid", [""])[0]
        if not external:
            raise ScriptError("会员日入口未返回 external_userid")
        self.external_userid = external
        return external

    def hyy_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.get(HYY_BASE + path, params=params, headers={"Authorization": ""}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ScriptError(f"{path}请求失败：{safe(exc)}") from exc
        payload = json_response(response, path)
        if payload.get("result") is False:
            raise ScriptError(str(payload.get("msg") or f"{path}业务失败"))
        return payload

    def hyy_post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                HYY_BASE + path,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": ""},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ScriptError(f"{path}请求失败：{safe(exc)}") from exc
        payload = json_response(response, path)
        if payload.get("result") is False:
            raise ScriptError(str(payload.get("msg") or f"{path}业务失败"))
        return payload

    def draw(self) -> str:
        data = {
            "shop_uid": os.getenv("HYY_SHOP_UID", "9").strip(),
            "turntable_id": os.getenv("HYY_TURNTABLE_ID", "1027").strip(),
            "external_userid": self.external_userid,
            "store_id": os.getenv("HYY_STORE_ID", "1677").strip(),
            "mass_type": "",
            "mass_id": "",
        }
        payload = self.hyy_post("/app/turntable/draw", data)
        if payload.get("result") is False:
            raise ScriptError(str(payload.get("msg") or "抽奖失败"))
        prize = payload.get("data") or {}
        if isinstance(prize, dict):
            prize = prize.get("prize") or {}
        return str(prize.get("prize_name") if isinstance(prize, dict) else prize or payload.get("msg") or "未知奖品")

    def run(self, token: str, external_override: str = "") -> list[str]:
        if external_override:
            self.external_userid = external_override
        elif token:
            self.get_external_userid(token)
        else:
            raise ScriptError(
                "YYB 已换出小程序 openid，但该服务的会员日接口需要汇翼云 JWT；"
                "请设置 HYY_ACCESS_TOKENS（按账号映射）或 HYY_EXTERNAL_USERIDS"
            )

        common = {
            "shop_uid": os.getenv("HYY_SHOP_UID", "9").strip(),
            "turntable_id": os.getenv("HYY_TURNTABLE_ID", "1027").strip(),
            "external_userid": self.external_userid,
        }
        info_payload = self.hyy_get("/app/turntable/getInfo", common)
        info_data = info_payload.get("data") if isinstance(info_payload.get("data"), dict) else {}
        info = info_data.get("info") if isinstance(info_data.get("info"), dict) else info_data
        if not isinstance(info, dict):
            raise ScriptError("活动信息格式异常")
        if not activity_open(info):
            raise ScriptError(f"活动当前未开放：{info.get('status_txt') or '不在活动时间'}")

        count_payload = self.hyy_get(
            "/app/turntable/count",
            {**common, "store_id": os.getenv("HYY_STORE_ID", "1677").strip()},
        )
        count_data = count_payload.get("data") if isinstance(count_payload.get("data"), dict) else {}
        try:
            remaining = max(0, int(count_data.get("count", 0)))
        except (TypeError, ValueError):
            remaining = 0
        limit = min(remaining, integer("HYY_MAX_DRAWS", 5, 0, 5))
        print(f"{self.account.label} 活动进行中，剩余抽奖次数：{remaining}，本轮执行：{limit}")
        if not flag("HYY_DRAW", True) or limit == 0:
            return []

        prizes: list[str] = []
        delay = number("HYY_DELAY", 1.5, 0.5, 10.0)
        for index in range(limit):
            if index:
                time.sleep(delay)
            try:
                prize = self.draw()
                prizes.append(prize)
                print(f"{self.account.label} 第 {index + 1} 次抽奖：{prize}")
            except ScriptError as exc:
                print(f"{self.account.label} 第 {index + 1} 次抽奖失败：{safe(exc)}")
                break
        prize_payload = self.hyy_post(
            "/app/turntable/myPrize",
            {**common, "store_id": os.getenv("HYY_STORE_ID", "1677").strip(), "page": 1, "page_size": 999},
        )
        prize_data = prize_payload.get("data") if isinstance(prize_payload.get("data"), dict) else {}
        if isinstance(prize_data, dict):
            print(f"{self.account.label} 历史中奖记录：{prize_data.get('count', 0)} 条")
        return prizes


def activity_open(info: dict[str, Any]) -> bool:
    if str(info.get("status")) not in {"20", "1", "True", "true"}:
        return False
    now = datetime.now()
    start = str(info.get("start_time") or "")
    end = str(info.get("end_time") or "")
    try:
        if start and now < datetime.strptime(start, "%Y-%m-%d %H:%M:%S"):
            return False
        if end and now > datetime.strptime(end, "%Y-%m-%d %H:%M:%S"):
            return False
    except ValueError:
        pass
    rule = info.get("rule") if isinstance(info.get("rule"), dict) else {}
    weekdays = rule.get("weekday") or info.get("weekday")
    if isinstance(weekdays, list) and weekdays:
        allowed = {str(x.get("week")) for x in weekdays if isinstance(x, dict)}
        if allowed and str(now.isoweekday()) not in allowed:
            return False
    start_period = str(rule.get("start_period") or info.get("start_period") or "")
    end_period = str(rule.get("end_period") or info.get("end_period") or "")
    if start_period and end_period:
        current = now.strftime("%H:%M")
        if not (start_period <= current <= end_period):
            return False
    return True


def notify(title: str, content: str) -> None:
    if not flag("HYY_NOTIFY", True):
        return
    try:
        from sendNotify import send  # type: ignore

        send(title, content)
    except Exception:
        pass


def main() -> None:
    try:
        accounts = parse_accounts()
    except ScriptError as exc:
        print(f"❌ {exc}")
        return
    load_remarks(accounts)
    token_map = load_mapping("HYY_ACCESS_TOKENS", "HYY_ACCESS_TOKEN_FILE")
    external_map = load_mapping("HYY_EXTERNAL_USERIDS", "HYY_EXTERNAL_USERID_FILE")
    cached_external = load_cache()
    for key, value in cached_external.items():
        external_map.setdefault(key, value)
    print(f"汇翼云会员日抽奖：共加载 {len(accounts)} 个 YYB 账号")
    if len(accounts) > 1 and (
        os.getenv("HYY_ACCESS_TOKEN", "").strip()
        or os.getenv("HYY_EXTERNAL_USERID", "").strip()
    ):
        print("⚠️ 多账号模式已忽略单账号凭据，请用 HYY_ACCESS_TOKENS/HYY_EXTERNAL_USERIDS 按账号映射")
    summary: list[str] = []
    for account in accounts:
        print(f"\n================ {account.label} ================")
        client = HyyClient(account)
        try:
            code = client.get_code()
            login_data = client.mini_login(code)
            print(f"{account.label} 小程序登录成功，openid：{mask_id(login_data.get('openid'))}")
            token = mapped_value(account, token_map, "HYY_ACCESS_TOKEN", len(accounts) == 1)
            external = mapped_value(account, external_map, "HYY_EXTERNAL_USERID", len(accounts) == 1)
            prizes = client.run(token, external)
            if client.external_userid and cached_external.get(account.ref) != client.external_userid:
                cached_external[account.ref] = client.external_userid
                save_cache(cached_external)
            text = "、".join(prizes) if prizes else "本轮无新增奖品"
            summary.append(f"{account.label}：{text}")
        except ScriptError as exc:
            message = safe(exc)
            print(f"{account.label} 失败：{message}")
            summary.append(f"{account.label}：失败（{message}）")
        except Exception as exc:  # 防止一个账号中断全部账号
            message = safe(exc)
            print(f"{account.label} 未预期异常：{message}")
            summary.append(f"{account.label}：异常（{message}）")
    if summary:
        notify("汇翼云会员日抽奖", "\n".join(summary))


if __name__ == "__main__":
    main()
