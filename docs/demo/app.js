(() => {
  "use strict";

  const accounts = [
    { id: 1, name: "张三", initial: "张", openid: "owDemo1nR4pLm7Qa81Fx", masked: "owDemo1...a81Fx", status: "ok", state: "凭据正常", refresh: "3 分钟前", proxy: "直连 · 山东济南", health: 82, healthText: "96 分钟", color: "coral" },
    { id: 3, name: "李四", initial: "李", openid: "owDemo3sA7hc3Tzqk82QL", masked: "owDemo3...k82QL", status: "ok", state: "凭据正常", refresh: "11 分钟前", proxy: "品赞 · 山东青岛", health: 71, healthText: "81 分钟", color: "blue" },
    { id: 4, name: "王五", initial: "王", openid: "owDemo4mP1vFbU0As91Jn", masked: "owDemo4...s91Jn", status: "warning", state: "建议重扫", refresh: "2 小时前", proxy: "静态代理 · 上海", health: 38, healthText: "43 分钟", color: "green" },
    { id: 5, name: "赵六", initial: "赵", openid: "owDemo5qH3nKyR9Wd62Pc", masked: "owDemo5...d62Pc", status: "ok", state: "凭据正常", refresh: "18 分钟前", proxy: "直连 · 山东济南", health: 66, healthText: "75 分钟", color: "amber" },
    { id: 6, name: "微信账号 6", initial: "6", openid: "owDemo6uT8eLzA4Bx73Vf", masked: "owDemo6...x73Vf", status: "expired", state: "需要重扫", refresh: "3 天前", proxy: "直连", health: 4, healthText: "已过期", color: "blue" }
  ];

  const jobs = [
    { id: "longfor", name: "龙湖天街签到", file: "LHTJ.js", cron: "每天 08:04", last: "今天 08:04 · 成功", enabled: true },
    { id: "laichong", name: "莱充积分任务", file: "laichong_points.py", cron: "每 7 小时", last: "今天 09:12 · 运行中", enabled: true },
    { id: "jtexpress", name: "极兔速递签到", file: "jtexpress_sign.py", cron: "每天 07:31", last: "今天 07:31 · 成功", enabled: true },
    { id: "wanhong", name: "万虹停车券", file: "wanhong_hxh.py", cron: "每天 09:10", last: "昨天 09:10 · 已停用", enabled: false }
  ];

  const views = {
    dashboard: ["我的控制台", "账号与能力调用"],
    scan: ["添加账号", "微信扫码授权"],
    runs: ["账号运行管理", "脚本任务与账号日志"],
    proxies: ["代理设置", "账号网络出口"],
    api: ["接口调试", "协议能力调用"],
    users: ["用户管理", "成员与访问权限"],
    settings: ["系统设置", "服务与面板配置"]
  };

  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  let selectedAccountId = 1;
  let proxyAccountId = 1;
  let scanStep = 1;
  let qrTimer = null;
  let logTimer = null;
  let activeJob = null;

  const getAccount = id => accounts.find(account => account.id === Number(id)) || accounts[0];
  const escapeHtml = value => String(value).replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);

  function showToast(message, type = "success") {
    const toast = document.createElement("div");
    toast.className = `toast ${type === "error" ? "error" : ""}`;
    toast.innerHTML = `<i></i><span>${escapeHtml(message)}</span>`;
    $("#toastRegion").appendChild(toast);
    window.setTimeout(() => toast.remove(), 2800);
  }

  function navigate(viewName) {
    if (!views[viewName]) return;
    $$(".view").forEach(view => view.classList.toggle("active", view.dataset.viewPanel === viewName));
    $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === viewName));
    $("#pageTitle").textContent = views[viewName][0];
    $("#breadcrumb").textContent = views[viewName][1];
    document.body.classList.remove("nav-open");
    history.replaceState(null, "", `#${viewName}`);
    if (viewName === "scan") renderScan();
    if (viewName === "runs") renderJobs();
    if (viewName === "proxies") renderProxyAccounts();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function statusClass(account) {
    if (account.status === "ok") return "state-ok";
    if (account.status === "warning") return "state-warning";
    return "state-expired";
  }

  function renderAccounts(filter = "") {
    const query = filter.trim().toLowerCase();
    const matches = accounts.filter(account => `${account.name} ${account.id} ${account.openid}`.toLowerCase().includes(query));
    $("#accountList").innerHTML = matches.length ? matches.map(account => `
      <button class="account-row ${account.id === selectedAccountId ? "selected" : ""}" type="button" data-account-id="${account.id}">
        <span class="account-avatar ${account.color}">${escapeHtml(account.initial)}</span>
        <span class="account-copy"><strong>${escapeHtml(account.name)}</strong><small>ID ${account.id} · ${escapeHtml(account.masked)}</small></span>
        <span class="account-state"><span class="${statusClass(account)}">${escapeHtml(account.state)}</span><small>${escapeHtml(account.refresh)}</small></span>
      </button>`).join("") : '<div class="empty-list">没有匹配的账号</div>';
    $$("[data-account-id]").forEach(button => button.addEventListener("click", () => selectAccount(button.dataset.accountId)));
  }

  function selectAccount(id) {
    selectedAccountId = Number(id);
    const account = getAccount(id);
    renderAccounts($("#accountSearch").value);
    $("#detailAvatar").textContent = account.initial;
    $("#detailAvatar").className = `detail-avatar ${account.color}`;
    $("#detailName").textContent = account.name;
    $("#detailId").textContent = account.id;
    $("#detailOpenid").textContent = account.masked;
    $("#detailProxy").textContent = account.proxy;
    $("#detailRefresh").textContent = account.refresh;
    $("#healthText").textContent = account.healthText;
    $("#healthProgress").style.width = `${account.health}%`;
    $("#healthProgress").style.background = account.health < 15 ? "var(--red)" : account.health < 45 ? "var(--amber)" : "var(--green)";
    const state = $(".detail-head .inline-status");
    state.className = `inline-status ${account.status === "ok" ? "ok" : statusClass(account)}`;
    state.innerHTML = `<i></i>${escapeHtml(account.state)}`;
    populateAccountSelects();
  }

  function populateAccountSelects() {
    const options = accounts.map(account => `<option value="${account.id}" ${account.id === selectedAccountId ? "selected" : ""}>${escapeHtml(account.name)} · ID ${account.id}</option>`).join("");
    $("#runAccount").innerHTML = options;
    $("#apiAccount").innerHTML = options;
  }

  function makeQr() {
    const fixed = new Set();
    const size = 13;
    const markFinder = (x, y) => {
      for (let row = y; row < y + 5; row += 1) for (let column = x; column < x + 5; column += 1) {
        if (row === y || row === y + 4 || column === x || column === x + 4 || (row >= y + 2 && row <= y + 2 && column >= x + 2 && column <= x + 2)) fixed.add(row * size + column);
      }
    };
    markFinder(0, 0); markFinder(8, 0); markFinder(0, 8);
    return Array.from({ length: size * size }, (_, index) => `<i class="${fixed.has(index) || ((index * 17 + index % 7 * 11) % 19 < 8) ? "on" : ""}"></i>`).join("");
  }

  function setScanStep(step) {
    scanStep = step;
    $("#scanStepCaption").textContent = `步骤 ${step} / 3`;
    $$('[data-scan-step]').forEach(item => {
      const itemStep = Number(item.dataset.scanStep);
      item.classList.toggle("active", itemStep === step);
      item.classList.toggle("done", itemStep < step);
    });
  }

  function renderScan() {
    window.clearInterval(qrTimer);
    setScanStep(scanStep);
    if (scanStep === 1) {
      $("#scanPrimary").innerHTML = `<div class="scan-card"><div class="qr-code" aria-label="演示二维码">${makeQr()}</div><div class="qr-timer">二维码将在 <strong id="qrSeconds">120</strong> 秒后刷新</div><h3>使用手机微信扫码</h3><p>在手机上确认登录后，页面会自动显示保存的账号 ID 与 OpenID。</p><button class="button secondary scan-demo-action" type="button" id="simulateScan">模拟扫码成功</button></div>`;
      let seconds = 120;
      qrTimer = window.setInterval(() => { seconds -= 1; const timer = $("#qrSeconds"); if (timer) timer.textContent = seconds; if (seconds <= 0) { window.clearInterval(qrTimer); renderScan(); } }, 1000);
      $("#simulateScan").onclick = () => { window.clearInterval(qrTimer); setScanStep(2); renderScan(); };
      return;
    }
    if (scanStep === 2) {
      $("#scanPrimary").innerHTML = `<div class="scan-card"><span class="scan-success-icon"><svg viewBox="0 0 24 24"><path d="m6 12 4 4 8-9"/></svg></span><h3>微信授权成功</h3><p>确认备注后保存账号。重复扫码同一 OpenID 会更新原账号。</p><div class="scan-account-card"><span class="detail-avatar blue">孙</span><dl><div><dt>账号 ID</dt><dd>7</dd></div><div><dt>账号状态</dt><dd>凭据正常</dd></div><div><dt>OpenID</dt><dd class="mono">owDemo7...m92Fa</dd></div><div><dt>登录代理</dt><dd>${escapeHtml($("#scanProxy").value)}</dd></div></dl></div><label class="field scan-input"><span>账号备注</span><input id="newAccountRemark" value="孙七" maxlength="32"></label><div class="scan-finish-actions"><button class="button secondary" type="button" id="rescan">重新扫码</button><button class="button primary" type="button" id="saveScanned">保存账号${$("#autoSync").checked ? "并同步" : ""}</button></div></div>`;
      $("#rescan").onclick = () => { setScanStep(1); renderScan(); };
      $("#saveScanned").onclick = () => { setScanStep(3); renderScan(); };
      return;
    }
    $("#scanPrimary").innerHTML = `<div class="scan-card"><span class="scan-success-icon"><svg viewBox="0 0 24 24"><path d="m6 12 4 4 8-9"/></svg></span><h3>账号已保存</h3><p>${$("#autoSync").checked ? "账号 7 已合并到青龙环境变量 YYB_SERVER，未产生重复记录。" : "账号 7 已保存，暂未同步自动化面板。"}</p><div class="scan-account-card"><span class="detail-avatar blue">孙</span><dl><div><dt>账号备注</dt><dd>孙七</dd></div><div><dt>同步结果</dt><dd>${$("#autoSync").checked ? "青龙 · 已完成" : "等待手动同步"}</dd></div><div><dt>账号引用</dt><dd>http://yyb-go:8000@7</dd></div><div><dt>凭据状态</dt><dd>可直接调用</dd></div></dl></div><div class="scan-finish-actions"><button class="button secondary" type="button" id="scanAnother">继续添加</button><button class="button primary" type="button" data-go="dashboard">查看账号</button></div></div>`;
    $("#scanAnother").onclick = () => { setScanStep(1); renderScan(); };
    $("#scanPrimary [data-go]").onclick = () => { setScanStep(1); navigate("dashboard"); showToast("演示账号已保存，刷新页面后恢复原始数据"); };
  }

  function renderJobs() {
    const state = $("#runStatus").value;
    const query = $("#runSearch").value.trim().toLowerCase();
    const filtered = jobs.filter(job => (state === "all" || (state === "enabled") === job.enabled) && `${job.name} ${job.file}`.toLowerCase().includes(query));
    $("#enabledJobs").textContent = jobs.filter(job => job.enabled).length;
    $("#disabledJobs").textContent = jobs.filter(job => !job.enabled).length;
    $("#jobs").innerHTML = filtered.length ? filtered.map(job => `
      <article class="job-row" data-job-id="${job.id}">
        <div class="job-main"><span class="job-icon"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></span><span><strong>${escapeHtml(job.name)}</strong><small>task ${escapeHtml(job.file)}</small></span></div>
        <div class="job-meta"><small>调度</small><strong>${escapeHtml(job.cron)}</strong></div>
        <label class="job-toggle"><span>${job.enabled ? "已启用" : "已停用"}</span><span class="switch"><input class="job-enabled" type="checkbox" ${job.enabled ? "checked" : ""}><span></span></span></label>
        <div class="job-controls"><button class="icon-button run-job" type="button" aria-label="立即运行"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></button><button class="icon-button view-log" type="button" aria-label="查看日志"><svg viewBox="0 0 24 24"><path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/></svg></button></div>
      </article>`).join("") : '<div class="empty-list">没有匹配的脚本</div>';
    $$(".job-row").forEach(row => {
      const job = jobs.find(item => item.id === row.dataset.jobId);
      row.querySelector(".job-enabled").onchange = event => { job.enabled = event.target.checked; renderJobs(); showToast(`${job.name}已${job.enabled ? "启用" : "停用"}`); };
      row.querySelector(".run-job").onclick = () => openLog(job, true);
      row.querySelector(".view-log").onclick = () => openLog(job, false);
    });
  }

  const sampleLogs = job => [
    `## 开始执行... ${new Date().toLocaleString("zh-CN", { hour12: false })}`,
    `加载账号配置：${getAccount($("#runAccount").value).name}（ID ${$("#runAccount").value}）`,
    `读取任务：${job.name} / ${job.file}`,
    "http://yyb-go:8000 获取 code 成功",
    "账号凭据校验通过，开始查询任务状态",
    job.id === "longfor" ? "活动：日日签 日日赚，今日=未签到" : "任务列表读取成功，待领取奖励 2 项",
    "提交业务请求成功",
    "本次执行完成：积分 +5",
    "## 执行结束，退出码 0"
  ];

  function openLog(job, runNow) {
    window.clearInterval(logTimer);
    activeJob = job;
    const account = getAccount($("#runAccount").value);
    $("#logAccount").textContent = `${account.name} · ID ${account.id}`;
    $("#logTitle").textContent = job.name;
    $("#logStatus").className = `run-status ${runNow ? "running" : "success"}`;
    $("#logStatus").textContent = runNow ? "运行中" : "最近成功";
    $("#logTime").textContent = runNow ? "刚刚开始" : job.last;
    const output = $("#logOutput");
    output.textContent = "";
    $("#logDrawer").classList.add("open");
    $("#logDrawer").setAttribute("aria-hidden", "false");
    $("#drawerOverlay").classList.add("open");
    if (!runNow) {
      output.textContent = sampleLogs(job).join("\n");
      output.scrollTop = output.scrollHeight;
      return;
    }
    const lines = sampleLogs(job);
    let index = 0;
    const append = () => {
      output.textContent += `${index ? "\n" : ""}${lines[index]}`;
      if ($("#autoScroll").checked) output.scrollTop = output.scrollHeight;
      index += 1;
      if (index >= lines.length) {
        window.clearInterval(logTimer);
        $("#logStatus").className = "run-status success";
        $("#logStatus").textContent = "成功";
        $("#logTime").textContent = "刚刚完成 · 9 秒";
        showToast(`${job.name}演示运行完成`);
      }
    };
    append();
    logTimer = window.setInterval(append, 720);
  }

  function closeLog() {
    window.clearInterval(logTimer);
    $("#logDrawer").classList.remove("open");
    $("#logDrawer").setAttribute("aria-hidden", "true");
    $("#drawerOverlay").classList.remove("open");
  }

  function renderProxyAccounts() {
    $("#proxyAccountList").innerHTML = accounts.map(account => `<button class="proxy-account ${account.id === proxyAccountId ? "selected" : ""}" type="button" data-proxy-account="${account.id}"><span class="account-avatar ${account.color}">${escapeHtml(account.initial)}</span><span><strong>${escapeHtml(account.name)}</strong><small>ID ${account.id} · ${escapeHtml(account.proxy)}</small></span></button>`).join("");
    $$('[data-proxy-account]').forEach(button => button.onclick = () => { proxyAccountId = Number(button.dataset.proxyAccount); const account = getAccount(proxyAccountId); $("#proxyAvatar").textContent = account.initial; $("#proxyAvatar").className = `detail-avatar ${account.color}`; $("#proxyName").textContent = `${account.name} · ID ${account.id}`; renderProxyAccounts(); });
  }

  function executeApi() {
    const button = $("#executeApi");
    const account = getAccount($("#apiAccount").value);
    const action = $("#apiAction").value;
    button.disabled = true;
    button.textContent = "正在调用";
    $("#responseTime").textContent = "请求中";
    window.setTimeout(() => {
      let body;
      if (action === "getCode") body = { code: 0, message: "success", data: { ref: String(account.id), app_id: $("#apiAppid").value, wx_code: "061DemoCode9u2VnQ", expires_in: 300 }, demo: true };
      else if (action === "getUserInfo") body = { code: 0, message: "success", data: { id: account.id, remark: account.name, openid: account.openid, status: account.status === "expired" ? "expired" : "alive" }, demo: true };
      else body = { code: 0, message: "success", data: { api_name: "callFunction", result: { errMsg: "cloud.callFunction:ok", requestId: "demo-request-92fa" } }, demo: true };
      $("#apiResponse").innerHTML = `<code>${escapeHtml(JSON.stringify(body, null, 2))}</code>`;
      $("#responseTime").textContent = "HTTP 200 · 186 ms";
      button.disabled = false;
      button.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>执行调用';
      showToast("演示接口调用成功");
    }, 650);
  }

  $$(".nav-item").forEach(item => item.onclick = () => navigate(item.dataset.view));
  $$('[data-go]').forEach(item => item.onclick = () => navigate(item.dataset.go));
  $("#menuButton").onclick = () => document.body.classList.add("nav-open");
  $("#sidebarOverlay").onclick = () => document.body.classList.remove("nav-open");
  $("#accountSearch").oninput = event => renderAccounts(event.target.value);
  $("#refreshAccounts").onclick = event => { const button = event.currentTarget; button.disabled = true; showToast("正在刷新 5 个演示账号"); window.setTimeout(() => { button.disabled = false; selectAccount(selectedAccountId); showToast("账号状态已刷新"); }, 700); };
  $("#copyOpenid").onclick = async () => { const value = getAccount(selectedAccountId).openid; try { await navigator.clipboard.writeText(value); showToast("OpenID 已复制"); } catch { showToast(`演示 OpenID：${value}`); } };
  $("#versionButton").onclick = () => showToast("当前已是演示环境最新版本 v0.2.18");
  $("#runAccount").onchange = event => { selectedAccountId = Number(event.target.value); renderJobs(); };
  $("#runStatus").onchange = renderJobs;
  $("#runSearch").oninput = renderJobs;
  $("#syncJobs").onclick = event => { event.currentTarget.disabled = true; showToast("正在读取青龙任务"); window.setTimeout(() => { event.currentTarget.disabled = false; showToast("任务列表同步完成"); }, 650); };
  $("#closeLog").onclick = closeLog;
  $("#drawerOverlay").onclick = closeLog;
  $("#clearLog").onclick = () => { $("#logOutput").textContent = ""; showToast("仅清空当前演示显示"); };
  $("#rerunJob").onclick = () => activeJob && openLog(activeJob, true);
  $("#saveProxy").onclick = () => { const account = getAccount(proxyAccountId); const mode = $('input[name="proxyMode"]:checked').value; account.proxy = mode === "direct" ? `直连 · ${$("#proxyProvince").value.replace("省", "")}${$("#proxyCity").value.replace("市", "")}` : `${$("#proxyProfile").value} · ${$("#proxyCity").value}`; renderProxyAccounts(); selectAccount(selectedAccountId); showToast(`${account.name}的代理设置已保存`); };
  $("#testProxy").onclick = event => { const button = event.currentTarget; button.disabled = true; button.textContent = "测试中"; window.setTimeout(() => { button.disabled = false; button.textContent = "重新测试"; showToast("代理连接正常，出口地区匹配"); }, 750); };
  $("#apiAction").onchange = event => $(".payload-field").hidden = event.target.value !== "operateWxData";
  $("#executeApi").onclick = executeApi;
  document.addEventListener("keydown", event => { if (event.key === "Escape") { closeLog(); document.body.classList.remove("nav-open"); } });

  renderAccounts();
  populateAccountSelects();
  renderJobs();
  renderProxyAccounts();
  selectAccount(1);
  const initialView = location.hash.slice(1);
  navigate(views[initialView] ? initialView : "dashboard");
})();
