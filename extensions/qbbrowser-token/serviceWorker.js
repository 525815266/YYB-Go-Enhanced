// QQ Browser rotates ctk. Forward it in memory; never store it in the extension.
importScripts('bridge.js', 'device.js');
const ALARM = 'yyb-qb-push';
const DEFAULT_INTERVAL_MIN = 30;
let inFlight;

function cfg() {
  return chrome.storage.local.get(['serverUrl', 'protocolToken', 'device', 'lastStatus', 'lastPushAt', 'lastKind']);
}
async function status(lastStatus, lastKind = 'info') {
  const result = {done: true, lastStatus, lastKind, lastPushAt: Date.now()};
  await chrome.storage.local.set({lastStatus, lastKind, lastPushAt: result.lastPushAt});
  return result;
}
async function push(readDevice) {
  const stored = await cfg();
  if (!stored.serverUrl) throw new Error('请先填写 YYB 服务地址');
  let target;
  try { target = new URL(stored.serverUrl); } catch { throw new Error('YYB 服务地址格式不正确'); }
  if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password) {
    throw new Error('服务地址必须为不含用户名和密码的 HTTP(S) 地址');
  }
  let device = stored.device;
  if (readDevice || !device?.machine) {
    await status('正在通过官方小游戏页面读取设备参数…');
    device = await readQBBrowserDevice();
  }
  await status('设备已读取，正在读取浏览器微信登录态…');
  const current = await readQBBrowserCredentials(device);
  await chrome.storage.local.set({device: current.device});
  await status('正在连接 YYB 服务并换取登录票据…');
  const headers = {'Content-Type': 'application/json'};
  if (stored.protocolToken) headers.Authorization = 'Bearer ' + stored.protocolToken;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  let response, payload;
  try {
    response = await fetch(stored.serverUrl.replace(/\/+$/, '') + '/api/qbbrowser/token', {
      method: 'POST', headers, body: JSON.stringify(current.credentials), signal: controller.signal,
      // Do not follow redirects with account credentials or protocol tokens.
      redirect: 'error'
    });
    payload = await response.json().catch(() => null);
    if (controller.signal.aborted) throw new Error('timeout');
  } catch {
    throw new Error(controller.signal.aborted
      ? '连接 YYB 服务或等待换票超时（15 秒）；请检查服务状态，无需重扫'
      : '连接 YYB 服务失败；请检查服务地址、网络及是否发生重定向');
  } finally { clearTimeout(timer); }
  if (response.ok && payload?.code === 0) {
    return status('换票已保存；实际取码及持续恢复尚需验证', 'success');
  }
  // Upstream bodies may contain private data. Keep only the HTTP status here.
  if (response.status === 401 || response.status === 403) {
    throw new Error(`推送被拒（HTTP ${response.status}）；请核对协议令牌及浏览器微信登录状态`);
  }
  throw new Error(`换票未成功（HTTP ${response.status}${payload ? '' : '，响应不是有效 JSON'}）；请检查 YYB 服务端记录`);
}
function pushOnce(readDevice = false) {
  if (!inFlight) {
    inFlight = push(readDevice).catch(async error => {
      const result = {done: true, lastStatus: error.message, lastKind: 'error', lastPushAt: Date.now()};
      await chrome.storage.local.set({lastStatus: result.lastStatus, lastKind: result.lastKind, lastPushAt: result.lastPushAt}).catch(() => {});
      return result;
    }).finally(() => { inFlight = undefined; });
  }
  return inFlight;
}
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM, {periodInMinutes: DEFAULT_INTERVAL_MIN});
  void pushOnce();
});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) void pushOnce(); });
chrome.runtime.onStartup.addListener(() => { void pushOnce(); });
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === 'push-now') {
    pushOnce(msg.readDevice === true).then(sendResponse);
    return true;
  }
  if (msg?.type === 'last-status') {
    cfg().then(sendResponse, () => sendResponse({lastStatus: '扩展设置读取失败', lastKind: 'error'}));
    return true;
  }
});
