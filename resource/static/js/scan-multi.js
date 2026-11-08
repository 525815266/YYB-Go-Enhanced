// Multi-channel scanning for the scan page: one QR per channel for the same WeChat.
// Every channel runs its own QR session (create, poll, confirm). As accounts are
// confirmed they are bound into one fleet (POST /api/fleet/bind-many), so scripts
// keep using a single account while requests rotate across the channels.
// The channels come from scanChannels(), so a channel added on the server shows up
// here without a change to this file.
(() => {
  'use strict';
  const QR_TTL = 110;
  const POLL_MS = 1500;
  const CONFIRM_RETRIES = 11;
  const $ = id => document.getElementById(id);
  const node = (tag, className, content) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = content;
    return element;
  };
  const productOf = account => account.scan_product || 'appstore';
  const labelOf = account => account.remark || account.nickname || account.alias || account.openid || `账号 ${account.id}`;
  const statusText = status => status === 'alive' ? '可用' : status === 'expired' ? '需重扫' : status === 'session_only' ? '需补授权' : '待确认';
  const channelInfo = product => scanChannels().find(channel => channel.id === product) || {id: product, name: scanChannelName(product), badge: scanChannelBadge(product), description: ''};
  const badgeImage = channel => {
    const image = document.createElement('img');
    image.alt = '';
    image.src = `/static/images/scan-${channel.badge}.png?v=093`;
    image.addEventListener('error', () => image.remove());
    return image;
  };
  const remember = on => { try { localStorage.setItem('yyb-multi-scan', on ? '1' : '0'); } catch { /* optional */ } };
  const remembered = () => { try { return localStorage.getItem('yyb-multi-scan') === '1'; } catch { return false; } };

  let cfg = null;
  let active = false;
  let base = null;                     // {members, primary}: set when channels are added to an existing account
  let release = () => {};
  let queue = new Promise(resolve => { release = resolve; });   // serialises switch changes, after init()
  let bindChain = Promise.resolve();   // serialises bind requests
  const scanners = new Map();          // channel id -> Scanner
  const accounts = new Map();          // account id -> account, in the order they were confirmed on this page
  const bound = new Set();             // ids known to sit in the target fleet
  const forced = new Set();            // ids the operator agreed to bind although the WeChat could not be matched
  const kept = new Set();              // ids the operator chose to leave independent
  const problems = new Map();          // id -> {text, mismatch}
  const notices = new Map();           // id -> note from the server

  class Scanner {
    constructor(channel) {
      this.channel = channel;
      this.kind = 'qr';                // 'qr': has or wants a QR, 'done': an account of this channel is in the group
      this.account = null;
      this.sid = null;
      this.gen = 0;
      this.busy = null;
      this.left = 0;
      this.pollTimer = null;
      this.clock = null;
      this.card = node('article', 'multi-card');
      this.card.dataset.product = channel.id;
      const head = node('div', 'multi-card-head');
      head.append(badgeImage(channel), node('strong', '', channel.name), node('span', '', channel.description || ''));
      this.qr = node('div', 'multi-qr');
      this.qr.setAttribute('role', 'img');
      this.qr.setAttribute('aria-label', `${channel.name} 登录二维码`);
      this.line = node('div', 'multi-line');
      this.line.setAttribute('aria-live', 'polite');
      this.refresh = node('button', 'btn', '重新生成');
      this.refresh.type = 'button';
      this.refresh.addEventListener('click', () => this.start());
      const actions = node('div', 'multi-actions');
      actions.append(this.refresh);
      this.card.append(head, this.qr, this.line, actions);
    }

    state(name, text) {
      this.card.dataset.state = name;
      this.line.textContent = text;
      this.refresh.disabled = name === 'saving';
    }

    stopTimers() {
      clearInterval(this.pollTimer);
      clearInterval(this.clock);
      this.pollTimer = this.clock = null;
    }

    async retire() {
      const previous = this.sid;
      this.sid = null;
      if (!previous) return;
      try { await cfg.api('POST', `/qr/${previous}/cancel`); } catch { /* the server drops it by age anyway */ }
    }

    async stop() {
      this.gen += 1;
      this.stopTimers();
      await this.retire();
    }

    // The channel already has an account in the group: no QR until the operator
    // asks for a re-scan.
    showDone(account, existing) {
      this.gen += 1;
      this.stopTimers();
      this.kind = 'done';
      this.account = account;
      this.qr.classList.remove('waiting');
      // A bound account that needs a new scan says so and offers it first.
      const health = existing && account.status !== 'alive' ? (account.status === 'expired' ? 'expired' : 'warn') : '';
      if (health) this.card.dataset.health = health;
      else delete this.card.dataset.health;
      this.refresh.classList.toggle('primary', !!health);
      if (existing) {
        const box = node('div', 'multi-bound');
        box.append(node('span', 'check', '✓'), node('b', '', `已绑定 ID ${account.id}`));
        this.qr.replaceChildren(box);
        this.state('bound', `${labelOf(account)} · ${statusText(account.status)}`);
      } else {
        this.qr.replaceChildren(node('span', 'check', '✓'));
        this.state('done', `已添加 · ID ${account.id}`);
      }
      this.refresh.textContent = '重新扫码';
    }

    async start() {
      const gen = ++this.gen;
      this.kind = 'qr';
      this.stopTimers();
      await this.retire();
      if (gen !== this.gen) return;
      delete this.card.dataset.health;
      this.refresh.classList.remove('primary');
      this.refresh.textContent = '重新生成';
      this.qr.classList.remove('waiting');
      this.qr.textContent = '正在生成二维码';
      this.state('loading', '正在获取二维码');
      try {
        // The panel is synced a little later, once the accounts are merged: it lists the primary only.
        const result = await cfg.api('POST', '/qr?as_base64=true&panel_sync=defer', cfg.proxyPayload(this.channel.id));
        if (gen !== this.gen) {
          cfg.api('POST', `/qr/${result.session_id}/cancel`).catch(() => {});
          return;
        }
        this.sid = result.session_id;
        const image = document.createElement('img');
        image.alt = `${this.channel.name} 微信登录二维码`;
        image.src = result.image_base64 || `${result.image_url}?t=${Date.now()}`;
        image.addEventListener('error', () => { if (gen === this.gen) this.state('error', '二维码图片加载失败，请重新生成'); });
        this.qr.replaceChildren(image);
        this.state('waiting', result.proxy ? `等待扫码 · ${result.proxy}` : '等待扫码');
        this.left = QR_TTL;
        this.clock = setInterval(() => {
          if (gen !== this.gen) return;
          this.left -= 1;
          if (this.left <= 0) {
            this.stopTimers();
            this.start();
          } else if (this.card.dataset.state === 'waiting') {
            this.line.textContent = `等待扫码 · ${this.left}s 后自动刷新`;
          }
        }, 1000);
        this.pollTimer = setInterval(() => this.poll(gen), POLL_MS);
      } catch (error) {
        if (gen === this.gen) this.state('error', `获取二维码失败：${error.message}`);
      }
    }

    async poll(gen) {
      const sid = this.sid;
      if (!sid || gen !== this.gen || this.busy === sid) return;
      this.busy = sid;
      try {
        const result = await cfg.api('GET', `/qr/${sid}/poll`);
        if (sid !== this.sid || gen !== this.gen) return;
        if (result.status === 'pending') {
          this.qr.classList.remove('waiting');
          if (this.card.dataset.state !== 'waiting') this.state('waiting', '等待扫码');
        } else if (result.status === 'scanned') {
          this.qr.classList.add('waiting');
          this.state('scanned', '已扫码，请在手机上确认授权');
        } else if (result.status === 'authorized' || result.status === 'confirmed') {
          this.qr.classList.add('waiting');
          this.stopTimers();
          this.confirm(sid, gen, 0);
        } else if (result.status === 'expired' || result.status === 'unknown') {
          this.stopTimers();
          this.start();
        } else if (result.status === 'cancelled') {
          this.stopTimers();
          this.qr.classList.remove('waiting');
          this.state('error', '已取消，请重新生成二维码');
        }
      } catch {
        if (sid === this.sid && gen === this.gen) this.line.textContent = '网络暂时波动，正在自动重试';
      } finally {
        if (this.busy === sid) this.busy = null;
      }
    }

    async confirm(sid, gen, attempt) {
      if (sid !== this.sid || gen !== this.gen) return;
      this.state('saving', '授权成功，正在保存账号');
      try {
        const account = await cfg.api('POST', `/qr/${sid}/confirm`);
        if (sid !== this.sid || gen !== this.gen) return;
        this.sid = null;
        this.showDone(account, false);
        onConfirmed(account);
      } catch (error) {
        if (sid !== this.sid || gen !== this.gen) return;
        if ((error.status === 409 || error.status === 502) && attempt < CONFIRM_RETRIES) {
          this.state('saving', '授权已确认，正在自动完成账号保存');
          setTimeout(() => this.confirm(sid, gen, attempt + 1), 1500);
          return;
        }
        this.qr.classList.remove('waiting');
        this.state('error', `保存账号失败：${error.message}，请重新生成二维码`);
      }
    }
  }

  // ----- the group -----

  // The fleet the accounts of this page end up in is rooted at the account that
  // anchors it: the one being supplemented, else the fleet an account scanned
  // here already belongs to, else the first account scanned here.
  function rootId() {
    if (base) return base.primary.id;
    let first = 0;
    for (const account of accounts.values()) {
      if (account.fleet_primary_id) return account.fleet_primary_id;
      if (!first) first = account.id;
    }
    return first;
  }

  const inGroup = (account, root) => account.id === root || account.fleet_primary_id === root || bound.has(account.id);

  function onConfirmed(account) {
    accounts.set(account.id, account);
    kept.delete(account.id);
    const root = rootId();
    if (!base || account.id === root) cfg.showAccount(accounts.get(root) || account);
    renderMembers();
    enqueueBind();
  }

  function enqueueBind() {
    bindChain = bindChain.then(bindPending).catch(() => {});
    return bindChain;
  }

  async function bindPending() {
    const root = rootId();
    if (!root) return;
    const pending = [...accounts.values()].filter(account => !inGroup(account, root) && !kept.has(account.id) && (forced.has(account.id) || !problems.has(account.id)));
    if (!pending.length) return;
    for (const force of [false, true]) {
      const list = pending.filter(account => forced.has(account.id) === force);
      if (!list.length) continue;
      try {
        const reply = await cfg.api('POST', '/api/fleet/bind-many', {primary_ref: String(root), member_refs: list.map(account => String(account.id)), force});
        for (const result of reply.results || []) {
          const account = accounts.get(result.id);
          if (result.bound) {
            bound.add(result.id);
            problems.delete(result.id);
            if (result.notice) notices.set(result.id, result.notice);
            if (account) account.fleet_primary_id = root;
            const rootAccount = accounts.get(root) || (base && base.members.find(member => member.id === root));
            if (rootAccount) rootAccount.fleet_primary_id = root;
          } else {
            problems.set(result.id, {text: result.error || '合并失败', mismatch: /unionid/.test(result.error || '')});
          }
        }
      } catch (error) {
        list.forEach(account => problems.set(account.id, {text: `合并失败：${error.message}`, mismatch: false}));
      }
    }
    renderMembers();
  }

  // The primary first, the rest in channel order.
  function memberRows() {
    const rows = new Map();
    if (base) base.members.forEach(account => rows.set(account.id, {account, existing: true}));
    accounts.forEach(account => rows.set(account.id, {account, existing: rows.get(account.id)?.existing || false}));
    const root = rootId();
    const rank = row => row.account.id === root ? -1 : scanChannelOrder(productOf(row.account));
    return [...rows.values()].sort((a, b) => rank(a) - rank(b) || a.account.id - b.account.id);
  }

  function renderMembers() {
    const box = $('multiMembers');
    if (!box) return;
    box.replaceChildren();
    const rows = active ? memberRows() : [];
    box.hidden = rows.length === 0;
    if (!rows.length) return;
    const root = rootId();
    box.append(node('h3', '', rows.length > 1 ? `多渠道账号 · ${rows.length} 个渠道` : '多渠道账号'));
    for (const {account, existing} of rows) {
      const channel = channelInfo(productOf(account));
      const problem = problems.get(account.id);
      let text = '正在合并…';
      let tone = '';
      const health = account.status === 'alive' ? '' : ` · ${statusText(account.status)}`;
      if (account.id === root) { text = `主账号，脚本只需保留它${health}`; tone = health ? 'bad' : 'ok'; }
      else if (inGroup(account, root) || existing) { text = `已合并${health}`; tone = health ? 'bad' : 'ok'; }
      else if (problem) { text = problem.text; tone = 'bad'; }
      else if (kept.has(account.id)) { text = '已保持独立'; }
      const row = node('div', 'multi-member');
      row.dataset.account = String(account.id);
      const main = node('div');
      main.append(node('b', '', `ID ${account.id} · ${channel.name} · ${labelOf(account)}`), node('small', tone, text));
      const actions = node('div', 'row-actions');
      if (problem && problem.mismatch && !kept.has(account.id) && !inGroup(account, root)) {
        const force = node('button', 'btn', '确认是同一微信，仍然合并');
        force.type = 'button';
        force.addEventListener('click', () => { forced.add(account.id); problems.delete(account.id); renderMembers(); enqueueBind(); });
        const keep = node('button', 'btn', '保持独立');
        keep.type = 'button';
        keep.addEventListener('click', () => { kept.add(account.id); problems.delete(account.id); renderMembers(); });
        actions.append(force, keep);
      }
      row.append(badgeImage(channel), main, actions);
      if (notices.has(account.id)) row.append(node('div', 'multi-member-note', notices.get(account.id)));
      box.append(row);
    }
    if (rows.length === 1) box.append(node('p', 'multi-hint', '再扫其他渠道的二维码，会自动合并到这个账号。'));
  }

  // ----- the page -----

  async function loadBase(ref) {
    const list = await cfg.api('GET', '/accounts?include_members=1');
    const account = list.find(item => String(item.id) === ref || item.openid === ref);
    if (!account) throw new Error(`没有找到账号 ${ref}`);
    const key = account.fleet_primary_id || account.id;
    const members = list.filter(item => (item.fleet_primary_id || item.id) === key);
    members.sort((a, b) => scanChannelOrder(productOf(a)) - scanChannelOrder(productOf(b)) || a.id - b.id);
    base = {members, primary: members.find(item => item.id === key) || members[0]};
  }

  function buildCards() {
    const grid = $('multiGrid');
    scanners.forEach(scanner => scanner.stop());
    scanners.clear();
    grid.replaceChildren();
    const channels = scanChannels();
    for (const channel of channels) {
      const scanner = new Scanner(channel);
      scanners.set(channel.id, scanner);
      grid.append(scanner.card);
      const scannedHere = [...accounts.values()].filter(account => productOf(account) === channel.id).pop();
      const existing = base && base.members.find(member => productOf(member) === channel.id);
      if (scannedHere) scanner.showDone(scannedHere, false);
      else if (existing) scanner.showDone(existing, true);
    }
    const note = $('multiBase');
    if (base) {
      const have = base.members.map(member => channelInfo(productOf(member)).name).join('、');
      const missing = channels.filter(channel => !base.members.some(member => productOf(member) === channel.id)).map(channel => channel.name);
      note.hidden = false;
      note.textContent = `给 ID ${base.primary.id} · ${labelOf(base.primary)} 补扫渠道。已有：${have}。${missing.length ? `待扫：${missing.join('、')}。` : '所有渠道都已绑定，需要时可重新扫码更新。'}`;
    } else {
      note.hidden = true;
    }
  }

  const startPending = () => scanners.forEach(scanner => { if (scanner.kind !== 'done') scanner.start(); });
  const stopAll = () => Promise.all([...scanners.values()].map(scanner => scanner.stop()));

  async function setActive(on) {
    if (on === active) return;
    active = on;
    document.body.classList.toggle('multi-on', on);
    $('multiScanSwitch').checked = on;
    $('multiPane').hidden = !on;
    if (on) {
      await cfg.stopSingle();
      $('authMethodDescription').textContent = '同一个微信依次扫下面的二维码，授权后自动合并成一个多渠道账号';
      buildCards();
      renderMembers();
      if (base) cfg.showAccount(base.primary);
      startPending();
    } else {
      await stopAll();
      scanners.clear();
      $('multiGrid').replaceChildren();
      renderMembers();
      cfg.startSingle();
    }
  }

  // The switch can be clicked before the page has finished starting up; the change
  // waits for init() and then runs in order.
  const toggle = $('multiScanSwitch');
  if (toggle) {
    toggle.addEventListener('change', event => {
      const on = event.target.checked;
      remember(on);
      queue = queue.then(() => setActive(on)).catch(() => {});
    });
  }
  window.addEventListener('pagehide', () => {
    scanners.forEach(scanner => { if (scanner.sid && navigator.sendBeacon) navigator.sendBeacon(`/qr/${scanner.sid}/cancel`); });
  });

  // Starts the page in multi-channel mode or, when it is off, in the single-channel
  // flow, so a single QR is never allocated in vain.
  async function init(options) {
    cfg = options;
    try {
      const params = new URLSearchParams(location.search);
      const ref = params.get('ref');
      let want = params.get('multi') === '1' || remembered() || toggle.checked;
      if (ref) {
        try {
          await loadBase(ref);
          want = true;
          toggle.disabled = true;
          $('multiScanHint').textContent = `正在给 ID ${base.primary.id} 补扫渠道，开关已锁定；新扫到的渠道会合并进这个账号。`;
        } catch (error) {
          $('multiScanHint').textContent = `补扫失败：${error.message}。可以直接新增账号。`;
        }
      }
      if (want) await setActive(true);
      else cfg.startSingle();
    } finally {
      release();
    }
  }

  window.ScanMulti = {
    init,
    active: () => active,
    stopAll,
    restart: () => { if (active) scanners.forEach(scanner => { if (scanner.kind === 'qr') scanner.start(); }); },
    syncRef: () => { const root = rootId(); return root ? String(root) : ''; }
  };
})();
