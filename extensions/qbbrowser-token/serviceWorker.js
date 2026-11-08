// QQ Browser rotates ctk. The service worker only forwards the current token.
importScripts('bridge.js');
const ALARM = "yyb-qb-push";
const DEFAULT_INTERVAL_MIN = 30;

async function cfg() {
  return new Promise(resolve => chrome.storage.local.get(["serverUrl", "protocolToken", "device", "lastStatus", "lastPushAt"], resolve));
}
async function pushOnce() {
  const stored = await cfg();
  if (!stored.serverUrl) { await chrome.storage.local.set({ lastStatus: "未配置服务地址" }); return; }
  let credentials;
  try { ({ credentials } = await readQBBrowserCredentials(stored.device)); }
  catch (e) { await chrome.storage.local.set({ lastStatus: "读取登录态失败: " + e.message, lastPushAt: Date.now() }); return; }
  const headers = { "Content-Type": "application/json" };
  if (stored.protocolToken) headers["Authorization"] = "Bearer " + stored.protocolToken;
  try {
    const response = await fetch(stored.serverUrl.replace(/\/+$/, "") + "/api/qbbrowser/token", {
      method: "POST", headers,
      body: JSON.stringify(credentials)
    });
    const payload = await response.json().catch(() => null);
    if (response.ok && payload && payload.code === 0) {
      await chrome.storage.local.set({ lastStatus: "换票已保存（账号 " + payload.data.id + "，持续恢复尚未验证）", lastPushAt: Date.now() });
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
