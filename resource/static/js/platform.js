(() => {
  const pages = {
    "/": ["我的控制台", "账号与能力调用"],
    "/scan": ["添加账号", "微信授权"],
    "/proxies": ["代理设置", "账号网络出口"],
    "/runs": ["账号调度", "脚本任务与账号日志"],
    "/users": ["用户管理", "成员与访问权限"],
    "/settings": ["个人设置", "资料与安全"]
  };
  const view = new URLSearchParams(location.search).get("view");
  const current = location.pathname === "/runs" && view === "push" ? ["独立推送", "账号通知设置"] : (pages[location.pathname] || ["YYB Go", "管理控制台"]);
  const main = document.querySelector("main");
  if (!main) return;

  const icons = {
    dashboard: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    scan: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 8v8M8 12h8"/></svg>',
    proxy: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/><circle cx="8" cy="7" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="10" cy="17" r="2"/></svg>',
    runs: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/><path d="M4 5v14"/></svg>',
    push: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/></svg>',
    account: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="3" width="16" height="18" rx="2"/><circle cx="12" cy="9" r="3"/><path d="M7.5 18c.7-2.4 2.2-3.6 4.5-3.6s3.8 1.2 4.5 3.6"/></svg>',
    logs: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5z"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>',
    test: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m13 2-9 12h7l-1 8 9-12h-7z"/></svg>',
    users: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3"/><path d="M3 20c.6-3.1 2.6-5 6-5s5.4 1.9 6 5"/><path d="M16 5.5a3 3 0 0 1 0 5.8M17 15c2 .5 3.3 2.1 4 5"/></svg>',
    settings: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/><circle cx="12" cy="12" r="4"/></svg>',
    docs: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h9l3 3v15H6z"/><path d="M9 11h6M9 15h6M9 7h3"/></svg>',
    logout: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 5H5v14h5M14 8l4 4-4 4M9 12h9"/></svg>'
  };
  const navGroups = [
    { label: "主菜单", items: [
      ["/", "dashboard", "我的控制台", true, false],
      ["/?focus=accounts", "account", "我的微信账号", true, false],
      ["/scan", "scan", "添加账号", true, false],
      ["/runs", "runs", "账号调度", true, false],
      ["/runs?view=push", "push", "独立推送", true, false],
      ["/runs?view=logs", "logs", "调用记录", true, false],
      ["/?focus=test", "test", "接口测试", true, false],
      ["/docs/index.html", "docs", "接口文档", true, false]
    ]},
    { label: "管理", items: [
      ["/proxies", "proxy", "代理设置", true, false],
      ["/users", "users", "用户管理", false, true],
      ["/settings", "settings", "个人设置", true, true]
    ]}
  ];
  const isActive = href => {
    const [path, queryString] = href.split("?");
    if (location.pathname !== path) return false;
    const currentQuery = new URLSearchParams(location.search);
    if (!queryString) return !currentQuery.has("focus") && !currentQuery.has("view");
    const targetQuery = new URLSearchParams(queryString);
    return [...targetQuery.keys()].every(key => targetQuery.get(key) === currentQuery.get(key));
  };
  const shell = document.createElement("div");
  shell.className = "platform-shell";
  shell.innerHTML = `
    <aside class="platform-sidebar" aria-label="主导航">
      <a class="platform-brand" href="/"><span class="platform-brand-mark">Y</span><span class="platform-brand-copy"><strong>YYB Go</strong><span>微信协议管理平台</span></span></a>
      <nav class="platform-nav">${navGroups.map(group => `<div class="platform-nav-section"><div class="platform-nav-group">${group.label}</div>${group.items.map(([href, icon, label, visible, authOnly]) => `<a href="${href}" data-label="${label}" data-admin-only="${!visible}" data-auth-only="${authOnly}" ${isActive(href) ? 'aria-current="page"' : ""}><span class="platform-nav-icon">${icons[icon]}</span><span class="platform-nav-label">${label}</span></a>`).join("")}</div>`).join("")}</nav>
      <div class="platform-sidebar-foot"><button type="button" id="platformLogout" data-label="退出登录"><span class="platform-nav-icon">${icons.logout}</span><span class="platform-nav-label">退出登录</span></button></div>
    </aside>
    <button class="platform-overlay" id="platformOverlay" type="button" aria-label="关闭导航"></button>
    <section class="platform-stage">
      <header class="platform-topbar">
        <div style="display:flex;align-items:center;gap:12px;min-width:0"><button class="platform-menu" id="platformMenu" type="button" aria-label="打开导航">☰</button><div class="platform-page-context"><div class="platform-breadcrumb">YYB Go / ${current[1]}</div><div class="platform-page-title">${current[0]}</div></div></div>
        <div class="platform-user"><div class="platform-user-copy"><strong id="platformUserName">正在读取</strong><span id="platformUserRole">当前用户</span></div><span class="platform-avatar" id="platformAvatar">Y</span></div>
      </header>
      <div class="platform-main"></div>
    </section>`;
  document.body.insertBefore(shell, document.body.firstChild);
  shell.querySelector(".platform-main").appendChild(main);
  document.body.classList.add("platform-ready");

  const closeNav = () => document.body.classList.remove("platform-nav-open");
  document.getElementById("platformMenu").onclick = () => document.body.classList.toggle("platform-nav-open");
  document.getElementById("platformOverlay").onclick = closeNav;
  shell.querySelectorAll(".platform-nav a").forEach(link => link.addEventListener("click", closeNav));
  document.getElementById("platformLogout").onclick = async () => { await fetch("/logout", { method: "POST" }); location.href = "/login"; };

  fetch("/api/auth/me").then(async response => {
    if (response.status === 401) {
      location.replace("/login");
      return null;
    }
    const body = await response.json();
    if (!response.ok || body.code !== 0) throw new Error(body.msg || "读取用户失败");
    const authEnabled = body.data.auth_enabled !== false;
    const user = body.data.user;
    const name = user.display_name || user.username;
    document.getElementById("platformUserName").textContent = name;
    document.getElementById("platformUserRole").textContent = authEnabled ? (user.role === "admin" ? "管理员" : "普通用户") : "本机模式";
    const roleLabel = authEnabled ? (user.role === "admin" ? "管理员" : "普通用户") : "本机模式";
    const roleStat = document.getElementById("currentRoleText");
    const quotaStat = document.getElementById("currentQuotaText");
    if (roleStat) roleStat.textContent = roleLabel;
    if (quotaStat) quotaStat.textContent = "无限制";
    document.getElementById("platformAvatar").textContent = Array.from(name)[0]?.toUpperCase() || "Y";
    shell.querySelectorAll('[data-admin-only="true"]').forEach(link => { link.hidden = user.role !== "admin"; });
    shell.querySelectorAll('[data-auth-only="true"]').forEach(link => { link.hidden = !authEnabled; });
    const repairAccountsButton = document.getElementById("repairAccountsBtn");
    if (repairAccountsButton) repairAccountsButton.hidden = authEnabled && user.role !== "admin";
    document.querySelector(".platform-sidebar-foot").hidden = !authEnabled;
  }).catch(() => {
    document.getElementById("platformUserName").textContent = "状态未知";
    document.getElementById("platformUserRole").textContent = "请刷新页面";
  });
})();
