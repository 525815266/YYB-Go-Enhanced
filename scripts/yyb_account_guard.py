#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YYB 多账号公共状态缓存。

业务脚本只需要在遍历 YYB_SERVER 前调用 ``filter_accounts``，在捕获业务
错误时调用 ``mark_from_error``。缓存是共享的，默认放在青龙持久化目录。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar


T = TypeVar("T")

STATUS_FILE_ENV = "YYB_ACCOUNT_STATUS_FILE"
DEFAULT_STATUS_FILE = "/ql/data/config/yyb_account_status.json"
STATUS_VERSION = 1

# 明确表示业务账号未完成初始化的文本。只在命中这些词时跳过。
UNBOUND_PATTERNS = (
    r"未授权手机号",
    r"手机号未授权",
    r"未完成手机号授权",
    r"请先.{0,16}授权手机号",
    r"尚未注册.{0,16}(?:会员|小程序)",
    r"未注册(?:会员|小程序)",
    r"未绑定(?:此小程序|手机号|会员|账号)",
    r"尚未绑定",
    r"未登录.{0,12}(?:会员|账号)",
    r"微信授权成功.{0,20}未登录",
)

# 这些词即使和“登录失败”同时出现，也不能标记为未注册。
TEMPORARY_PATTERNS = (
    r"timeout",
    r"timed out",
    r"超时",
    r"HTTP\s*[45]\d\d",
    r"\b(?:502|503|504)\b",
    r"服务端异常",
    r"系统繁忙",
    r"活动太火爆",
    r"风控",
    r"third login fds limit",
    r"登录过期",
    r"token.{0,8}(?:失效|过期)",
)

_UNBOUND_RE = re.compile("|".join(UNBOUND_PATTERNS), re.IGNORECASE)
_TEMPORARY_RE = re.compile("|".join(TEMPORARY_PATTERNS), re.IGNORECASE)

TTL_SECONDS = {
    "unbound": 24 * 60 * 60,
    "unregistered": 24 * 60 * 60,
    "code_unavailable": 30 * 60,
    "temporary_error": 10 * 60,
    "ready": 6 * 60 * 60,
    "unknown": 15 * 60,
}


def status_file() -> Path:
    configured = os.getenv(STATUS_FILE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    default = Path(DEFAULT_STATUS_FILE)
    if default.parent.exists() or os.getenv("QL_DATA_DIR"):
        return default
    # 本地开发/测试时不污染仓库，跟随脚本目录保存。
    return Path(__file__).resolve().parent / "yyb_account_status.json"


def account_ref(value: Any) -> str:
    """从 YYB_SERVER 行、对象或裸 ref 中取得稳定账号标识。"""
    if isinstance(value, dict):
        value = value.get("ref") or value.get("id") or value.get("openid") or ""
    text = str(value or "").strip()
    if "@" in text:
        text = text.rsplit("@", 1)[1].strip()
    return text


def _read() -> dict[str, Any]:
    path = status_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("accounts"), dict):
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return {"version": STATUS_VERSION, "updated_at": 0, "accounts": {}}


def _write(payload: dict[str, Any]) -> None:
    path = status_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["version"] = STATUS_VERSION
    payload["updated_at"] = int(time.time())
    fd, temp_name = tempfile.mkstemp(prefix=".yyb-status-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def classify_error(message: Any) -> tuple[str, str] | None:
    """返回可缓存的状态；临时故障返回 ``temporary_error``，其余返回 None。"""
    text = str(message or "").replace("\n", " ").strip()
    if not text:
        return None
    if _TEMPORARY_RE.search(text):
        return "temporary_error", text[:240]
    if _UNBOUND_RE.search(text):
        lowered = text.lower()
        status = "unregistered" if ("注册" in text or "会员" in text) else "unbound"
        return status, text[:240]
    return None


def _storage_key(ref: Any, app_id: str = "") -> str:
    key = account_ref(ref)
    return f"{key}::{app_id}" if key and app_id else key


def get_status(ref: Any, *, app_id: str = "") -> dict[str, Any] | None:
    key = account_ref(ref)
    if not key:
        return None
    accounts = _read().get("accounts", {})
    if app_id:
        return accounts.get(_storage_key(key, app_id))
    return accounts.get(key)


def should_skip(ref: Any, *, app_id: str = "", now: float | None = None) -> tuple[bool, str]:
    key = account_ref(ref)
    if not key or os.getenv("YYB_GUARD_BYPASS", "").strip() == "1":
        return False, ""
    item = get_status(key, app_id=app_id)
    if not isinstance(item, dict):
        return False, ""
    status = str(item.get("status", "unknown"))
    if status == "disabled":
        return True, str(item.get("reason") or "账号已被公共缓存停用")
    checked = float(item.get("checked_at") or 0)
    ttl = int(TTL_SECONDS.get(status, TTL_SECONDS["unknown"]))
    current = time.time() if now is None else now
    if checked and current - checked < ttl and status in {"unbound", "unregistered", "code_unavailable", "temporary_error"}:
        return True, str(item.get("reason") or status)
    return False, ""


def set_status(ref: Any, status: str, reason: str = "", *, app_id: str = "") -> None:
    ref_key = account_ref(ref)
    key = _storage_key(ref_key, app_id)
    if not key:
        return
    payload = _read()
    accounts = payload.setdefault("accounts", {})
    accounts[key] = {
        "ref": ref_key,
        "status": status,
        "reason": str(reason or "")[:240],
        "app_id": str(app_id or ""),
        "checked_at": int(time.time()),
    }
    _write(payload)


def mark_from_error(ref: Any, message: Any, *, app_id: str = "") -> str | None:
    classified = classify_error(message)
    if not classified:
        return None
    status, reason = classified
    set_status(ref, status, reason, app_id=app_id)
    return status


def mark_ready(ref: Any, *, app_id: str = "") -> None:
    set_status(ref, "ready", "", app_id=app_id)


def filter_accounts(
    values: Iterable[T],
    ref_getter: Callable[[T], Any] | None = None,
    *,
    app_id: str = "",
    log: Callable[[str], None] | None = print,
) -> list[T]:
    """过滤冷却中的账号，并保持原顺序。"""
    getter = ref_getter or (lambda value: value)
    kept: list[T] = []
    for value in values:
        ref = account_ref(getter(value))
        skip, reason = should_skip(ref, app_id=app_id)
        if skip:
            if log:
                log(f"账号 {ref} 已由 YYB 公共缓存跳过：{reason}")
            continue
        kept.append(value)
    return kept


def prune(*, refs: Iterable[Any] | None = None) -> dict[str, Any]:
    """清理不再存在于 YYB_SERVER 的账号状态，返回汇总。"""
    payload = _read()
    accounts = payload.setdefault("accounts", {})
    allowed = {account_ref(ref) for ref in refs or () if account_ref(ref)}
    removed: list[str] = []
    if allowed:
        for key in list(accounts):
            base_key = key.split("::", 1)[0]
            if base_key not in allowed:
                removed.append(key)
                accounts.pop(key, None)
    _write(payload)
    return {"total": len(accounts), "removed": removed, "file": str(status_file())}


__all__ = [
    "account_ref",
    "classify_error",
    "filter_accounts",
    "get_status",
    "mark_from_error",
    "mark_ready",
    "prune",
    "set_status",
    "should_skip",
    "status_file",
]
