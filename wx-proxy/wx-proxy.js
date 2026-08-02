/**
 * YYB-Go-Enhanced wx/code 兼容代理
 *
 * 目的：让 smallfawn 青龙脚本零改动接入 YYB 作为微信协议后端。
 * 脚本调用格式（smallcat 标准）：
 *   POST http://<host>:8787/wx/code
 *   headers: { auth: <token>, Content-Type: application/json }
 *   body: { appid: '<appid>', openid: '<openid>' }
 *   response: { status: true, data: { code: '...' } }
 *
 * 本代理转发到 YYB /wxapp/getCode，做格式转换：
 *   请求 {openid, appid} → YYB {ref: '<openid>', app_id: '<appid>'}
 *   响应 YYB {data.result.code} → {status:true, data:{code: '...'}}
 *
 * 环境：node >= 16（无需 npm install，纯 stdlib 写法用 fetch）
 *
 * 启动：node wx-proxy.js
 * 环境变量：
 *   PROXY_PORT       监听端口，默认 8787
 *   YYB_URL          YYB 服务地址，默认 http://127.0.0.1:9001
 *   AUTH_TOKEN       认证 token，默认 aa06c54e82f5439dc025e5f223b6466f
 */

const http = require("http");

const PORT = parseInt(process.env.PROXY_PORT, 10) || 8787;
const YYB_URL = (process.env.YYB_URL || "http://127.0.0.1:9001").replace(/\/$/, "");
const AUTH_TOKEN = process.env.AUTH_TOKEN || "aa06c54e82f5439dc025e5f223b6466f";

function parseUrl(url) {
  // 简易解析
  const m = url.match(/^https?:\/\/([^/]+)(\/.*)/);
  return m ? { host: m[1], path: m[2] } : null;
}

function request(opts) {
  return new Promise((resolve, reject) => {
    const parsed = parseUrl(opts.url);
    const method = opts.method || "POST";
    const body = opts.data ? JSON.stringify(opts.data) : null;

    const req = http.request(
      {
        hostname: parsed.host,
        port: 80,
        path: parsed.path,
        method,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": body ? Buffer.byteLength(body) : 0,
          ...(opts.headers || {}),
        },
        timeout: 15000,
      },
      (res) => {
        let chunks = "";
        res.on("data", (c) => (chunks += c));
        res.on("end", () => {
          let data = null;
          try {
            data = JSON.parse(chunks);
          } catch (e) {
            data = { _raw: chunks };
          }
          resolve({ status: res.statusCode, data, headers: res.headers || {} });
        });
      }
    );
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("timeout"));
    });
    if (body) req.write(body);
    req.end();
  });
}

const server = http.createServer(async (req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", async () => {
    const path = req.url || "";

    // /wx/code — 主要兼容接口
    if (path === "/wx/code" || path === "/wx/code/") {
      try {
        let parsed = {};
        try {
          parsed = JSON.parse(body) || {};
        } catch (e) {}

        const openid = parsed.openid || "";
        const appid = parsed.appid || parsed.app_id || "";
        if (!openid || !appid) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ status: false, message: "openid and appid required" }));
          return;
        }

        // 转发到 YYB
        const { status, data } = await request({
          method: "POST",
          url: `${YYB_URL}/wxapp/getCode`,
          data: { ref: openid, app_id: appid },
        });

        // YYB 响应格式转换
        const code =
          (data && data.data && data.data.result && data.data.result.code) || "";

        if (status === 200 && code) {
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ status: true, message: "获取成功", data: { code } }));
        } else {
          const errMsg =
            (data && data.msg) || `HTTP ${status}`;
          res.writeHead(502, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              status: false,
              message: `获取 code 失败: ${errMsg}`,
              data: data || {},
            })
          );
        }
      } catch (err) {
        res.writeHead(502, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ status: false, message: err.message, data: null }));
      }
      return;
    }

    // /api/client/login — 兼容 smallcat 登录接口（用于脚本初始化）
    if (path.startsWith("/api/client/login")) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          status: true,
          data: {
            auth: AUTH_TOKEN,
            nickname: "YYB-Proxy",
            version: "1.1.2",
          },
        })
      );
      return;
    }

    // /health — 健康检查
    if (path === "/health" || path === "/") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: true, data: { ok: true } }));
      return;
    }

    // /credits/balance — 兼容 smallcat 积分查询（YYB 无积分，始终返回充足）
    if (path === "/credits/balance") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: true, data: { balance: 99999 } }));
      return;
    }

    // 未识别路径
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: false, message: "not found: " + path }));
  });
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`[wx-proxy] listening on 0.0.0.0:${PORT}`);
  console.log(`[wx-proxy] forwarding to ${YYB_URL}/wxapp/getCode`);
});
