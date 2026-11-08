// Run against an isolated test service that holds the accounts of tests/seed-multi-channel-ui.py:
// Boom (three channels, already one fleet), Alice (three independent accounts), Carol
// (another WeChat) and Dave (a pair without unionid). Every QR response below is fake; the
// account bodies and the fleet answers come from the service itself.
// Optional: YYB_TEST_SHOTS=<dir> saves screenshots.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const base = process.env.YYB_TEST_URL;
if (!base) throw new Error('Set YYB_TEST_URL to an isolated service');
const shots = process.env.YYB_TEST_SHOTS || '';
const json = data => ({status: 200, contentType: 'application/json', body: JSON.stringify({code: 0, msg: 'success', data})});
const qrImage = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="280" height="280"><rect width="280" height="280" fill="%23eeeeee"/><text x="40" y="150" font-size="30">FAKE QR</text></svg>';
const OPENIDS = {
  boom: ['oBoomAppStore000000000000001', 'pcmgr:boom00000000000000000002', 'oBoomGameAssist00000000000003'],
  alice: ['oAliceAppStore0000000000000004', 'pcmgr:alice0000000000000000005', 'oAliceGameAssist000000000000006'],
  carol: ['oCarolAppStore0000000000000007'],
  dave: ['oDaveAppStore00000000000000008', 'pcmgr:dave00000000000000000009']
};

