(() => {
  const $ = id => document.getElementById(id);
  const form = $("logFilters"), rows = $("logRows");
  const categories = {system:"服务运行",api:"接口请求",protocol:"协议调用",keepalive:"后台续期",account:"账号授权",management:"管理操作",auth:"面板登录"};
  const levels = {debug:"调试",info:"信息",warn:"警告",error:"错误"};
  const stages = {refresh_token:"凭据续期",check_login:"授权检查",login_buffer:"登录票据",manual_auth:"会话登录",native_autoauth_transport:"独立恢复·传输",native_autoauth_application:"独立恢复·应用",native_autoauth_business:"独立恢复·业务",shortlink_transport:"短连接·传输",shortlink_application:"短连接·应用",pcmgr_login_buffer:"PCMgr 票据",pcmgr_key_renewal:"PCMgr 续期"};
  const cursors = [0];
  let page = 0, next = 0, pending = false, timer, sequence = 0;
  let filters = new URLSearchParams(), since = 0;
  const el = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
  const busy = state => {
    pending = state;
    $("logResults").setAttribute("aria-busy", String(state));
    $("refreshLogs").disabled = state;
    $("previousLogs").disabled = state || page === 0;
    $("nextLogs").disabled = state || !next;
    form.querySelector('[type="submit"]').disabled = state;
  };
  function render(entries) {
    const fragment = document.createDocumentFragment();
    if (!entries.length) { const tr = el("tr"), td = el("td", "没有符合条件的记录。可调整筛选条件，或在后续操作后刷新。", "log-empty"); td.colSpan = 5; tr.append(td); fragment.append(tr); }
    for (const entry of entries) {
      const tr = el("tr", undefined, "log-row"), date = new Date(entry.created_at);
      const timeCell = el("td"), time = el("time", date.toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}));
      time.dateTime = date.toISOString(); timeCell.append(time);
      const severity = el("td"), badge = el("span", levels[entry.level] || entry.level, "log-badge"); badge.dataset.level = entry.level; severity.append(badge);
      const account = el("td");
      account.append(el("span", entry.account_id ? `ID ${entry.account_id}` : "全局", "log-account"));
      if (entry.channel) account.append(el("span", entry.channel === "pcmgr" ? "PCMgr" : "YYB", "log-channel"));
      const message = el("td"), details = entry.details || {};
      message.append(el("span", entry.message));
      if (details.rejections?.length) message.append(el("span", details.rejections.map(item => `${stages[item.stage] || item.stage} ${item.code}${item.wechat_code ? ` / WX ${item.wechat_code}` : ""}`).join(" · "), "log-rejections"));
      const disclosure = el("details", undefined, "log-detail"), dl = el("dl");
      disclosure.append(el("summary", "查看详情"));
      const values = [["事件", entry.event], ["路径", details.route], ["方法", details.method], ["HTTP", details.http_status], ["耗时", details.duration_ms !== undefined ? `${details.duration_ms} ms` : ""], ["丢弃条数", details.count]];
      for (const [label, value] of values) if (value !== undefined && value !== "" && value !== 0) dl.append(el("dt", label), el("dd", String(value)));
      disclosure.append(dl); message.append(disclosure);
      tr.append(timeCell, severity, el("td", categories[entry.category] || entry.category), account, message); fragment.append(tr);
    }
    rows.replaceChildren(fragment);
  }
  async function load() {
    if (pending) return;
    const current = ++sequence;
    busy(true); $("logError").hidden = true;
    const q = new URLSearchParams(filters);
    if (cursors[page]) q.set("before", cursors[page]);
    if (since) q.set("since", since);
    try {
      const response = await fetch(`/api/system-logs?${q}`, {cache:"no-store",signal:AbortSignal.timeout(15000)});
      const body = await response.json();
      if (!response.ok || body.code !== 0) throw new Error(body.msg || "读取日志失败");
      if (current !== sequence) return;
      const data = body.data || {};
      render(data.entries || []); next = data.next_before || 0;
      $("logStatus").textContent = `本页 ${data.entries.length} 条 · ${new Date().toLocaleTimeString("zh-CN",{hour12:false})} 更新`;
      $("logPage").textContent = `第 ${page + 1} 页`;
    } catch (error) {
      $("logError").textContent = error.name === "TimeoutError" ? "读取超时，请刷新重试。" : error.message;
      $("logError").hidden = false;
      $("logStatus").textContent = "日志读取失败";
      if (!rows.querySelector(".log-row")) render([]);
    } finally { busy(false); }
  }
  function applyFilters() {
    if (pending) return;
    filters = new URLSearchParams();
    const data = new FormData(form);
    for (const name of ["category","level","channel","account_id"]) if (data.get(name)) filters.set(name, data.get(name));
    since = Date.now() - Number(data.get("hours") || 168) * 3600000;
    page = 0; cursors.splice(0, cursors.length, 0); next = 0;
    load();
  }
  form.addEventListener("submit", event => { event.preventDefault(); applyFilters(); });
  form.addEventListener("reset", () => { if (!pending) setTimeout(applyFilters, 0); });
  $("refreshLogs").onclick = () => { if (pending) return; page = 0; cursors.splice(0, cursors.length, 0); load(); };
  $("previousLogs").onclick = () => { if (!pending && page > 0) { page--; load(); } };
  $("nextLogs").onclick = () => { if (!pending && next) { cursors[++page] = next; load(); } };
  $("liveLogs").onchange = () => { clearInterval(timer); if ($("liveLogs").checked) timer = setInterval(() => { if (!document.hidden && !pending && page === 0 && !rows.querySelector("details[open]")) load(); }, 15000); };
  window.addEventListener("pagehide", () => clearInterval(timer));
  applyFilters();
})();
