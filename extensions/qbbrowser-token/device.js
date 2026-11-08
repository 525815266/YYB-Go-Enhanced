// QQ's native device bridge is restricted by page origin. An unsigned extension
// page can expose the function but return undefined. Read only device metadata
// on the public official game page; account tokens never enter that page.
globalThis.readQBBrowserDevice = async function () {
  const url = 'https://pcgame.qqbrowser.qq.com/main';
  let tab, timer, expired = false;
  let updated = () => {};
  const loaded = new Promise(resolve => {
    updated = (id, change) => { if (tab?.id === id && change.status === 'complete') resolve(); };
    chrome.tabs.onUpdated.addListener(updated);
  });
  try {
    if (typeof chrome.scripting?.executeScript !== 'function') {
      throw new Error('设备读取需要脚本权限，请在扩展管理页重新加载 1.2.0 或更新版本');
    }
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('读取官方设备页面超时，请检查 pcgame.qqbrowser.qq.com 的连接')), 15000);
    });
    await Promise.race([chrome.tabs.create({url, active: false}).then(async created => {
      tab = created;
      if (expired) {
        await chrome.tabs.remove(tab.id).catch(() => {});
        throw new Error('设备读取已结束');
      }
    }), deadline]);
    return await Promise.race([(async () => {
      const current = await chrome.tabs.get(tab.id);
      if (current.status !== 'complete') await loaded;
      if (expired) throw new Error('设备读取已结束');
      const before = await chrome.tabs.get(tab.id);
      if (expired) throw new Error('设备读取已结束');
      if (new URL(before.url).origin !== new URL(url).origin) throw new Error('设备页面跳转到其他站点，已停止读取');
      const results = await chrome.scripting.executeScript({target: {tabId: tab.id}, world: 'MAIN', func: () => {
        const native = globalThis.qb_external;
        const read = name => { try { const v = native?.[name]?.(); return typeof v === 'string' ? v : ''; } catch { return ''; } };
        return {origin: location.origin, machine: read('getGuid'), guid: read('getFavSyncGuid'), qimei: read('getQ16'), version: read('getVersion')};
      }});
      if (expired) throw new Error('设备读取已结束');
      const device = results?.[0]?.result;
      const after = await chrome.tabs.get(tab.id);
      if (device?.origin !== new URL(url).origin || new URL(after.url).origin !== device.origin) {
        throw new Error('设备页面来源校验失败，已停止读取');
      }
      if (!device.machine) throw new Error('官方页面也未返回机器标识，请保留此提示供排查；无需重扫');
      delete device.origin;
      return device;
    })(), deadline]);
  } finally {
    expired = true;
    clearTimeout(timer);
    chrome.tabs.onUpdated.removeListener(updated);
    if (tab?.id !== undefined) await chrome.tabs.remove(tab.id).catch(() => {});
  }
};
