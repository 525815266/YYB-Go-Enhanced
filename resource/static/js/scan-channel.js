// Shared by the console, the scan page and the public one-time authorization page.
// The channel list is the one registry in Go (GET /api/scan-channels), so a channel
// added there shows up on every page. The built-in list only covers the first paint
// and the public page, which cannot read that endpoint.
(() => {
  let channels = [
    {id: 'appstore', name: 'YYB', badge: 'yyb', description: '应用宝'},
    {id: 'pcmgr', name: 'PCMgr', badge: 'pcmgr', description: '腾讯电脑管家'},
    {id: 'gameassist', name: 'GameAssist', badge: 'gameassist', description: '腾讯手游助手'},
    {id: 'ldplayer', name: 'LDPlayer', badge: 'ldplayer', description: '雷电模拟器（实验）'},
    {id: 'qbbrowser', name: 'QQ浏览器', badge: 'qbbrowser', description: 'QQ浏览器独立扫码'}
  ];
  const info = product => channels.find(item => item.id === (product || 'appstore'));
  window.scanChannels = () => channels.slice();
  window.scanChannelName = product => info(product)?.name || (product ? String(product) : 'YYB');
  window.scanChannelBadge = product => info(product)?.badge || 'yyb';
  window.scanChannelOrder = product => Math.max(0, channels.findIndex(item => item.id === (product || 'appstore')));
  window.loadScanChannels = async () => {
    try {
      const response = await fetch('/api/scan-channels', {headers: {Accept: 'application/json'}});
      const payload = await response.json();
      const list = payload && payload.data && payload.data.channels;
      if (response.ok && Array.isArray(list) && list.length && list.every(item => item && item.id && item.name && item.badge)) channels = list;
    } catch { /* keep the built-in list */ }
    return channels.slice();
  };
})();
window.confirmScanChannelSwitch = ({from, to, account}) => new Promise(resolve => {
  const dialog = document.createElement('dialog');
  dialog.className = 'scan-switch-dialog';
  dialog.setAttribute('aria-labelledby', 'scanSwitchTitle');
  const title = document.createElement('h2');
  title.id = 'scanSwitchTitle';
  title.textContent = `切换至 ${scanChannelName(to)}？`;
  const intro = document.createElement('p');
  intro.textContent = `${account || '当前账号'}：${scanChannelName(from)} → ${scanChannelName(to)}`;
  const details = document.createElement('p');
  details.textContent = '扫码成功后替换原 ID 的 OpenID、凭据与会话，保留代理、归属和任务。取消或未扫码不会更改账号。';
  const warning = document.createElement('p');
  warning.className = 'scan-switch-warning';
  warning.textContent = '跨渠道无法仅凭 OpenID 核对同一微信，请务必使用原微信。若新身份已属于其他账号，将拒绝替换。平台管理的面板变量会同步，手动维护的旧 OpenID 引用需自行更新。';
  const actions = document.createElement('div');
  actions.className = 'scan-switch-actions';
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.textContent = '保留原渠道';
  const confirm = document.createElement('button');
  confirm.type = 'button'; confirm.className = 'primary'; confirm.textContent = '确认切换';
  let settled = false;
  const finish = value => { if (settled) return; settled = true; dialog.close(); dialog.remove(); resolve(value); };
  cancel.onclick = () => finish(false);
  confirm.onclick = () => finish(true);
  dialog.addEventListener('cancel', event => { event.preventDefault(); finish(false); });
  actions.append(cancel, confirm); dialog.append(title, intro, details, warning, actions);
  document.body.append(dialog); dialog.showModal(); cancel.focus();
});
