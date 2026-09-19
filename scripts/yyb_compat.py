"""Shared YYB_SERVER adapter for standalone Python business scripts.

The adapter only supplies wx.login code (and, when explicitly requested, the
phone authorization payload) returned by YYB Go. It does not manufacture
encryptedData, iv, signature, or other unavailable WeChat runtime fields.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, MutableMapping

import requests


def _accounts() -> list[str]:
    result: list[str] = []
    for raw in os.getenv("YYB_SERVER", "").splitlines():
        value = raw.strip()
        if not value or "@" not in value or value == "[object Object]":
            continue
        endpoint, ref = (part.strip() for part in value.rsplit("@", 1))
        if endpoint and ref:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = "http://" + endpoint
            result.append(endpoint.rstrip("/") + "@" + ref)
    return result


def _parts(server: str) -> tuple[str, str]:
    value = str(server).strip()
    if "@" not in value:
        return value.rstrip("/"), ""
    endpoint, ref = value.rsplit("@", 1)
    return endpoint.rstrip("/"), ref


def _appid(namespace: MutableMapping[str, Any], args: tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
    value = kwargs.get("appid") or kwargs.get("app_id")
    if not value and args and isinstance(args[0], str):
        value = args[0]
    if not value:
        value = namespace.get("APPID") or namespace.get("APP_ID") or ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value)


def _request(server: str, path: str, appid: str, payload: Dict[str, Any] | None = None) -> Any:
    endpoint, ref = _parts(server)
    if not endpoint or not ref or not appid:
        raise RuntimeError("YYB 参数不完整：需要 地址@账号ID 和 app_id")
    headers: Dict[str, str] = {}
    api_key = os.getenv("YYB_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    response = requests.post(
        endpoint + path,
        json={"ref": ref, "app_id": appid, **(payload or {})},
        headers=headers,
        timeout=float(os.getenv("YYB_REQUEST_TIMEOUT", "30")),
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("YYB 返回非 JSON") from exc
    if response.status_code >= 400:
        if isinstance(body, dict):
            raise RuntimeError(str(body.get("message") or body.get("msg") or body))
        raise RuntimeError(str(body))
    return body


def _find_code(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("wx_code", "login_code"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate and candidate not in {"invalid", "null"}:
                return candidate
        # YYB wraps the real code below data/result. Check nested objects before
        # accepting a generic string `code`, which may only be an HTTP status.
        for child in value.values():
            found = _find_code(child)
            if found:
                return found
        candidate = value.get("code")
        if isinstance(candidate, str) and candidate and candidate not in {"invalid", "null"}:
            return candidate
    elif isinstance(value, list):
        for child in value:
            found = _find_code(child)
            if found:
                return found
    return None


def _phone_body(body: Any) -> Any:
    if not isinstance(body, dict):
        return body
    return body.get("result") or body.get("data") or body


def install(namespace: MutableMapping[str, Any]) -> None:
    """Patch a script namespace before its ``main()`` call."""

    accounts = _accounts()
    if accounts:
        namespace["SERVERS"] = accounts

    original_get_code = namespace.get("get_code")
    if not callable(original_get_code):
        return

    def yyb_code(server: str, *args: Any, **kwargs: Any) -> str:
        body = _request(server, "/wxapp/getCode", _appid(namespace, args, kwargs))
        code = _find_code(body)
        if not code:
            preview = json.dumps(body, ensure_ascii=False)[:300] if isinstance(body, (dict, list)) else str(body)[:300]
            raise RuntimeError("YYB 未返回 wx.login code：" + preview)
        return code

    def get_code(server: str, *args: Any, **kwargs: Any) -> Any:
        if "@" in str(server):
            return yyb_code(server, *args, **kwargs)
        return original_get_code(server, *args, **kwargs)

    namespace["get_code"] = get_code

    original_get_code_for = namespace.get("get_code_for")
    if callable(original_get_code_for):
        def get_code_for(server: str, appid: str) -> Any:
            if "@" in str(server):
                return yyb_code(server, appid)
            return original_get_code_for(server, appid)
        namespace["get_code_for"] = get_code_for

    def get_phone_payload(server: str, *args: Any, **kwargs: Any) -> Any:
        if "@" not in str(server):
            raise RuntimeError("当前账号不是 YYB_SERVER 格式，无法获取手机号授权包")
        return _phone_body(_request(server, "/wxapp/getPhoneNumber", _appid(namespace, args, kwargs)))

    for helper in ("get_phone_number_payload", "get_phone_authorize", "get_phone_data"):
        if helper in namespace:
            namespace[helper] = get_phone_payload
