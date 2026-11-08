const $ = id => document.getElementById(id);
const version = chrome.runtime.getManifest().version;
$("version").textContent = `v${version}`;
$("officialLogin").onclick = () => {
  try {
    if (typeof chrome.account?.promptLogin !== 'function') throw new Error('请在 QQ 浏览器菜单中手动登录微信');
    renderStatus({lastStatus:'已请求打开官方登录；若未出现窗口，请在 QQ 浏览器菜单中手动登录微信'});
    chrome.account.promptLogin(() => {
      if (chrome.runtime.lastError) renderStatus({lastStatus:'官方登录窗口未打开，请在 QQ 浏览器菜单中手动登录微信',lastKind:'error'});
    });
  } catch { renderStatus({lastStatus:'此环境没有可用的官方登录接口，请在 QQ 浏览器菜单中手动登录微信',lastKind:'error'}); }
};
async function load() {
  const stored = await chrome.storage.local.get(["serverUrl", "protocolToken", "lastStatus", "lastPushAt", "lastKind"]);
  $("serverUrl").value = stored.serverUrl || "";
  $("protocolToken").value = stored.protocolToken || "";
  renderStatus(stored);
}
function renderStatus(stored) {
  const el = $("status");
  el.style.display = "block";
  el.dataset.kind = stored.lastKind || 'info';
  el.textContent = (stored.lastStatus || "尚未推送") + (stored.lastPushAt ? " · " + new Date(stored.lastPushAt).toLocaleString("zh-CN", { hour12: false }) : "");
}
$("pushNow").onclick = async () => {
  const button = $("pushNow");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = '正在推送…';
  renderStatus({lastStatus:'正在保存设置并联系扩展后台…'});
  let timer, finished = false;
  try {
    const serverUrl = $("serverUrl").value.trim();
    let target;
    try { target = new URL(serverUrl); } catch { throw new Error('请填写完整的 YYB 服务地址，例如 http://192.168.9.83:8000'); }
    if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password) throw new Error('服务地址必须为不含用户名和密码的 HTTP(S) 地址');
    const response = await Promise.race([
      (async () => {
        await chrome.storage.local.set({ serverUrl, protocolToken: $("protocolToken").value.trim() });
        if (finished) throw new Error('本次推送已结束，请重试');
        return chrome.runtime.sendMessage({ type: "push-now", readDevice: true });
      })(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('扩展后台未在 40 秒内完成。请检查浏览器扩展错误及服务连接，无需重扫')), 40000); })
    ]);
    if (!response?.done) throw new Error('扩展后台没有返回结果，请在扩展管理页重新加载后重试');
    renderStatus(response);
  } catch (error) {
    const status = {lastStatus: error.message, lastPushAt: Date.now(), lastKind:'error'};
    renderStatus(status);
    void chrome.storage.local.set(status).catch(() => {});
  } finally {
    finished = true;
    clearTimeout(timer);
    button.disabled = false;
    button.textContent = '立即推送';
  }
};
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.lastStatus) chrome.storage.local.get(['lastStatus','lastPushAt','lastKind']).then(renderStatus).catch(() => {});
});
load().catch(() => renderStatus({lastStatus:'设置读取失败，请在扩展管理页重新加载此扩展',lastKind:'error'}));
