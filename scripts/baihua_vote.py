#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: baihua_vote
"""百花约跑团线上投票（YYB 账号关联版）。

活动使用微信公众号 ``snsapi_base`` 网页 OAuth。YYB Go 的 iLink 凭据只能调用
小程序接口，不能直接生成公众号 OAuth code，因此首次授权后需要保存活动下发的
``app_access_token``。脚本随后可在 token 有效期内自动检查验证码并投票一次。

青龙环境变量：

``YYB_SERVER``
    每行 ``yyb-go:8000@账号ID或OpenID``，用于确定账号顺序和日志标签。
``BAIHUA_VOTE_TOKENS``
    每行 ``账号ID#活动JWT``。若只填写 JWT，则按 YYB_SERVER 顺序对应。
``BAIHUA_VOTE_STAGE`` / ``BAIHUA_VOTE_PLAYER``
    默认分别为 ``5CYjC3kyfN``、``568451``。
``BAIHUA_VOTE_DRY_RUN``
    设为 ``1`` 时仅验证登录态和验证码，不提交投票。

可用 ``--import-har 抓包.har --ref 账号ID`` 从本人授权抓包中提取 token 并写入
缓存。缓存默认位于 ``/ql/data/config/baihua_vote_tokens.json``。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

import requests


APPID = "wxabec74192b99a2d6"
DEFAULT_STAGE = "5CYjC3kyfN"
DEFAULT_PLAYER = "568451"
DEFAULT_CACHE = "/ql/data/config/baihua_vote_tokens.json"
DEFAULT_ENTRY = (
    "http://s-5xez31l8-lobxrjlcjm72bqti.js.djd.m.ssgcjy.com/"
    "app/5CYjC3kyfN/player/568451"
)
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.50 NetType/WIFI Language/zh_CN"
)


@dataclass(frozen=True)
class VoteAccount:
    ref: str
    token: str


def truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def yyb_refs(raw: str | None = None) -> list[str]:
    return [ref for _, ref in yyb_accounts(raw)]


def yyb_accounts(raw: str | None = None) -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in (raw if raw is not None else os.getenv("YYB_SERVER", "")).splitlines():
        value = line.strip()
        if not value or "@" not in value:
            continue
        endpoint, ref = (part.strip() for part in value.rsplit("@", 1))
        if endpoint and not endpoint.startswith(("http://", "https://")):
            endpoint = "http://" + endpoint
        if endpoint and ref and ref not in seen:
            accounts.append((endpoint.rstrip("/"), ref))
            seen.add(ref)
    return accounts


def normalize_token(value: str) -> str:
    token = value.strip().strip('"').strip("'")
    if "app_access_token=" in token:
        token = token.split("app_access_token=", 1)[1].split(";", 1)[0]
    for _ in range(3):
        decoded = unquote(token)
        if decoded == token:
            break
        token = decoded
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token.count(".") != 2:
        raise ValueError("不是有效的活动 JWT")
    return token


def jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".", 2)[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("活动 JWT payload 无法解析") from exc
    if not isinstance(claims, dict):
        raise ValueError("活动 JWT payload 格式错误")
    return claims


def token_base_url(token: str) -> str:
    claims = jwt_claims(token)
    if claims.get("appid") != APPID or claims.get("guard") != "app":
        raise ValueError("JWT 不属于当前微信公众号活动")
    issuer = str(claims.get("iss") or "").strip()
    if not issuer.startswith(("http://", "https://")) or "/app/auth/" not in issuer:
        raise ValueError("JWT 缺少可信活动签发地址")
    return issuer.split("/app/auth/", 1)[0].rstrip("/")


def parse_token_config(raw: str, refs: Iterable[str]) -> dict[str, str]:
    refs = list(refs)
    result: dict[str, str] = {}
    value = raw.strip()
    if not value:
        return result
    if value.startswith(("{", "[")):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            for ref, token in parsed.items():
                result[str(ref).strip()] = normalize_token(str(token))
            return result
        if isinstance(parsed, list):
            value = "\n".join(str(item) for item in parsed)

    unbound: list[str] = []
    for line in value.replace("&", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        if "#" in line:
            ref, token = line.split("#", 1)
            result[ref.strip()] = normalize_token(token)
        else:
            unbound.append(normalize_token(line))
    free_refs = [ref for ref in refs if ref not in result]
    if len(unbound) > len(free_refs):
        raise ValueError("未标注账号的 token 数量超过 YYB_SERVER 账号数量")
    result.update(zip(free_refs, unbound))
    return result


def load_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): normalize_token(str(value)) for key, value in data.items()}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"缓存读取失败，已忽略：{exc}")
        return {}


def save_cache(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def extract_tokens_from_har(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    found: list[str] = []
    for entry in data.get("log", {}).get("entries", []):
        for header in entry.get("response", {}).get("headers", []):
            if str(header.get("name", "")).lower() != "set-cookie":
                continue
            raw = str(header.get("value", ""))
            if "app_access_token=" not in raw:
                continue
            token = normalize_token(raw)
            if token not in found:
                found.append(token)
    return found


def discover_oauth_url(
    entry_url: str, session: requests.Session | None = None, timeout: float = 15
) -> str:
    """Resolve the activity's rotating host and return its current OAuth URL."""

    client = session or requests.Session()
    current = entry_url
    for _ in range(4):
        response = client.get(
            current,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=False,
            timeout=timeout,
        )
        location = response.headers.get("Location", "").strip()
        if location.startswith("https://open.weixin.qq.com/connect/oauth2/authorize"):
            query = parse_qs(urlparse(location).query)
            if query.get("appid", [""])[0] != APPID:
                raise RuntimeError("活动返回了不匹配的公众号 AppID")
            if not query.get("redirect_uri", [""])[0]:
                raise RuntimeError("公众号 OAuth URL 缺少 redirect_uri")
            return location
        if response.status_code not in {301, 302, 303, 307, 308} or not location:
            break
        current = location
    raise RuntimeError("未能从活动入口发现公众号 OAuth URL")


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _protocol_error(body: Any) -> str:
    for value in _walk(body):
        if isinstance(value, str) and (
            "invalid api_name" in value.lower()
            or "not support" in value.lower()
            or "unsupported" in value.lower()
        ):
            return value
    return ""