async function fresh(browser) {
  const context = await browser.newContext({viewport: {width: 1280, height: 1000}});
  const login = await context.request.post(base + '/login', {data: {username: process.env.YYB_TEST_USER, password: process.env.YYB_TEST_PASS}});
  assert.equal((await login.json()).code, 0);
  // These legacy grouping fixtures cover three providers; registry expansion
  // and QQ/LD identity consent are covered by account-link-multi-ui.cjs.
  await context.route('**/api/scan-channels', async route => {
    const data = await (await context.request.get(base+'/api/scan-channels')).json();
    data.data.channels = data.data.channels.filter(channel => ['appstore','pcmgr','gameassist'].includes(channel.id));
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  return {context, page, errors};
}

const accountsOf = async context => (await (await context.request.get(base + '/accounts?include_members=1')).json()).data;
const byId = (list, id) => list.find(account => account.id === id);

// The seeded account ids by name, found through their openid.
async function seeded(context) {
  const all = await accountsOf(context);
  return Object.fromEntries(Object.entries(OPENIDS).map(([name, openids]) => [name, openids.map(openid => {
    const account = all.find(item => item.openid === openid);
    assert(account, 'seed account missing: ' + openid + ' (run tests/seed-multi-channel-ui.py)');
    return account.id;
  })]));
}

// Fake QR service: a session per product that answers what the test sets. The accounts
// it confirms are the service's own, so binding them is real.
async function mockQR(context, accountsByProduct) {
  const state = {seq: 0, sessions: new Map(), requested: [], urls: [], cancelled: [], bound: [], sync: []};
  const sessionOf = url => state.sessions.get(url.split('/qr/')[1].split('/')[0]);
  await context.route('**/qr?as_base64=true*', route => {
    state.urls.push(route.request().url());
    const body = route.request().postDataJSON();
    const id = 'fake-' + (++state.seq);
    state.sessions.set(id, {id, product: body.product, status: 'pending', live: true});
    state.requested.push(body.product);
    return route.fulfill(json({session_id: id, product: body.product, image_base64: qrImage}));
  });
  await context.route('**/qr/*/cancel', route => {
    const session = sessionOf(route.request().url());
    if (session) session.live = false;
    state.cancelled.push(route.request().url());
    return route.fulfill(json({cancelled: true}));
  });
  await context.route('**/qr/*/poll', route => route.fulfill(json({status: sessionOf(route.request().url())?.status || 'unknown'})));
  await context.route('**/qr/*/confirm', route => {
    const session = sessionOf(route.request().url());
    session.live = false;   // the service drops a session once it is confirmed
    return route.fulfill(json(accountsByProduct[session.product]));
  });
  await context.route('**/api/qinglong/sync', route => {
    state.sync.push(route.request().postDataJSON());
    return route.fulfill(json({added: true}));
  });
  context.on('request', request => {
    if (request.url().includes('/api/fleet/bind-many')) state.bound.push(request.postDataJSON());
  });
  // The test plays the phone: the live session of a product gets authorized.
  state.scan = product => {
    const live = [...state.sessions.values()].filter(session => session.product === product && session.live);
    assert(live.length, 'no live QR session for ' + product);
    live.at(-1).status = 'authorized';
  };
  return state;
}

async function withAccounts(context, pairs) {
  const all = await accountsOf(context);
  return Object.fromEntries(pairs.map(([product, id]) => [product, byId(all, id)]));
}

const dissolve = (context, ref) => context.request.post(base + '/api/fleet/dissolve', {data: {ref: String(ref)}});
const shot = async (page, name) => { if (shots) { fs.mkdirSync(shots, {recursive: true}); await page.screenshot({path: path.join(shots, name + '.png'), fullPage: true}); } };
const done = (page, product) => page.waitForSelector(`.multi-card[data-product="${product}"][data-state="done"]`, {timeout: 15000});
const waiting = (page, count) => page.waitForFunction(n => document.querySelectorAll('.multi-card[data-state="waiting"]').length === n, count);
const noOverflow = async (page, label) => {
  for (const width of [1440, 768, 390, 320]) {
    await page.setViewportSize({width, height: 1000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `${label} overflows at ${width}`);
  }
  await page.setViewportSize({width: 1280, height: 1000});
};
const member = (page, id) => page.locator(`.multi-member[data-account="${id}"]`);

// Alice, Carol and Dave start (and end) the run as independent accounts, Boom as one fleet.
async function resetFleets(browser) {
  const {context} = await fresh(browser);
  const S = await seeded(context);
  for (const id of [...S.alice, ...S.carol, ...S.dave]) await dissolve(context, id);
  const [primary, ...members] = S.boom;
  await context.request.post(base + '/api/fleet/bind-many', {data: {primary_ref: String(primary), member_refs: members.map(String), force: true}});
  await context.close();
}

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    await resetFleets(browser);
    // The channel list the scan page uses is the server's list; a new account is built
    // from one QR per channel, the first scan becoming the primary.
    {
      const {context, page, errors} = await fresh(browser);
      const S = await seeded(context);
      const [appstore, pcmgr, gameassist] = S.alice;
      const channels = (await (await context.request.get(base + '/api/scan-channels')).json()).data.channels.filter(c => ['appstore','pcmgr','gameassist'].includes(c.id));
      assert(channels.length >= 3);
      const ids = channels.map(channel => channel.id);
      const qr = await mockQR(context, await withAccounts(context, [['appstore', appstore], ['pcmgr', pcmgr], ['gameassist', gameassist]]));

      await page.goto(base + '/scan');
      await page.waitForFunction(() => document.querySelector('#qrWrap img'));
      assert.equal(await page.locator('#scanProduct option').count(), channels.length, 'single-channel select follows the registry');
      assert.equal(await page.locator('#multiScanSwitch').isChecked(), false);
      assert.equal(await page.locator('#multiPane').isVisible(), false);

      // Switch on: one QR per channel, the single QR is retired.
      await page.locator('.multi-switch').click();
      await waiting(page, ids.length);
      assert.deepEqual(qr.requested.slice(1).sort(), [...ids].sort());
      assert(!qr.urls[0].includes('panel_sync'), 'the single flow syncs the panel at once');
      assert(qr.urls.slice(1).every(url => url.includes('panel_sync=defer')), 'a multi-channel scan syncs the panel after the merge');
      assert(qr.cancelled.some(url => url.includes('/fake-1/')), 'the single QR must be cancelled');
      assert.equal(await page.locator('#qrPane').isVisible(), false);
      assert.equal(await page.locator('#modeSwitch').isVisible(), false);
      assert.equal(await page.locator('#scanProductField').isVisible(), false);
      assert.equal(await page.locator('.multi-card').count(), ids.length);
      await shot(page, 'scan-multi-waiting');
      await noOverflow(page, 'multi scan page');

      // The first scan becomes the primary account.
      qr.scan('pcmgr');
      await done(page, 'pcmgr');
      await page.waitForSelector(`.multi-member[data-account="${pcmgr}"] small.ok`);
      assert.match(await member(page, pcmgr).locator('small').innerText(), /主账号/);
      assert.equal(await page.locator('#accountId').innerText(), String(pcmgr));
      // The others join it as they are scanned.
      qr.scan('appstore');
      qr.scan('gameassist');
      await done(page, 'appstore');
      await done(page, 'gameassist');
      await page.waitForFunction(() => [...document.querySelectorAll('.multi-member small')].filter(item => /已合并/.test(item.textContent)).length === 2);
      assert.equal(qr.bound.at(-1).primary_ref, String(pcmgr));
      assert.equal(await page.locator('.multi-member').count(), 3);
      await shot(page, 'scan-multi-merged');
      const merged = await accountsOf(context);
      for (const id of S.alice) assert.equal(byId(merged, id).fleet_primary_id, pcmgr, `account ${id} is in the fleet of ${pcmgr}`);

      // The panel gets the primary only.
      await page.click('#syncBtn');
      await page.waitForFunction(() => /已添加到面板/.test(document.getElementById('actionMessage').textContent));
      assert.deepEqual(qr.sync.at(-1), {ref: String(pcmgr)});

      // Switching off returns to the single flow, with a new QR of its own.
      const before = qr.requested.length;
      await page.locator('.multi-switch').click();
      await page.waitForFunction(() => !document.getElementById('multiPane').offsetParent);
      await page.waitForFunction(() => document.querySelector('#qrWrap img'));
      assert.equal(qr.requested.length, before + 1);
      assert.equal(await page.locator('#qrPane').isVisible(), true);
      assert.equal(await page.locator('.multi-card').count(), 0);
      assert.deepEqual(errors, []);
      await dissolve(context, pcmgr);
      await context.close();
    }

    // A click on the switch while the page is still starting up is not lost, and
    // the single flow never allocates a QR for it.
    {
      const {context, page, errors} = await fresh(browser);
      const qr = await mockQR(context, {});
      await context.route('**/api/scan-settings', async route => {
        await new Promise(resolve => setTimeout(resolve, 1500));
        return route.continue();
      });
      await page.goto(base + '/scan', {waitUntil: 'domcontentloaded'});
      await page.locator('.multi-switch').click();
      await waiting(page, 3);
      assert.deepEqual([...qr.requested].sort(), ['appstore', 'gameassist', 'pcmgr']);
      assert.equal(await page.locator('#qrPane').isVisible(), false);
      assert.deepEqual(errors, []);
      await context.close();
    }

    // The switch is remembered and a QR is never wasted on the single flow when it is on.
    {
      const {context, page, errors} = await fresh(browser);
      const qr = await mockQR(context, {});
      await page.goto(base + '/scan');
      await page.waitForFunction(() => document.querySelector('#qrWrap img'));
      await page.locator('.multi-switch').click();
      await waiting(page, 3);
      qr.requested.length = 0;
      await page.reload();
      await waiting(page, 3);
      assert.equal(await page.locator('#multiScanSwitch').isChecked(), true);
      assert.equal(qr.requested.length, 3, 'one QR per channel and none for the single flow');
      await page.goto(base + '/scan?multi=1');
      await waiting(page, 3);
      assert.deepEqual(errors, []);
      await context.close();
    }

    // Another WeChat is held back until the operator decides.
    {
      const {context, page, errors} = await fresh(browser);
      const S = await seeded(context);
      const alice = S.alice[0], carol = S.carol[0];
      const qr = await mockQR(context, await withAccounts(context, [['appstore', alice], ['pcmgr', carol]]));
      await page.goto(base + '/scan?multi=1');
      await waiting(page, 3);
      qr.scan('appstore');
      await done(page, 'appstore');
      qr.scan('pcmgr');
      await done(page, 'pcmgr');
      await page.waitForSelector(`.multi-member[data-account="${carol}"] .btn`);
      assert.match(await member(page, carol).locator('small').innerText(), /unionid/);
      await shot(page, 'scan-multi-mismatch');
      assert.equal(byId(await accountsOf(context), carol).fleet_primary_id ?? null, null, 'a different WeChat must stay independent');
      await member(page, carol).locator('.btn', {hasText: '保持独立'}).click();
      assert.match(await member(page, carol).locator('small').innerText(), /已保持独立/);
      assert.equal(await member(page, carol).locator('.btn').count(), 0);
      // Scanning the channel again asks again; an operator who knows better can force it.
      await page.locator('.multi-card[data-product="pcmgr"] .btn').click();
      await page.waitForSelector('.multi-card[data-product="pcmgr"][data-state="waiting"]');
      qr.scan('pcmgr');
      await done(page, 'pcmgr');
      await page.waitForSelector(`.multi-member[data-account="${carol}"] .btn`);
      await member(page, carol).locator('.btn', {hasText: '仍然合并'}).click();
      await page.waitForFunction(id => /已合并/.test(document.querySelector(`.multi-member[data-account="${id}"] small`).textContent), carol);
      assert.equal(byId(await accountsOf(context), carol).fleet_primary_id, alice);
      assert.equal(qr.bound.at(-1).force, true);
      assert.deepEqual(errors, []);
      await dissolve(context, alice);
      await context.close();
    }

    // A missing unionid only warns.
    {
      const {context, page, errors} = await fresh(browser);
      const S = await seeded(context);
      const [first, second] = S.dave;
      const qr = await mockQR(context, await withAccounts(context, [['appstore', first], ['pcmgr', second]]));
      await page.goto(base + '/scan?multi=1');
      await waiting(page, 3);
      qr.scan('appstore');
      await done(page, 'appstore');
      qr.scan('pcmgr');
      await done(page, 'pcmgr');
      await page.waitForSelector(`.multi-member[data-account="${second}"] .multi-member-note`);
      assert.match(await member(page, second).locator('.multi-member-note').innerText(), /unionid/);
      assert.match(await member(page, second).locator('small').first().innerText(), /已合并/);
      assert.deepEqual(errors, []);
      await dissolve(context, first);
      await context.close();
    }

    // Existing-account supplementation now creates one root-anchored update link.
    {
      const {context, page, errors} = await fresh(browser);
      const S = await seeded(context);
      const [boomApp, boomMember] = S.boom;
      let requested;
      await context.route('**/api/account-links',route=>{requested=route.request().postDataJSON();return route.fulfill(json({url:base+'/account-link/synthetic-update'}));});
      await context.route('**/account-link/synthetic-update',route=>route.fulfill({status:200,contentType:'text/html',body:'<p>更新链接</p>'}));
      const qr = await mockQR(context, {});
      await page.goto(base+'/scan?ref='+boomMember);
      await page.waitForURL(base+'/account-link/synthetic-update');
      assert.deepEqual(requested,{kind:'update',ref:String(boomApp),allow_multi_channel:true});
      assert.equal(qr.requested.length,0);
      assert.deepEqual(errors, []);
      await context.close();
    }

    // A channel added on the server shows up without a page change.
    {
      const {context, page, errors} = await fresh(browser);
      const extra = {id: 'wechatpad', name: 'WeChatPad', badge: 'wechatpad', description: '示例新渠道'};
      const registry = (await (await context.request.get(base + '/api/scan-channels')).json()).data.channels;
      await context.route('**/api/scan-channels', route => route.fulfill(json({channels: [...registry, extra]})));
      const qr = await mockQR(context, {});
      await page.goto(base + '/scan?multi=1');
      await waiting(page, registry.length + 1);
      assert(qr.requested.includes('wechatpad'));
      assert.equal(await page.locator('.multi-card[data-product="wechatpad"] strong').innerText(), 'WeChatPad');
      await shot(page, 'scan-multi-four-channels');
      await noOverflow(page, 'four channel scan page');
      assert.deepEqual(errors, []);
      await context.close();
    }
    // ---- the console ----

    // One card per fleet: a chip per channel, what needs attention, the members on demand.
    {
      const {context, page, errors} = await fresh(browser);
      const S = await seeded(context);
      const [boomApp, boomPc, boomGame] = S.boom;
      await page.goto(base + '/?ref=' + boomApp);
      const fleet = page.locator(`.account-card.fleet[data-fleet="${boomApp}"]`);
      await fleet.waitFor();
      assert.equal(await page.locator('.account-card.fleet').count(), 1);
      assert.equal(await page.locator('.account-card[data-openid="pcmgr:boom00000000000000000002"]').count(), 0, 'members do not get cards of their own');
      assert.equal(await fleet.locator('.channel-chip:not(.missing)').count(), 3);
      assert.deepEqual(await fleet.locator('.channel-chip:not(.missing)').evaluateAll(chips => chips.map(chip => [...chip.classList].filter(name => ['alive', 'expired', 'warn'].includes(name))[0])), ['alive', 'alive', 'expired']);
      assert.match(await fleet.locator('.account-status').innerText(), /2\/3 可用/);
      assert.match(await fleet.locator('.fleet-note').innerText(), /GameAssist 已失效/);
      assert.match(await page.locator('#accountFilterCount').innerText(), /1 个多渠道账号/);
      assert.equal(await fleet.locator('.fleet-members').isVisible(), false);
      await shot(page, 'console-fleet-collapsed');

      await fleet.locator('.fleet-expand').click();
      assert.equal(await fleet.locator('.fleet-member').count(), 3);
      assert.match(await fleet.locator(`.fleet-member[data-account="${boomApp}"]`).innerText(), /主账号/);
      // Picking a member selects it; the card stays highlighted as a whole.
      await fleet.locator(`.fleet-member[data-account="${boomPc}"] .fleet-member-select`).click();
      await page.waitForFunction(id => new URLSearchParams(location.search).get('ref') === String(id), boomPc);
      assert.equal(await fleet.evaluate(card => card.classList.contains('selected')), true);
      assert.equal(await fleet.locator(`.fleet-member[data-account="${boomPc}"]`).evaluate(row => row.classList.contains('selected')), true);
      assert.equal(await fleet.locator(`.fleet-member[data-account="${boomApp}"]`).evaluate(row => row.classList.contains('selected')), false);
      assert.match(await page.locator('#multiScanLink').getAttribute('href'), new RegExp(`ref=${boomApp}$`), 'the button adds channels to the primary');
      await shot(page, 'console-fleet-open');
      // A re-render keeps the card open.
      await page.click('#reloadAccountsBtn');
      await page.waitForFunction(() => document.querySelector('.account-card.fleet .fleet-expand')?.getAttribute('aria-expanded') === 'true');
      // Search and status filters show the card when any of its accounts matches.
      await page.fill('#accountSearch', 'Alice');
      assert.equal(await page.locator('.account-card.fleet').count(), 0);
      await page.fill('#accountSearch', 'pcmgr:boom');
      await page.waitForSelector('.account-card.fleet');
      assert.match(await page.locator('#accountFilterCount').innerText(), /显示 3 \//);
      await page.fill('#accountSearch', '');
      await page.selectOption('#accountStatusFilter', 'expired');
      assert.equal(await page.locator('.account-card.fleet').count(), 1, 'one expired channel is enough');
      await page.selectOption('#accountStatusFilter', 'all');

      // An independent account gets the button too.
      assert.match(await page.locator(`.account-card[data-openid="oAliceAppStore0000000000000004"] .account-row-actions a`).first().getAttribute('href'), new RegExp(`/scan\\?ref=${S.alice[0]}$`));
      for (const width of [1440, 768, 390, 320]) {
        await page.setViewportSize({width, height: 1000});
        assert.equal(await fleet.evaluate(card => card.scrollWidth > card.clientWidth + 1), false, `the fleet card overflows at ${width}`);
      }
      assert.deepEqual(errors.filter(message => !/avatar|404/.test(message)), []);
      await context.close();
    }

    // The mobile dock offers the multi-channel scan for whichever account is selected.
    {
      const {context, page} = await fresh(browser);
      const S = await seeded(context);
      await page.setViewportSize({width: 390, height: 900});
      await page.goto(base + '/?ref=' + S.boom[2]);
      await page.waitForSelector('.account-card.fleet');
      await page.waitForSelector('#mobileAccountActions:not([hidden])');
      assert.match(await page.locator('#mobileMultiScanLink').getAttribute('href'), new RegExp(`ref=${S.boom[0]}$`));
      await context.close();
    }

    // Merging separate accounts into one, from the console.
    {
      const {context, page, errors} = await fresh(browser);
      const S = await seeded(context);
      const [aliceApp, alicePc, aliceGame] = S.alice;
      const carol = S.carol[0];
      await page.goto(base + '/');
      await page.waitForSelector('.account-card');
      await page.click('#mergeChannelsBtn');
      await page.waitForSelector('#mergeChannelsDialog[open]');
      const options = await page.locator('#mergeChannelsPrimary option').evaluateAll(list => list.map(option => option.value));
      assert(options.includes(String(aliceApp)) && options.includes(String(S.boom[0])));
      assert(options.includes(String(alicePc)), 'any independent account can be the primary');
      assert(!options.includes(String(S.boom[1])), 'a member of a fleet is not a primary');
      await page.selectOption('#mergeChannelsPrimary', String(aliceApp));
      const picks = async () => page.locator('#mergeChannelsList input').evaluateAll(list => Object.fromEntries(list.map(input => [input.value, input.checked])));
      let list = await picks();
      assert.equal(list[alicePc], true, 'the same nickname on another channel is suggested');
      assert.equal(list[aliceGame], true);
      assert.equal(list[carol], false, 'a different nickname is not');
      for (const id of S.boom) assert.equal(String(id) in list, false, 'accounts of another fleet cannot be merged in');
      await shot(page, 'console-merge-dialog');
      await page.click('#mergeChannelsConfirmBtn');
      await page.waitForFunction(() => document.getElementById('mergeChannelsConfirmBtn').textContent === '完成');
      assert.equal(await page.locator('#mergeChannelsResults .ok').count(), 2);
      assert.match(await page.locator('#mergeChannelsResults').innerText(), new RegExp(`保留主账号 ID ${aliceApp}`));
      let all = await accountsOf(context);
      assert.deepEqual([aliceApp, alicePc, aliceGame].map(id => byId(all, id).fleet_primary_id), [aliceApp, aliceApp, aliceApp]);
      await shot(page, 'console-merge-done');
      await page.click('#mergeChannelsConfirmBtn');
      await page.waitForFunction(() => !document.getElementById('mergeChannelsDialog').open);
      await page.waitForSelector(`.account-card.fleet[data-fleet="${aliceApp}"]`);
      assert.equal(await page.locator(`.account-card.fleet[data-fleet="${aliceApp}"] .channel-chip:not(.missing)`).count(), 3);

      // Someone else's account is refused until the operator confirms.
      await page.click('#mergeChannelsBtn');
      await page.waitForSelector('#mergeChannelsDialog[open]');
      await page.selectOption('#mergeChannelsPrimary', String(aliceApp));
      await page.locator(`#mergeChannelsList input[value="${carol}"]`).check();
      await page.click('#mergeChannelsConfirmBtn');
      await page.waitForSelector('#mergeChannelsResults .bad .btn');
      assert.match(await page.locator('#mergeChannelsResults .bad').innerText(), /unionid/);
      assert.equal(byId(await accountsOf(context), carol).fleet_primary_id ?? null, null);
      await page.locator('#mergeChannelsResults .bad .btn').click();
      await page.waitForFunction(() => document.querySelectorAll('#mergeChannelsResults .ok').length === 1);
      assert.equal(byId(await accountsOf(context), carol).fleet_primary_id, aliceApp);
      await page.click('#mergeChannelsConfirmBtn');
      await page.waitForFunction(() => !document.getElementById('mergeChannelsDialog').open);

      // Taking one out and splitting the rest.
      const card = page.locator(`.account-card.fleet[data-fleet="${aliceApp}"]`);
      await card.waitFor();
      assert.equal(await card.locator('.channel-chip:not(.missing)').count(), 4);
      await card.locator('.fleet-expand').click();
      page.once('dialog', dialog => dialog.accept());
      await card.locator(`.fleet-member[data-account="${carol}"] [data-leave-fleet]`).click();
      await page.waitForFunction(id => !document.querySelector(`.fleet-member[data-account="${id}"]`), carol);
      assert.equal(byId(await accountsOf(context), carol).fleet_primary_id ?? null, null);
      assert.equal(byId(await accountsOf(context), alicePc).fleet_primary_id, aliceApp);
      page.once('dialog', dialog => dialog.accept());
      await card.locator('[data-dissolve-fleet]').click();
      await page.waitForFunction(id => !document.querySelector(`.account-card.fleet[data-fleet="${id}"]`), aliceApp);
      all = await accountsOf(context);
      for (const id of [...S.alice, carol]) assert.equal(byId(all, id).fleet_primary_id ?? null, null, `account ${id} is independent again`);
      assert.equal(byId(all, S.boom[0]).fleet_primary_id, S.boom[0], 'the other fleet is untouched');
      assert.deepEqual(errors.filter(message => !/avatar|404/.test(message)), []);
      await context.close();
    }

    // The console follows the server's channel list: a channel nobody has bound shows as a way to add it.
    {
      const {context, page, errors} = await fresh(browser);
      const S = await seeded(context);
      const extra = {id: 'wechatpad', name: 'WeChatPad', badge: 'wechatpad', description: '示例新渠道'};
      const registry = (await (await context.request.get(base + '/api/scan-channels')).json()).data.channels;
      await context.route('**/api/scan-channels', route => route.fulfill(json({channels: [...registry, extra]})));
      await page.goto(base + '/');
      const fleet = page.locator(`.account-card.fleet[data-fleet="${S.boom[0]}"]`);
      await fleet.waitFor();
      await page.waitForFunction(id => document.querySelector(`.account-card.fleet[data-fleet="${id}"] .channel-chip.missing`), S.boom[0]);
      assert.equal(await fleet.locator('.channel-chip.missing').count(), registry.length + 1 - 3);
      assert.match(await fleet.locator('.channel-chip.missing').first().getAttribute('href'), new RegExp(`/scan\\?ref=${S.boom[0]}$`));
      assert.equal(await page.locator('#accountLinkProduct option').count(), registry.length + 1);
      assert.deepEqual(errors.filter(message => !/avatar|404/.test(message)), []);
      await context.close();
    }
    console.log('multi-channel scan UI ok');
  } finally {
    await resetFleets(browser).catch(() => {});
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
