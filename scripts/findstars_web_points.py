#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: findstars_web_points
"""寻星电玩网页积分任务。

网页授权完成后，网站会给浏览器一个 FS-Token。支持导入本人授权的 HAR：

python findstars_web_points.py --import-har 登录.har --label 网页账号

验证成功的登录态持久保存在青龙 config 目录；同备注的缓存优先于旧变量。
也可继续手工配置青龙变量：

FINDSTARS_TOKENS=Boom#<FS-Token>
另一个账号#<FS-Token>

也兼容单账号 FINDSTARS_TOKEN / FINDSTARS_FS_TOKEN。脚本只领取服务端
判定可领取的任务。公众号网页授权与 YYB 小程序 code 不通用；当前没有
验证可用的 YYB 自动换取、续期网页令牌的链路。
"""

import argparse
import base64
import hashlib
import json
import os
import random
import string
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

import requests


BASE_URL = os.getenv("FINDSTARS_API", "https://fsapi.ihezu.com").rstrip("/")
API_KEY = "L$PRBtj3@4r9^cNd"  # 网站前端公开的请求签名常量，不是用户令牌
ORIGIN = os.getenv("FINDSTARS_ORIGIN", "https://www.findstars.cn")
USER_AGENT = os.getenv(
    "FINDSTARS_UA",
    "Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36 "
    "Chrome/131.0 Mobile Safari/537.36",
)


def _nonce(length: int = 10) -> str:
    alphabet = string.digits + string.ascii_uppercase + string.ascii_lowercase
    return "".join(random.choice(alphabet) for _ in range(length))


def _headers(token: str) -> Dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = _nonce()
    sign_text = f"api_key={API_KEY}&FS-TS={timestamp}&FS-Nonce={nonce}"
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": USER_AGENT,
        "FS-TS": timestamp,
        "FS-Nonce": nonce,
        "FS-Sign": hashlib.md5(sign_text.encode()).hexdigest(),
        "FS-Token": token,
    }


def _load_tokens() -> List[Tuple[str, str]]:
    raw = os.getenv("FINDSTARS_TOKENS", "").strip()
    if not raw:
        raw = os.getenv("FINDSTARS_TOKEN", os.getenv("FINDSTARS_FS_TOKEN", "")).strip()
    # 青龙会把同名环境变量拼接成 ampersand 分隔的字符串；同时保留
    # 手工配置多行文本的兼容性。令牌本身不使用 ampersand。
    lines: List[str] = []
    for raw_line in raw.splitlines():
        lines.extend(part for part in raw_line.split("&") if part.strip())

    result: List[Tuple[str, str]] = []
    for index, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        label, token = f"账号 {index}", line
        if "#" in line:
            label, token = (part.strip() for part in line.split("#", 1))
        elif "|" in line:
            label, token = (part.strip() for part in line.split("|", 1))
        if token:
            result.append((label or f"账号 {index}", token))
    cached = _read_cache(_cache_path())
    result = [(label, cached.get(label, {}).get("token", token)) for label, token in result]
    configured_labels = {label for label, _ in result}
    result.extend((label, record["token"]) for label, record in cached.items() if label not in configured_labels)
    if not result:
        raise RuntimeError("未配置网页登录态，请用 --import-har 导入授权抓包，或配置 FINDSTARS_TOKENS")
    # 同一个 token 出现在变量与导入缓存时，只执行一次。
    unique: List[Tuple[str, str]] = []
    seen = set()
    for label, token in result:
        if token not in seen:
            unique.append((label, token))
            seen.add(token)
    return unique


def _cache_path() -> Path:
    configured = os.getenv("FINDSTARS_TOKEN_CACHE", "").strip()
    if configured:
        return Path(configured).expanduser()
    default = (Path("/ql/data/config") if Path("/ql/data/config").is_dir()
               else Path.home() / ".config" / "yyb-go") / "findstars_web_tokens.json"
    return default


