const $ = id => document.getElementById(id);
$("officialLogin").onclick = () => {
  if (typeof chrome.account?.promptLogin === 'function') chrome.account.promptLogin(() => {});
  else renderStatus({lastStatus:'此环境没有 QQ 浏览器官方登录接口，请在浏览器菜单中手动登录微信'});
};
async function load() {
  const stored = await chrome.storage.local.get(["serverUrl", "protocolToken", "lastStatus", "lastPushAt"]);
  $("serverUrl").value = stored.serverUrl || "";
  $("protocolToken").value = stored.protocolToken || "";
  renderStatus(stored);
}
function renderStatus(stored) {
  const el = $("status");
  el.style.display = "block";
  el.textContent = (stored.lastStatus || "尚未推送") + (stored.lastPushAt ? " · " + new Date(stored.lastPushAt).toLocaleString("zh-CN", { hour12: false }) : "");
}
$("pushNow").onclick = async () => {
  await chrome.storage.local.set({ serverUrl: $("serverUrl").value.trim(), protocolToken: $("protocolToken").value.trim() });
  try {
    const {device} = await readQBBrowserCredentials();
    // Store only device metadata. The rotating ctk stays in memory.
    await chrome.storage.local.set({device});
    await chrome.runtime.sendMessage({ type: "push-now" });
  } catch (error) {
    await chrome.storage.local.set({lastStatus: error.message, lastPushAt: Date.now()});
  }
  setTimeout(async () => renderStatus(await chrome.storage.local.get(["lastStatus", "lastPushAt"])), 1200);
};
load();
