(() => {
 const $ = id => document.getElementById(id);
 let pending = '', latest = '', hasUpdate = false, available = false, downloadable = false, downloadURL = '', busy = false, polling = false, stopped = false;
 const tell = text => { $('jobStatus').textContent = text; };
 const controls = () => {
  $('updateService').disabled = busy || !available || !hasUpdate;
  $('restartService').disabled = busy || !available;
  $('checkUpdate').disabled = busy;
  $('downloadPackage').hidden = !downloadable || !hasUpdate;
  $('downloadPackage').setAttribute('aria-disabled', String(busy));
 };
 async function api(options = {}, check = false) {
  const response = await fetch('/api/maintenance' + (check ? '?check=1' : ''), { ...options, signal: AbortSignal.timeout(15000), headers: { 'Content-Type': 'application/json', 'X-YYB-Maintenance': '1' } });
  if (response.status === 401) { location.assign('/login?next=/maintenance'); throw new Error('登录已过期'); }
  const body = await response.json();
  if (!response.ok || body.code !== 0) throw new Error(body.msg || '维护请求失败');
  return body.data;
 }
 async function load(check = false) {
  const data = await api({}, check);
  $('currentVersion').textContent = `v${data.version}`;
  available = data.managed_update === true || data.runtime?.managed_update === true;
  downloadable = data.runtime?.download_available === true && Boolean(data.runtime?.download_url);
  downloadURL = data.runtime?.download_url || '';
  $('downloadPackage').href = downloadURL || '#';
  $('downloadPackage').textContent = data.runtime?.label ? `下载 ${data.runtime.label}` : '下载当前平台版本';
  $('capability').textContent = available ? 'Docker 维护执行器已连接，可在面板更新或重启服务。' : (downloadable ? `${data.runtime.label} 独立客户端，可下载匹配当前架构的更新。` : data.message);
  $('maintenanceHelp').textContent = data.runtime?.instructions || '请按当前部署方式完成更新；替换程序前先停止服务并备份数据库。';
  if (check) { latest = data.check_error ? '' : data.latest_version; $('latestVersion').textContent = latest ? `v${latest}` : '检查失败'; }
  const job = data.agent?.job; busy = Boolean(job?.running);
  if (check) hasUpdate = data.has_update === true;
  if (latest === data.version) hasUpdate = false;
  if (job?.message) {
   if (job.running) tell(job.message);
   else if (job.finished_at) tell(`上次操作（${new Date(job.finished_at * 1000).toLocaleString('zh-CN', { hour12: false })}）：${job.message}`);
   else tell(job.message);
  }
  if (data.check_error) tell(data.check_error);
  else if (check && latest && !hasUpdate) tell('当前版本无需更新。');
  controls(); return busy;
 }
 async function poll() {
  if (polling) return; polling = true;
  // A disconnect is expected during replacement, not proof of success.
  const deadline = Date.now() + 12 * 60 * 1000;
  try {
   while (!stopped && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 3000));
    if (stopped) break;
    try { if (!(await load())) return; } catch { tell('服务正在切换，等待重新连接…'); }
   }
   if (!stopped) tell('等待超过 12 分钟。请刷新页面查看执行结果，不要重复提交。');
  } finally { polling = false; }
 }
 $('checkUpdate').onclick = async () => { busy = true; controls(); try { await load(true); } catch (e) { tell(e.message); } finally { busy = false; controls(); } };
 for (const [id, action, label] of [['updateService', 'update', '更新服务'], ['restartService', 'restart', '重启服务']]) {
  $(id).onclick = () => { pending = action; $('confirmTitle').textContent = label; $('confirmArea').hidden = false; $('confirmAction').focus(); };
 }
 $('cancelAction').onclick = () => { pending = ''; $('confirmArea').hidden = true; };
 $('downloadPackage').onclick = event => { if (busy || !downloadURL) event.preventDefault(); };
 $('confirmAction').onclick = async () => {
  if (busy || !pending) return;
  busy = true; controls(); $('confirmArea').hidden = true;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const requestID = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  try { await api({ method: 'POST', body: JSON.stringify({ action: pending, confirm: true, request_id: requestID }) }); tell('操作已受理，正在等待执行结果…'); void poll(); }
  catch (e) { tell(`${e.message}。请刷新核对执行状态后再试。`); busy = false; controls(); }
  pending = '';
 };
 window.addEventListener('pagehide', () => { stopped = true; });
 load().then(running => { if (running) void poll(); }).catch(e => tell(e.message));
})();
