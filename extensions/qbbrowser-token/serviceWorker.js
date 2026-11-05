// YYB Token Bridge: 把 QQ 浏览器外层微信登录态推送给 YYB Go 服务。
// 桥候选按序尝试（QQ 浏览器为扩展注入 browser/chrome.account 命名空间）。
const ALARM = "yyb-qb-push";
const DEFAULT_INTERVAL_MIN = 30;

async function cfg() {
  return new Promise(resolve => chrome.storage.local.get(["serverUrl", "protocolToken", "guid", "lastStatus", "lastPushAt"], resolve));
}
async function stableGuid() {
  const stored = await cfg();
  if (stored.guid) return stored.guid;
  const g = crypto.randomUUID().replaceAll("-", "");
  await chrome.storage.local.set({ guid: g });
  return g;
}
async function tryBridge() {
  const b = self.browser || chrome;
  const attempts = [];
  const withTimeout = (p, ms) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("bridge timeout")), ms))]);
  // 候选 1: browser.login.getAccountInfo（promise 或回调双形态）
  try {
    const login = b.login || (b.browser && b.browser.login);
    if (login && typeof login.getAccountInfo === "function") {
      const result = await withTimeout(new Promise((resolve, reject) => {
        const maybe = login.getAccountInfo(info => resolve(info));
        if (maybe && typeof maybe.then === "function") maybe.then(resolve, reject);
      }), 4000);
      if (result) attempts.push({ bridge: "browser.login.getAccountInfo", info: result });
    }
  } catch (e) { attempts.push({ bridge: "browser.login.getAccountInfo", error: String(e) }); }
  // 候选 2: chrome.account.getLoginInfo（回调）
  try {
    const acct = b.account || (b.chrome && b.chrome.account);
    if (acct && typeof acct.getLoginInfo === "function") {
      const result = await withTimeout(new Promise(resolve => acct.getLoginInfo(info => resolve(info))), 4000);
      if (result) attempts.push({ bridge: "chrome.account.getLoginInfo", info: result });
    }
  } catch (e) { attempts.push({ bridge: "chrome.account.getLoginInfo", error: String(e) }); }
  // 归一化：在任一候选里找 token 与 uid
  for (const a of attempts) {
    const info = a.info || {};
    const token = info.accessToken || info.access_token || info.qb_access_token || info.token || "";
    const uid = String(info.uid || info.user_id || info.userId || info.openid || "");
    if (token && uid) return { token, uid, bridge: a.bridge };
  }
  const err = new Error("没有可用登录桥: " + attempts.map(a => a.bridge + (a.error ? "(" + a.error + ")" : "")).join("; "));
  throw err;
}
async function pushOnce() {
  const stored = await cfg();
  if (!stored.serverUrl) { await chrome.storage.local.set({ lastStatus: "未配置服务地址" }); return; }
  let token, uid, bridge;
  try { ({ token, uid, bridge } = await tryBridge()); }
  catch (e) { await chrome.storage.local.set({ lastStatus: "读取登录态失败: " + e.message, lastPushAt: Date.now() }); return; }
  const guid = await stableGuid();
  const headers = { "Content-Type": "application/json" };
  if (stored.protocolToken) headers["Authorization"] = "Bearer " + stored.protocolToken;
  try {
    const response = await fetch(stored.serverUrl.replace(/\/+$/, "") + "/api/qbbrowser/token", {
      method: "POST", headers,
      body: JSON.stringify({ access_token: token, user_id: uid, guid, appid: "wx0dbc1ae97f156c38" })
    });
    const payload = await response.json().catch(() => null);
    if (response.ok && payload && payload.code === 0) {
      await chrome.storage.local.set({ lastStatus: "推送成功（" + bridge + "，账号 " + payload.data.id + "）", lastPushAt: Date.now() });
    } else {
      await chrome.storage.local.set({ lastStatus: "推送被拒: " + (payload && payload.msg || response.status), lastPushAt: Date.now() });
    }
  } catch (e) {
    await chrome.storage.local.set({ lastStatus: "推送失败: " + e.message, lastPushAt: Date.now() });
  }
}
chrome.runtime.onInstalled.addListener(async () => {
  chrome.alarms.create(ALARM, { periodInMinutes: DEFAULT_INTERVAL_MIN });
  await pushOnce();
});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) pushOnce(); });
chrome.runtime.onStartup.addListener(pushOnce);
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "push-now") { pushOnce().then(() => sendResponse({ done: true })); return true; }
  if (msg && msg.type === "last-status") { cfg().then(s => sendResponse(s)); return true; }
});
