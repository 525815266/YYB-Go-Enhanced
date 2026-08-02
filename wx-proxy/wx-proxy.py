#!/usr/bin/env python3
"""
YYB-Go-Enhanced wx/code 兼容代理
用 Python 3.11 stdlib 实现，不需要任何第三方包。

目的：让 smallfawn 青龙脚本零改动接入 YYB 作为微信协议后端。

脚本调用格式（smallcat 标准）：
  POST http://<host>:8787/wx/code
  headers: { auth: <token>, Content-Type: application/json }
  body: { appid: '<appid>', openid: '<openid>' }
  response: { status: true, data: { code: '...' } }

本代理转发到 YYB /wxapp/getCode，做格式转换：
  请求 {openid, appid} → YYB {ref: '<openid>', app_id: '<appid>'}
  响应 YYB {data.result.code} → {status:true, data:{code: '...'}}

启动：python3 wx-proxy.py
环境变量：
  PROXY_PORT   监听端口，默认 8787
  YYB_URL      YYB 服务地址，默认 http://127.0.0.1:9001
  AUTH_TOKEN   认证 token，默认 aa06c54e82f5439dc025e5f223b6466f
"""

import json
import os
import http.server
import urllib.request
import urllib.error


PORT = int(os.environ.get("PROXY_PORT", "8787"))
YYB_URL = os.environ.get("YYB_URL", "http://127.0.0.1:9001").rstrip("/")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "aa06c54e82f5439dc025e5f223b6466f")


def call_yyb(openid, appid):
    """转发到 YYB /wxapp/getCode，返回 code 字符串或 None"""
    body = json.dumps({"ref": openid, "app_id": appid}).encode("utf-8")
    req = urllib.request.Request(
        f"{YYB_URL}/wxapp/getCode",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"
    except Exception as e:
        return None, f"request error: {e}"

    code = (
        data.get("data")
        or {}
    )
    if isinstance(code, dict):
        code = (code.get("result") or {}).get("code", "")
    return (code, data.get("msg", "")) if code else (None, data.get("msg", ""))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 减少日志，只记录错误
        if args and "502" in str(args):
            super().log_message(fmt, *args)

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b""

        path = self.path.rstrip("/")

        # /wx/code — 核心兼容接口
        if path == "/wx/code":
            try:
                parsed = json.loads(body) if body else {}
            except Exception:
                parsed = {}

            openid = (parsed.get("openid") or "").strip()
            appid = (parsed.get("appid") or parsed.get("app_id") or "").strip()

            if not openid or not appid:
                self._send_json(400, {"status": False, "message": "openid and appid required"})
                return

            code, err = call_yyb(openid, appid)
            if code:
                self._send_json(200, {"status": True, "message": "获取成功", "data": {"code": code}})
            else:
                self._send_json(502, {"status": False, "message": f"获取 code 失败: {err}"})
            return

        # /wx/getuserinfo — 部分脚本用此端点
        if path == "/wx/getuserinfo":
            try:
                parsed = json.loads(body) if body else {}
            except Exception:
                parsed = {}
            openid = (parsed.get("openid") or "").strip()
            appid = (parsed.get("appid") or parsed.get("app_id") or "").strip()
            if not openid or not appid:
                self._send_json(400, {"status": False, "message": "openid and appid required"})
                return
            code, err = call_yyb(openid, appid)
            if code:
                self._send_json(200, {"status": True, "data": {"code": code, "encryptedData": "placeholder", "iv": "placeholder"}})
            else:
                self._send_json(502, {"status": False, "message": f"获取 code 失败: {err}"})
            return
        # 未识别 POST
        self._send_json(404, {"status": False, "message": "not found: " + path})

    def do_GET(self):
        path = self.path.rstrip("/")

        # /health
        if path == "/health" or path == "":
            self._send_json(200, {"status": True, "data": {"ok": True}})
            return

        # /credits/balance — YYB 无积分机制，始终返回充足
        if path == "/credits/balance":
            self._send_json(200, {"status": True, "data": {"balance": 99999}})
            return

        self._send_json(404, {"status": False, "message": "not found: " + path})


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[wx-proxy] listening on 0.0.0.0:{PORT}")
    print(f"[wx-proxy] forwarding to {YYB_URL}/wxapp/getCode")
    print(f"[wx-proxy] auth token: {AUTH_TOKEN}")
    server.serve_forever()
