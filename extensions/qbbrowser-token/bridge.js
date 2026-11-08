// Based on QQ Browser's installed file-tools extension. Never guess a token
// alias or substitute an account openid for the native machine identifier.
globalThis.readQBBrowserCredentials = async function (fallbackDevice) {
  const account = globalThis.chrome?.account;
  if (typeof account?.getLoginInfo !== 'function') throw new Error('当前页面没有 QQ 浏览器账号接口，请在 QQ 浏览器扩展设置页操作');
  let timer;
  const info = await Promise.race([
    new Promise(resolve => account.getLoginInfo(resolve)),
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('QQ 浏览器账号接口超时')), 4000); })
  ]).finally(() => clearTimeout(timer));
  if (Number(info?.mode) !== 3 || !info.ctk || !info.openid) throw new Error('请先在 QQ 浏览器中使用微信登录；尚未取得微信 ctk');
  const native = globalThis.qb_external;
  const device = {...fallbackDevice};
  const methods = {machine: 'getGuid', guid: 'getFavSyncGuid', qimei: 'getQ16', version: 'getVersion'};
  for (const [key, method] of Object.entries(methods)) {
    try {
      const value = native?.[method]?.();
      if (typeof value === 'string' && value) device[key] = value;
    } catch { /* Some extension contexts expose only the asynchronous API. */ }
  }
  // Newer extension contexts also expose the native calls asynchronously.
  const external = globalThis.chrome?.qbExternal;
  const readNative = name => new Promise(resolve => {
    if (typeof external?.[name] !== 'function') return resolve('');
    const timeout = setTimeout(() => resolve(''), 2000);
    try { external[name](value => {
      const failed = globalThis.chrome?.runtime?.lastError;
      clearTimeout(timeout); resolve(!failed && typeof value === 'string' ? value : '');
    }); }
    catch { clearTimeout(timeout); resolve(''); }
  });
  await Promise.all(Object.entries(methods).map(async ([key, method]) => {
    if (!device[key]) device[key] = await readNative(method);
  }));
  // The official gateway sends guid || "". Only machine id is mandatory.
  if (!device.machine) throw new Error('QQ 浏览器未提供机器标识，请保持官方浏览器运行后重试；不能用 openid 替代');
  device.guid ||= '';
  const qbrn = {
    QIMEI36: device.qimei || '', 'Q-H5-GUID': device.guid,
    GAME_H5_DEVICE_FINGERPRINT_ID: device.machine,
    GAME_H5_DEVICE_MACHINE_ID: device.machine,
    GAME_H5_DEVICE_QB_VERSION: device.version || ''
  };
  return {credentials: {access_token: info.ctk, openid: info.openid,
    user_id: device.machine, guid: device.guid, nickname: info.nickName || '',
    qbrn_cookie: Object.entries(qbrn).map(([k,v]) => `${k}=${v}`).join('; ')}, device};
};