def _read_cache(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    body = json.loads(path.read_text(encoding="utf-8"))
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("api") != BASE_URL or not isinstance(accounts, dict):
        raise RuntimeError("网页登录态缓存格式或 API 地址不匹配，请检查 FINDSTARS_TOKEN_CACHE")
    if any(not isinstance(item, dict) or not item.get("token") or not item.get("user_id")
           for item in accounts.values()):
        raise RuntimeError("网页登录态缓存缺少 token 或用户 ID，未覆盖原文件")
    return accounts


def _save_cache(path: Path, accounts: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"api": BASE_URL, "accounts": accounts}, stream, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def extract_tokens_from_har(path: Path) -> List[str]:
    entries = json.loads(path.read_text(encoding="utf-8-sig")).get("log", {}).get("entries", [])
    tokens: List[str] = []
    for entry in reversed(entries):
        request = entry.get("request", {})
        url = urlsplit(request.get("url", ""))
        if url.scheme != "https" or url.netloc != "fsapi.ihezu.com" or not url.path.startswith("/api/"):
            continue
        if url.path == "/api/login/_login":
            content = entry.get("response", {}).get("content", {})
            raw = content.get("text") or ""
            if content.get("encoding") == "base64":
                raw = base64.b64decode(raw).decode("utf-8")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            if isinstance(body, dict) and body.get("code") == 200 and body.get("status") == "success":
                token = (body.get("data") or {}).get("token")
                if isinstance(token, str) and token.strip():
                    tokens.append(token.strip())
        for header in request.get("headers", []):
            if str(header.get("name", "")).lower() == "fs-token" and header.get("value"):
                tokens.append(str(header["value"]).strip())
    return list(dict.fromkeys(token for token in tokens if token))


def import_har(path: Path, label: str = "") -> int:
    accounts = _read_cache(_cache_path())
    verified: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for token in extract_tokens_from_har(path):
        client = FindStarsClient(token)
        try:
            with client.session:
                user = client.user()
        except SessionExpired:
            continue
        if not user.get("id"):
            raise RuntimeError("网页登录态未返回用户 ID，未保存")
        verified.setdefault(str(user["id"]), (token, user))
    if not verified:
        raise RuntimeError("HAR 未包含仍有效的寻星登录态，请在微信重新授权后导出 HAR")
    if label and len(verified) != 1:
        raise RuntimeError("HAR 包含多个账号，不能用一个 --label 覆盖；请去掉该参数分别导入")
    for user_id, (token, user) in verified.items():
        existing = next((key for key, item in accounts.items() if str(item["user_id"]) == user_id), None)
        key = label.strip() or existing or str(user.get("name") or f"账号 {user_id}")
        if key in accounts and str(accounts[key]["user_id"]) != user_id:
            if label:
                raise RuntimeError("指定备注已绑定其他用户，未覆盖原登录态，请使用不同 --label")
            key = f"{key} (ID {user_id})"
            if key in accounts and str(accounts[key]["user_id"]) != user_id:
                raise RuntimeError("导入账号备注冲突，未覆盖原登录态，请指定不同 --label")
        if existing and existing != key:
            del accounts[existing]
        accounts[key] = {"user_id": user_id, "name": user.get("name"), "token": token,
                         "imported_at": int(time.time())}
    _save_cache(_cache_path(), accounts)
    print(f"已验证并保存 {len(verified)} 个网页登录态：{_cache_path()}；未提交积分领取")
    return len(verified)


class SessionExpired(RuntimeError):
    pass


class FindStarsClient:
    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()

    def post(self, path: str, payload: Any = None) -> Any:
        response = self.session.post(
            BASE_URL + path,
            json={} if payload is None else payload,
            headers=_headers(self.token),
            timeout=20,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"HTTP {response.status_code}: 非 JSON 响应") from exc
        if not isinstance(body, dict):
            raise RuntimeError("接口返回格式错误")
        if body.get("code") in (400010, 400011):
            raise SessionExpired("FS-Token 已失效，请重新网页登录授权后用 --import-har 更新（YYB code 不能替代）")
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {body.get('message') or '请求失败'}")
        if body.get("status") != "success" or body.get("code") != 200:
            raise RuntimeError(body.get("message") or f"接口失败: code={body.get('code')}")
        return body.get("data")

    def user(self) -> Dict[str, Any]:
        data = self.post("/api/user/show")
        if not isinstance(data, dict) or not data.get("id") or "coins" not in data:
            raise RuntimeError("账号信息缺少用户 ID 或积分，未执行领取")
        return data

    def tasks(self) -> List[Dict[str, Any]]:
        data = self.post("/api/activeTask/index")
        if not isinstance(data, list) or any(not isinstance(task, dict) for task in data):
            raise RuntimeError("任务列表格式错误，不能视为无待领取奖励")
        return data

    def receive(self, task_id: Any) -> None:
        self.post("/api/activeTask/receive", {"activeTaskId": task_id})


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _claimable(task: Dict[str, Any]) -> bool:
    # 与官网按钮保持一致：当前进度达标，且尚未达到活动总领取次数。
    if _number(task.get("totalTimes")) > 0 and _number(task.get("totalProgress")) >= _number(task["totalTimes"]):
        return False
    if "progress" in task and "times" in task:
        return _number(task["times"]) > 0 and _number(task["progress"]) == _number(task["times"])
    return str(task.get("status", "")).lower() == "complete"


def run_one(label: str, token: str) -> str:
    client = FindStarsClient(token)
    try:
        with client.session:
            return _run_client(label, client)
    except requests.RequestException as exc:
        raise RuntimeError(f"网络异常（{type(exc).__name__}），本轮未完成，请检查登录态和任务状态") from exc


def _run_client(label: str, client: FindStarsClient) -> str:
    before = client.user()
    start_coins = _number(before.get("coins"))
    tasks = client.tasks()
    claimable = [task for task in tasks if _claimable(task)]
    claimed: List[str] = []
    submitted: List[Dict[str, Any]] = []
    failures: List[str] = []
    for task in claimable:
        task_name = str(task.get("name") or task.get("id"))
        if os.getenv("FINDSTARS_DRY_RUN", "0") == "1":
            claimed.append(f"{task_name}(dry-run)")
            continue
        try:
            client.receive(task.get("id"))
            claimed.append(task_name)
            submitted.append(task)
        except requests.RequestException as exc:
            failures.append(f"{task_name}：网络异常（{type(exc).__name__}），领取结果待核对；未自动重试")
        except RuntimeError as exc:
            failures.append(f"{task_name}：{exc}")
    after = client.user()
    if submitted:
        remaining = {str(task.get("id")): task for task in client.tasks()}
        for task in submitted:
            current = remaining.get(str(task.get("id")))
            if current and _claimable(current) and _number(current.get("totalProgress")) <= _number(task.get("totalProgress")):
                failures.append(f"{task.get('name') or task.get('id')}：接口返回成功，但任务状态未变化，请核对；未自动重试")
    end_coins = _number(after.get("coins"))
    delta = end_coins - start_coins
    task_text = "、".join(claimed) if claimed else "本轮未领取"
    summary = f"{label}（{before.get('name') or '用户'}）：积分 {start_coins:g} → {end_coins:g}（变化 {delta:+g}）；领取：{task_text}"
    if not claimable:
        summary += "；当前无可领取奖励（未达标或已领取），不是登录失败"
    if failures:
        raise RuntimeError(summary + "；领取失败或待核对：" + "、".join(failures))
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="寻星电玩网页积分领取及授权 HAR 导入")
    parser.add_argument("--import-har", type=Path, help="验证并保存 HAR 登录态；仅导入，不领取积分")
    parser.add_argument("--label", default="", help="导入单账号的备注；与原变量备注一致时替换旧登录态")
    args = parser.parse_args(argv)
    try:
        if args.import_har:
            import_har(args.import_har, args.label)
            return 0
        if args.label:
            raise RuntimeError("--label 只能与 --import-har 一起使用")
        targets = _load_tokens()
    except Exception as exc:
        print(f"配置错误：{exc}")
        return 1
    results = []
    failed = 0
    for label, token in targets:
        try:
            results.append(run_one(label, token))
        except Exception as exc:
            results.append(f"{label}：执行失败：{exc}")
            failed += 1
    print("\n".join(results))
    print(f"结果：{len(targets) - failed}/{len(targets)} 个账号正常，{failed} 个失败或待核对")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
