const $ = id => document.getElementById(id);
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
  await chrome.runtime.sendMessage({ type: "push-now" });
  setTimeout(async () => renderStatus(await chrome.storage.local.get(["lastStatus", "lastPushAt"])), 1200);
};
load();