def _oauth_code(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("oauth_code", "wx_code"):
        value = body.get(key)
        if isinstance(value, str) and len(value) >= 16:
            return value
    for key, value in body.items():
        if isinstance(value, (dict, list)):
            found = _oauth_code(value) if isinstance(value, dict) else ""
            if found:
                return found
        if key == "code" and isinstance(value, str) and len(value) >= 16:
            return value
    return ""


def exchange_oauth_code(
    oauth_url: str, code: str, session: requests.Session | None = None, timeout: float = 20
) -> str:
    """Send a returned OAuth code through the activity callback and extract JWT."""

    query = parse_qs(urlparse(oauth_url).query)
    redirect_uri = query.get("redirect_uri", [""])[0]
    state = query.get("state", [""])[0]
    if not redirect_uri or not state:
        raise RuntimeError("OAuth URL 缺少回调参数")
    separator = "&" if "?" in redirect_uri else "?"
    callback = f"{redirect_uri}{separator}code={code}&state={state}"
    client = session or requests.Session()
    response = client.get(
        callback,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=True,
        timeout=timeout,
    )
    for cookie in client.cookies:
        if cookie.name == "app_access_token":
            return normalize_token(cookie.value)
    raw = response.headers.get("Set-Cookie", "")
    if "app_access_token=" in raw:
        return normalize_token(raw)
    raise RuntimeError("OAuth 回调完成但未取得活动 app_access_token")


def probe_yyb_oauth(
    endpoint: str,
    ref: str,
    oauth_url: str,
    timeout: float = 30,
    session: requests.Session | None = None,
) -> str:
    """Try the YYB compatibility operation and return an exchanged activity JWT.

    Current YYB iLink sessions normally return ``invalid api_name`` here. The
    explicit probe keeps that limitation visible while allowing a future
    protocol implementation to become usable without changing the task flow.
    """

    client = session or requests.Session()
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("YYB_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    response = client.post(
        endpoint + "/wx/mpgeta8key",
        headers=headers,
        json={
            "ref": ref,
            "app_id": APPID,
            "payload": {
                "api_name": "mpGetA8Key",
                "data": {
                    "url": oauth_url,
                    "scene": 7,
                    "opcode": 2,
                    "code_type": 0,
                    "code_version": 0,
                },
                "env": 1,
            },
        },
        timeout=timeout,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"YYB 授权探测返回非 JSON（HTTP {response.status_code}）") from exc
    if response.status_code >= 400:
        raise RuntimeError(str(body.get("message") or body.get("msg") or body))
    error = _protocol_error(body)
    if error:
        raise RuntimeError(
            "YYB 当前 iLink 不支持公众号 mp-geta8key：" + error
        )
    code = _oauth_code(body)
    if not code:
        raise RuntimeError("YYB 调用成功但未返回公众号 OAuth code")
    return exchange_oauth_code(oauth_url, code, session=client, timeout=timeout)


def probe_missing_accounts(cache_path: Path, refs: list[str] | None = None) -> int:
    accounts = yyb_accounts()
    selected = set(refs or [])
    if selected:
        accounts = [item for item in accounts if item[1] in selected]
    if not accounts:
        print("YYB_SERVER 中没有可探测账号")
        return 1
    oauth_url = discover_oauth_url(os.getenv("BAIHUA_VOTE_ENTRY", DEFAULT_ENTRY))
    cache = load_cache(cache_path)
    success = 0
    for endpoint, ref in accounts:
        if ref in cache and not VoteClient(cache[ref]).expired():
            print(f"YYB账号 {ref} 已有有效活动 token，跳过协议探测")
            continue
        try:
            cache[ref] = probe_yyb_oauth(endpoint, ref, oauth_url)
            save_cache(cache_path, cache)
            success += 1
            print(f"YYB账号 {ref} 已通过协议取得并缓存活动 token")
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"YYB账号 {ref} 协议取登录态失败：{exc}")
    return 0 if success else 1


