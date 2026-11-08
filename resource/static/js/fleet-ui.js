// Console side of multi-channel accounts: one card per fleet (a chip per channel and
// expandable member rows) and the dialog that merges separate accounts into one.
// A classic script loaded before the console's inline script: it reads the page's
// globals (state, api, accountName, ...) when it is called, never while it loads.
(() => {
  'use strict';

  const fleetKey = account => account.fleet_primary_id || account.id;
  const productOf = account => account.scan_product || 'appstore';
  const badgeSrc = product => `/static/images/scan-${scanChannelBadge(product)}.png?v=093`;
  const isFleet = group => group.members.length > 1;
  const idList = ids => ids.join('、');

  // Accounts that share a fleet become one group; everything else is a group of one.
  // The primary comes first in `primary`, the members are in channel order.
  function groups(accounts) {
    const byKey = new Map();
    for (const account of accounts) {
      const key = fleetKey(account);
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key).push(account);
    }
    return [...byKey].map(([key, members]) => {
      members.sort((a, b) => scanChannelOrder(productOf(a)) - scanChannelOrder(productOf(b)) || a.id - b.id);
      return {key, primary: members.find(item => item.id === key) || members[0], members};
    });
  }

  function health(account) {
    const expired = account.status === 'expired';
    const tone = expired ? 'expired' : account.rescan_recommended ? 'warn' : account.status === 'alive' ? 'alive' : 'unknown';
    const text = expired ? '已失效' : account.status === 'session_only' ? '需补授权' : account.rescan_recommended ? '建议重扫' : statusText(account.status);
    return {tone, text};
  }

  function summary(group) {
    const total = group.members.length;
    const usable = group.members.filter(member => member.status === 'alive').length;
    const tones = group.members.map(member => health(member).tone);
    if (tones.every(tone => tone === 'alive')) return {cls: 'alive', text: `${total} 个渠道可用`};
    if (group.members.every(member => member.status === 'expired')) return {cls: 'expired', text: '全部失效'};
    return {cls: 'warning', text: `${usable}/${total} 可用`};
  }

  const chipClass = tone => tone === 'alive' ? 'alive' : tone === 'expired' ? 'expired' : 'warn';

  function chipsMarkup(group) {
    const present = new Set(group.members.map(productOf));
    const lit = group.members.map(member => {
      const {tone, text} = health(member);
      const label = `${scanChannelName(productOf(member))} · ID ${member.id} · ${text}`;
      return `<span class="channel-chip ${chipClass(tone)}" title="${escapeHTML(label)}" aria-label="${escapeHTML(label)}"><img alt="" src="${badgeSrc(productOf(member))}"></span>`;
    });
    // A channel the registry lists but the group lacks is one click from being added.
    const missing = scanChannels().filter(channel => !present.has(channel.id)).map(channel =>
      `<a class="channel-chip missing" href="/scan?ref=${encodeURIComponent(group.primary.id)}" title="${escapeHTML(channel.name)} 未绑定，点击补扫" aria-label="补扫 ${escapeHTML(channel.name)} 渠道">+</a>`);
    return `<span class="channel-strip">${lit.concat(missing).join('')}</span>`;
  }

  function attention(group) {
    const notes = group.members.map(member => ({member, ...health(member)})).filter(item => item.tone !== 'alive')
      .map(item => `${scanChannelName(productOf(item.member))} ${item.text}`);
    return notes.length ? `<span class="fleet-note">${escapeHTML(notes.join('、'))}</span>` : '';
  }

  function memberMarkup(group, member) {
    const {tone, text} = health(member);
    const isPrimary = member.id === group.primary.id;
    const pending = member.status !== 'alive' && member.status !== 'expired' && member.status !== 'session_only';
    const channel = scanChannelName(productOf(member));
    return `<div class="fleet-member" data-openid="${escapeHTML(member.openid)}" data-account="${member.id}">
      <img alt="" src="${badgeSrc(productOf(member))}">
      <span class="fleet-member-main">
        <button type="button" class="fleet-member-select" aria-label="选择账号 ${member.id}，${escapeHTML(channel)}">ID ${member.id} · ${escapeHTML(channel)}${isPrimary ? ' · 主账号' : ''}</button>
        <small class="state-${chipClass(tone)}">${escapeHTML(text)}</small>
      </span>
      <span class="fleet-member-actions">
        ${pending ? '<button type="button" class="btn small" data-confirm-status="alive">仍有效</button><button type="button" class="btn small danger" data-confirm-status="expired">已失效</button>' : ''}
        <button type="button" class="btn secondary" data-update-account aria-label="更新 ID ${member.id} 的扫码">更新</button>
        <a class="btn" href="/proxies?ref=${encodeURIComponent(member.id)}" aria-label="管理 ID ${member.id} 的代理">代理</a>
        ${isPrimary ? '' : `<button type="button" class="btn secondary" data-leave-fleet aria-label="把 ID ${member.id} 移出多渠道账号">移出</button>`}
      </span>
      ${lifecycleMarkup(member)}
    </div>`;
  }

  async function dissolve(group) {
    const ids = idList(group.members.map(member => member.id));
    if (!confirm(`确认拆分多渠道账号 ID ${group.primary.id}？\n\nID ${ids} 会恢复成各自独立的账号，不会删除任何账号。\n面板里如果只保留了主账号 ID ${group.primary.id}，其他 ID 需要自行加回。`)) return;
    try {
      await api('POST', '/api/fleet/dissolve', {ref: String(group.primary.id)});
      await loadAccounts({background: true});
      setActivity(`已拆分多渠道账号 ID ${group.primary.id}`);
    } catch (error) {
      setActivity(`拆分失败：${error.message}`);
    }
  }

  async function leave(group, member) {
    // A fleet needs two accounts: taking the second-to-last one out ends it.
    if (group.members.length <= 2) return dissolve(group);
    if (!confirm(`确认把 ID ${member.id}（${scanChannelName(productOf(member))}）移出多渠道账号 ID ${group.primary.id}？\n\n它会恢复成独立账号，不会被删除。`)) return;
    try {
      await api('DELETE', '/api/fleet', {ref: String(member.id)});
      await loadAccounts({background: true});
      setActivity(`ID ${member.id} 已移出多渠道账号`);
    } catch (error) {
      setActivity(`移出失败：${error.message}`);
    }
  }

  // The card of one fleet. It is built like a single account's card, so selection,
  // the mobile dock and the clean skin treat it the same way.
  function card(group, previouslyExpired) {
    const {primary, members, key} = group;
    const name = accountName(primary);
    const sum = summary(group);
    state.expandedFleets ??= new Set();
    state.autoOpenedFleets ??= new Set();
    const memberSelected = members.some(member => member.openid === state.selectedOpenID) && primary.openid !== state.selectedOpenID;
    if (memberSelected && !state.autoOpenedFleets.has(key)) {
      state.autoOpenedFleets.add(key);
      state.expandedFleets.add(key);
    }
    const expanded = state.expandedFleets.has(key);
    const justExpired = sum.cls === 'expired' && previouslyExpired !== null && members.some(member => !previouslyExpired.has(member.id));
    const element = document.createElement('article');
    element.className = `account-card fleet ${sum.cls}${justExpired ? ' expired-alert' : ''}`;
    element.dataset.openid = primary.openid;
    element.dataset.memberOpenids = JSON.stringify(members.map(member => member.openid));
    element.dataset.fleet = String(key);
    if (sum.cls === 'expired') element.dataset.stateLabel = '已失效';
    const proxy = proxyText(state.proxyConfigs.get(String(primary.id)));
    const others = members.filter(member => member.id !== primary.id).map(member => member.id);
    const panelId = `fleet-members-${key}`;
    element.innerHTML = `
      <span class="avatar">
        <img alt="" src="/accounts/avatar?ref=${encodeURIComponent(primary.openid)}">
        <span class="avatar-letter hidden">${escapeHTML(name.slice(0, 1).toUpperCase() || 'Y')}</span>
      </span>
      <span class="account-main">
        <span class="account-header">
          <button type="button" class="account-name account-select" aria-pressed="false" aria-label="选择多渠道账号 ${primary.id}，${escapeHTML(name)}，${members.length} 个渠道，${sum.text}">${escapeHTML(name)}</button>
          <span class="account-header-tools"><span class="account-status ${sum.cls}"><span class="status-dot"></span>${escapeHTML(sum.text)}</span></span>
        </span>
        ${chipsMarkup(group)}
        <span class="account-meta" title="主账号 ID ${primary.id}，成员 ${idList(others)}">ID ${primary.id}（主）· ${idList(others)} · ${escapeHTML(proxy)}</span>
        ${attention(group)}
        <button type="button" class="fleet-expand" aria-expanded="${expanded}" aria-controls="${panelId}">渠道详情<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg></button>
      </span>
      <span class="account-row-actions"><a class="btn secondary" href="/scan?ref=${encodeURIComponent(primary.id)}" aria-label="给 ${escapeHTML(name)} 补扫或重扫渠道">多渠道扫码</a><a class="btn" href="/proxies?ref=${encodeURIComponent(primary.id)}" aria-label="管理 ${escapeHTML(name)} 的代理">代理</a></span>
      <div class="fleet-members" id="${panelId}"${expanded ? '' : ' hidden'}>
        ${members.map(member => memberMarkup(group, member)).join('')}
        <div class="fleet-foot"><button type="button" class="btn secondary" data-dissolve-fleet>拆分多渠道账号</button></div>
      </div>`;
    const avatar = element.querySelector('.avatar img');
    avatar.addEventListener('error', () => {
      avatar.classList.add('hidden');
      element.querySelector('.avatar-letter').classList.remove('hidden');
    });
    element.addEventListener('click', () => selectAccount(primary));
    for (const link of element.querySelectorAll('.account-row-actions a, .channel-chip.missing')) link.addEventListener('click', event => event.stopPropagation());
    const toggle = element.querySelector('.fleet-expand');
    toggle.addEventListener('click', event => {
      event.stopPropagation();
      const open = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(open));
      element.querySelector('.fleet-members').hidden = !open;
      if (open) state.expandedFleets.add(key);
      else state.expandedFleets.delete(key);
    });
    element.querySelector('[data-dissolve-fleet]').addEventListener('click', event => { event.stopPropagation(); dissolve(group); });
    for (const row of element.querySelectorAll('.fleet-member')) {
      const member = members.find(item => String(item.id) === row.dataset.account);
      row.addEventListener('click', event => { event.stopPropagation(); selectAccount(member); });
      row.querySelector('[data-update-account]').addEventListener('click', event => {
        event.stopPropagation();
        selectAccount(member);
        openAccountLinkDialog('update');
      });
      row.querySelector('.fleet-member-actions a').addEventListener('click', event => event.stopPropagation());
      row.querySelector('[data-leave-fleet]')?.addEventListener('click', event => { event.stopPropagation(); leave(group, member); });
      row.querySelectorAll('[data-confirm-status]').forEach(action => action.addEventListener('click', event => {
        event.stopPropagation();
        setAccountStatus(member, action.dataset.confirmStatus, action);
      }));
    }
    return element;
  }

  // ----- merge existing accounts into one multi-channel account -----

  let mergeBusy = false;

  const node = (tag, className, content) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = content;
    return element;
  };

  function primaryGroup() {
    const id = Number($('mergeChannelsPrimary').value);
    return groups(state.accounts).find(group => group.primary.id === id);
  }

  // Only independent accounts can be merged in: the server refuses members of
  // another fleet, and two fleets are not joined.
  function renderCandidates() {
    const target = primaryGroup();
    const list = $('mergeChannelsList');
    list.replaceChildren();
    $('mergeChannelsResults').replaceChildren();
    if (!target) return;
    const taken = new Set(target.members.map(productOf));
    const suggested = account => Boolean(account.nickname) && account.nickname === target.primary.nickname && !taken.has(productOf(account));
    const candidates = groups(state.accounts).filter(group => !isFleet(group) && group.primary.id !== target.primary.id).map(group => group.primary)
      .sort((a, b) => Number(suggested(b)) - Number(suggested(a)) || a.id - b.id);
    for (const account of candidates) {
      const channel = scanChannelName(productOf(account));
      const label = node('label', 'fleet-pick');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = String(account.id);
      input.checked = suggested(account);
      const image = document.createElement('img');
      image.alt = '';
      image.src = badgeSrc(productOf(account));
      const text = node('span');
      const note = suggested(account) ? ' · 昵称相同、渠道不同，已勾选' : taken.has(productOf(account)) ? ' · 主账号已有这个渠道' : '';
      text.append(node('b', '', `ID ${account.id} · ${channel} · ${accountName(account)}`), node('small', '', `${health(account).text}${note}`));
      label.append(input, image, text);
      list.append(label);
    }
    if (!candidates.length) list.append(node('p', 'link-dialog-footnote', '没有可合并的独立账号。已在多渠道账号里的账号需要先拆分或移出。'));
    $('mergeChannelsConfirmBtn').disabled = !candidates.length;
  }

  function openMergeDialog() {
    const all = groups(state.accounts);
    if (all.length < 2) return;
    const selected = getSelectedAccount();
    const select = $('mergeChannelsPrimary');
    select.innerHTML = all.map(group => `<option value="${group.primary.id}">ID ${group.primary.id} · ${escapeHTML(accountName(group.primary))} · ${escapeHTML(group.members.map(member => scanChannelName(productOf(member))).join(' + '))}</option>`).join('');
    const pick = (selected && all.find(group => group.key === fleetKey(selected))) || all.find(isFleet) || all[0];
    select.value = String(pick.primary.id);
    $('mergeChannelsConfirmBtn').textContent = '合并';
    $('mergeChannelsConfirmBtn').dataset.done = '';
    renderCandidates();
    $('mergeChannelsDialog').showModal();
  }

  function showResults(reply, primary) {
    const box = $('mergeChannelsResults');
    box.replaceChildren();
    for (const result of reply.results || []) {
      const account = state.accounts.find(item => item.id === result.id);
      const label = account ? `ID ${account.id} · ${scanChannelName(productOf(account))}` : `ID ${result.id}`;
      const line = node('div', result.bound ? 'ok' : 'bad', `${label}：${result.bound ? '已合并' : result.error || '合并失败'}${result.notice ? `（${result.notice}）` : ''}`);
      if (!result.bound && /unionid/.test(result.error || '')) {
        const force = node('button', 'btn secondary', '确认是同一微信，仍然合并');
        force.type = 'button';
        force.addEventListener('click', () => mergeSelected([String(result.id)]));
        line.append(' ', force);
      }
      box.append(line);
    }
    if ((reply.results || []).some(result => result.bound)) {
      const bound = reply.results.filter(result => result.bound).map(result => result.id);
      box.append(node('p', 'link-dialog-footnote', `合并完成。面板（YYB_SERVER）只需要保留主账号 ID ${primary}；成员 ID ${idList(bound)} 如果之前加进过面板，可以手动移除，否则脚本会按每个 ID 各跑一遍。`));
    }
  }

  async function mergeSelected(forceIds) {
    if (mergeBusy) return;
    const primary = $('mergeChannelsPrimary').value;
    const ids = forceIds || [...$('mergeChannelsList').querySelectorAll('input:checked')].map(input => input.value);
    if (!ids.length) {
      $('mergeChannelsResults').replaceChildren(node('div', 'bad', '请至少勾选一个要并入的账号。'));
      return;
    }
    mergeBusy = true;
    $('mergeChannelsConfirmBtn').disabled = true;
    $('mergeChannelsPrimary').disabled = true;
    $('mergeChannelsResults').replaceChildren(node('div', '', '正在核对微信身份并合并…'));
    try {
      const reply = await api('POST', '/api/fleet/bind-many', {primary_ref: primary, member_refs: ids, force: Boolean(forceIds)});
      showResults(reply, primary);
      if ((reply.results || []).some(result => result.bound)) {
        await loadAccounts({background: true});
        setActivity(`已合并到多渠道账号 ID ${primary}`);
        const button = $('mergeChannelsConfirmBtn');
        button.textContent = '完成';
        button.dataset.done = '1';
      }
    } catch (error) {
      $('mergeChannelsResults').replaceChildren(node('div', 'bad', error.message));
    } finally {
      mergeBusy = false;
      $('mergeChannelsConfirmBtn').disabled = false;
      $('mergeChannelsPrimary').disabled = false;
    }
  }

  function bindMergeDialog() {
    $('mergeChannelsBtn').addEventListener('click', openMergeDialog);
    $('mergeChannelsPrimary').addEventListener('change', renderCandidates);
    $('mergeChannelsClose').addEventListener('click', () => { if (!mergeBusy) $('mergeChannelsDialog').close(); });
    $('mergeChannelsCancelBtn').addEventListener('click', () => { if (!mergeBusy) $('mergeChannelsDialog').close(); });
    $('mergeChannelsDialog').addEventListener('cancel', event => { if (mergeBusy) event.preventDefault(); });
    $('mergeChannelsConfirmBtn').addEventListener('click', event => {
      if (event.currentTarget.dataset.done === '1') $('mergeChannelsDialog').close();
      else mergeSelected();
    });
  }

  // The channel select of the update-link dialog follows the registry.
  function fillLinkProducts() {
    const select = $('accountLinkProduct');
    const current = select.value;
    select.replaceChildren(...scanChannels().map(channel => {
      const option = document.createElement('option');
      option.value = channel.id;
      option.textContent = channel.name;
      return option;
    }));
    if ([...select.options].some(option => option.value === current)) select.value = current;
  }

  window.FleetUI = {groups, card, isFleet, fleetKey, bindMergeDialog, fillLinkProducts, scanHref: account => `/scan?ref=${encodeURIComponent(fleetKey(account))}`};
})();
