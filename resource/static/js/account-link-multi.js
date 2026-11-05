// Token-scoped endpoints only. Public links cannot invoke console fleet APIs.
window.AccountLinkMulti = (() => {
  let closed = false, finishing = false, api, setState, onFinish, cards = [], completed = new Map();
  const $ = id => document.getElementById(id);
  const element = (tag, text, cls) => { const el = document.createElement(tag); if (text) el.textContent = text; if (cls) el.className = cls; return el; };
  function stop() {
    closed = true;
    for (const card of cards) { clearTimeout(card.timer); card.controller?.abort(); card.button.disabled = true; if (!card.saved) card.qr.textContent = '授权已结束'; }
    if ($('linkFinish')) $('linkFinish').disabled = true;
    if ($('linkMultiToggle')) $('linkMultiToggle').disabled = true;
    if ($('linkSingleProduct')) $('linkSingleProduct').disabled = true;
  }
  async function cancel(card) {
    ++card.generation; clearTimeout(card.timer); card.controller?.abort();
    const sid = card.sid; card.sid = '';
    if (sid && !card.saved) await api(`/qr/${encodeURIComponent(sid)}/cancel`, {method:'POST',body:'{}'}).catch(() => {});
  }
  function progress() {
    $('linkProgress').textContent = `已保存 ${completed.size} 个渠道，可只授权需要的渠道。`;
    $('linkFinish').disabled = closed || finishing || !completed.size || cards.some(c => c.saving);
    $('linkMultiToggle').disabled = closed || finishing || cards.some(c => c.saving);
    $('linkSingleProduct').disabled = closed || finishing || cards.some(c => c.saving);
  }
  function message(card, text, kind = '') { card.state.textContent = text; card.state.className = `state ${kind}`; }
  async function create(card) {
    if (closed || finishing || card.saving || card.saved) return;
    await cancel(card);
    if (closed || finishing) return;
    const generation = card.generation;
    card.controller = new AbortController(); card.button.disabled = true; card.qr.textContent = '正在生成二维码…'; message(card, '请使用原微信扫码'); card.attempts = 0;
    try {
      const result = await api('/qr', {method:'POST',body:JSON.stringify({product:card.product}),signal:card.controller.signal});
      if (closed || finishing || generation !== card.generation) {
        api(`/qr/${encodeURIComponent(result.session_id)}/cancel`, {method:'POST',body:'{}'}).catch(() => {}); return;
      }
      card.sid = result.session_id;
      const img = element('img'); img.alt = `${card.name} 现场扫码二维码`; img.draggable = false; img.src = result.image_url;
      card.qr.replaceChildren(img); card.root.dataset.state = 'waiting'; card.button.disabled = false;
      poll(card, generation);
    } catch (error) {
      if (closed || generation !== card.generation || error.name === 'AbortError') return;
      card.qr.textContent = '二维码生成失败'; message(card, error.message, 'error'); card.button.disabled = false;
      if (error.status === 410) expire(error.message);
    }
  }
  async function poll(card, generation) {
    if (closed || finishing || generation !== card.generation || !card.sid) return;
    let retry = true;
    try {
      const result = await api(`/qr/${encodeURIComponent(card.sid)}/poll`, {signal:card.controller.signal});
      if (closed || finishing || generation !== card.generation) return;
      if (['authorized','confirmed'].includes(result.status)) {
        card.saving = true; card.button.disabled = true; progress(); message(card, '正在核验微信身份并保存');
        if (++card.attempts > 12) throw new Error('保存超时，请重新生成二维码');
        const account = await api(`/qr/${encodeURIComponent(card.sid)}/confirm`, {method:'POST',body:'{}',signal:card.controller.signal});
        if (closed || generation !== card.generation) return;
        completed.set(card.product, account.id); card.saved = true; card.root.dataset.state = 'done';
        card.qr.textContent = '✓ 已保存'; message(card, `账号 ID ${account.id}`, 'ok'); card.button.hidden = true; retry = false;
      } else if (result.status === 'scanned') message(card, '已扫码，请在手机上确认');
      else if (result.status === 'pending') message(card, '等待扫码');
      else { message(card, '二维码已过期，可重新生成', 'error'); card.qr.textContent = '二维码不可用'; retry = false; }
    } catch (error) {
      if (closed || generation !== card.generation || error.name === 'AbortError') return;
      message(card, error.message, 'error');
      retry = (error.status === 409 && error.message.startsWith('buffer not ready') || error.status === 502) && card.attempts < 12;
      // A lost confirmation response is safe to retry: the server caches its result.
      if (!error.status && card.attempts > 0 && card.attempts < 12) retry = true;
      if (error.status === 410) expire(error.message);
    } finally {
      card.saving = false; card.button.disabled = closed || card.saved; progress();
      if (retry && !closed && !finishing && generation === card.generation) card.timer = setTimeout(() => poll(card, generation), 1500);
    }
  }
  function expire(text) { stop(); setState(text || '授权链接已失效，请重新生成', 'error'); }
  async function render() {
    if (cards.some(c => c.saving)) return;
    await Promise.all(cards.map(cancel));
    if (closed) return;
    const multi = $('linkMultiToggle').checked;
    $('linkSingleProduct').hidden = multi;
    const channels = scanChannels().filter(c => multi || c.id === $('linkSingleProduct').value);
    cards = channels.map(channel => {
      const root = element('article', '', 'link-channel'); root.dataset.product = channel.id;
      const title = element('h2', channel.name), description = element('p', channel.description);
      const qr = element('div', '', 'qr'), state = element('div', '', 'state'), button = element('button', '重新生成二维码', 'btn'); button.type = 'button';
      root.append(title,description,qr,state,button);
      const card = {root,qr,state,button,product:channel.id,name:channel.name,generation:0,sid:'',saved:completed.has(channel.id)};
      button.onclick = () => create(card);
      if (card.saved) { root.dataset.state = 'done'; qr.textContent = '✓ 已保存'; message(card, `账号 ID ${completed.get(channel.id)}`, 'ok'); button.hidden = true; }
      return card;
    });
    $('linkChannelGrid').replaceChildren(...cards.map(c => c.root)); progress();
    await Promise.all(cards.filter(c => !c.saved).map(create));
  }
  function init(options) {
    ({api,setState,onFinish} = options); const meta = options.meta;
    completed = new Map((meta.completed_channels || []).map(c => [c.product,c.account_id])); closed = false;
    document.querySelector('.panel').classList.add('multi-link');
    document.querySelector('.scan-channel').classList.add('hidden'); $('qr').classList.add('hidden'); $('retry').classList.add('hidden');
    $('intro').textContent = meta.kind === 'update' ? `为${meta.target || '账号 ID '+meta.account_id}更新或补充渠道，保留原 ID。` : '首次扫码建立新账号，其余渠道须使用同一微信。';
    $('linkHint').textContent = '每个渠道保存后立即生效。核验失败不会修改该渠道；已保存的授权会保留。点击“完成授权”关闭链接，未扫渠道无需补齐。';
    const host = $('linkMulti'); host.classList.remove('hidden');
    const toolbar = element('div', '', 'link-multi-toolbar'), label = element('label');
    const toggle = element('input'); toggle.type = 'checkbox'; toggle.id = 'linkMultiToggle'; toggle.checked = true; label.append(toggle, document.createTextNode('同时显示多渠道二维码'));
    const select = element('select'); select.id = 'linkSingleProduct'; select.setAttribute('aria-label','选择扫码渠道'); select.hidden = true;
    for (const channel of scanChannels()) { const opt = element('option', channel.name); opt.value = channel.id; select.append(opt); }
    select.value = meta.product; toolbar.append(label,select);
    const grid = element('div', '', 'link-multi-grid'); grid.id = 'linkChannelGrid';
    const footer = element('div', '', 'link-multi-footer'), count = element('span'); count.id = 'linkProgress'; count.setAttribute('role','status');
    const finish = element('button','完成授权','btn'); finish.id = 'linkFinish'; finish.type = 'button';
    finish.onclick = async () => {
      finishing = true; progress(); toggle.disabled = true; select.disabled = true;
      try { await api('/finish',{method:'POST',body:'{}'}); stop(); onFinish(); setState('授权完成，链接已关闭，请关闭此标签页','ok'); }
      catch (error) { setState(error.message,'error'); if (error.status === 410) stop(); }
      finally { finishing = false; toggle.disabled = closed; select.disabled = closed; progress(); if (!closed) for (const card of cards) if (card.sid && !card.saved) {clearTimeout(card.timer); card.timer = setTimeout(() => poll(card,card.generation),1500);} }
    };
    footer.append(count,finish); host.append(toolbar,grid,footer);
    const change = async () => { toggle.disabled = true; select.disabled = true; try { await render(); } finally { toggle.disabled = closed; select.disabled = closed; } };
    toggle.onchange = change; select.onchange = change;
    setState('请逐个确认各渠道授权，完成后关闭链接'); render();
    window.addEventListener('pagehide', stop, {once:true});
  }
  return {init,stop};
})();