class VoteClient:
    def __init__(self, token: str, timeout: float = 20, session: requests.Session | None = None):
        self.token = normalize_token(token)
        self.claims = jwt_claims(self.token)
        self.base_url = token_base_url(self.token)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json, text/plain, */*",
                "User-Agent": USER_AGENT,
                "Referer": self.base_url + "/",
            }
        )

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(
            method, self.base_url + path, timeout=self.timeout, **kwargs
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"活动接口返回非 JSON（HTTP {response.status_code}）") from exc
        if response.status_code >= 400:
            raise RuntimeError(
                f"活动接口 HTTP {response.status_code}: {body.get('msg') if isinstance(body, dict) else body}"
            )
        if not isinstance(body, dict):
            raise RuntimeError("活动接口返回结构异常")
        return body

    def expired(self, leeway: int = 60) -> bool:
        try:
            return int(self.claims.get("exp", 0)) <= int(time.time()) + leeway
        except (TypeError, ValueError):
            return True

    def profile(self, stage: str) -> dict[str, Any]:
        return self.request("GET", "/api/v1/app/fans/profile", params={"stage_code": stage})

    def captcha_required(self, stage: str, player: str) -> bool:
        body = self.request(
            "GET",
            "/api/v1/app/votes/show_captcha",
            params={"stage_code": stage, "player_id": player},
        )
        return bool((body.get("data") or {}).get("show_captcha"))

    def vote(self, stage: str, player: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/app/votes",
            params={"stage_code": stage},
            json={"player_ids": [str(player)]},
        )


def run_account(account: VoteAccount, stage: str, player: str, dry_run: bool) -> bool:
    client = VoteClient(account.token, timeout=float(os.getenv("BAIHUA_VOTE_TIMEOUT", "20")))
    fan_id = client.claims.get("sub", "-")
    print(f"\n===== YYB账号 {account.ref} / 活动用户 {fan_id} =====")
    if client.expired():
        print("活动登录态已过期，请重新完成一次公众号授权并导入 token")
        return False
    profile = client.profile(stage)
    if profile.get("code") != 0:
        print(f"登录态校验失败：{profile.get('msg') or profile}")
        return False
    if client.captcha_required(stage, player):
        print("活动要求人工验证码，本账号已跳过，不会绕过验证码")
        return False
    if dry_run:
        print("验证成功，当前无需验证码；dry-run 未提交投票")
        return True
    result = client.vote(stage, player)
    message = str(result.get("msg") or "")
    if result.get("code") == 0:
        print(f"投票成功：{message or 'ok'}")
        return True
    if any(word in message for word in ("已达上限", "已经投", "已投票", "机会已用完")):
        print(f"今日已完成或达到额度：{message}")
        return True
    print(f"投票失败：{message or result}")
    return False


def build_accounts(cache_path: Path) -> tuple[list[VoteAccount], list[str]]:
    refs = yyb_refs()
    cached = load_cache(cache_path)
    configured = parse_token_config(os.getenv("BAIHUA_VOTE_TOKENS", ""), refs)
    tokens = {**cached, **configured}
    if configured and configured != cached:
        save_cache(cache_path, tokens)
    ordered_refs = refs + [ref for ref in tokens if ref not in refs]
    accounts = [VoteAccount(ref, tokens[ref]) for ref in ordered_refs if ref in tokens]
    missing = [ref for ref in refs if ref not in tokens]
    return accounts, missing


def import_har(path: Path, ref: str, cache_path: Path) -> int:
    tokens = extract_tokens_from_har(path)
    if not tokens:
        print("HAR 中未找到 app_access_token", file=sys.stderr)
        return 2
    if len(tokens) > 1:
        print(f"HAR 中找到 {len(tokens)} 个 token，将保存最后一个")
    claims = jwt_claims(tokens[-1])
    cache = load_cache(cache_path)
    cache[ref] = tokens[-1]
    save_cache(cache_path, cache)
    exp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(claims.get("exp", 0))))
    print(f"已保存账号 {ref} 的活动登录态，活动用户={claims.get('sub', '-')}，有效期至 {exp}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="百花约跑团公众号网页投票")
    parser.add_argument("--import-har", type=Path, help="从本人授权 HAR 导入活动 token")
    parser.add_argument("--ref", help="导入 token 对应的 YYB 账号 ID/OpenID")
    parser.add_argument(
        "--probe-yyb-oauth",
        action="store_true",
        help="按 YYB_SERVER 账号探测公众号 OAuth 协议能力，不提交投票",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(os.getenv("BAIHUA_VOTE_TOKEN_FILE", DEFAULT_CACHE)),
    )
    args = parser.parse_args(argv)
    if args.import_har:
        if not args.ref:
            parser.error("--import-har 必须同时提供 --ref")
        return import_har(args.import_har, args.ref, args.cache)
    if args.probe_yyb_oauth:
        refs = [part.strip() for part in (args.ref or "").split(",") if part.strip()]
        return probe_missing_accounts(args.cache, refs)

    stage = os.getenv("BAIHUA_VOTE_STAGE", DEFAULT_STAGE).strip() or DEFAULT_STAGE
    player = os.getenv("BAIHUA_VOTE_PLAYER", DEFAULT_PLAYER).strip() or DEFAULT_PLAYER
    accounts, missing = build_accounts(args.cache)
    print(f"目标活动={stage}，选手={player}，可运行账号={len(accounts)}")
    for ref in missing:
        print(f"YYB账号 {ref} 尚无公众号活动 token，已跳过")
    if not accounts:
        print("没有可用活动登录态。先完成公众号授权并用 --import-har 导入。")
        return 1
    success = 0
    for account in accounts:
        try:
            success += int(run_account(account, stage, player, truthy("BAIHUA_VOTE_DRY_RUN")))
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"\nYYB账号 {account.ref} 执行异常：{exc}")
    print(f"\n执行完成：成功/已完成 {success}，总计 {len(accounts)}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
