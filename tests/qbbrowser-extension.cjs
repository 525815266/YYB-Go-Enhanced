// Synthetic account/network APIs only. Reproduces silent failures without login.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const directory = path.resolve(__dirname, '../extensions/qbbrowser-token');
const source = file => fs.readFileSync(path.join(directory, file), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const defer = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {resolve, promise}; };

function worker(overrides = {}) {
  const stored = {serverUrl: 'http://yyb.test', protocolToken: 'synthetic-protocol', device: {machine: 'synthetic-machine'}};
  const records = {stored, requests: [], deviceReads: 0};
  let listener;
  const context = vm.createContext({URL, Date, AbortController,
    setTimeout: (fn, ms) => setTimeout(fn, ms === 15000 ? 20 : ms), clearTimeout,
    importScripts() {},
    readQBBrowserDevice: async () => { records.deviceReads++; return {machine: 'official-device'}; },
    readQBBrowserCredentials: async device => ({device, credentials: {access_token: 'synthetic-ctk', user_id: device.machine}}),
    fetch: overrides.fetch || (async (url, options) => {
      records.requests.push({url, options});
      return {ok: true, status: 200, json: async () => ({code: 0, data: {id: 1}})};
    }),
    chrome: {storage: {local: {
      get: overrides.get || (async () => ({...stored})),
      set: overrides.set || (async value => { Object.assign(stored, value); })
    }}, alarms: {create() {}, onAlarm: {addListener() {}}}, runtime: {
      onInstalled: {addListener() {}}, onStartup: {addListener() {}},
      onMessage: {addListener(fn) { listener = fn; }}
    }}
  });
  vm.runInContext(source('serviceWorker.js'), context);
  const message = readDevice => new Promise(resolve => {
    assert.equal(listener({type: 'push-now', readDevice}, {}, resolve), true);
  });
  return {context, records, message};
}

function deviceFixture(options = {}) {
  const state = {removed: [], listeners: new Set(), injections: 0};
  const official = 'https://pcgame.qqbrowser.qq.com/main';
  const context = vm.createContext({URL, setTimeout: fn => setTimeout(fn, 10), clearTimeout,
    chrome: {tabs: {
      onUpdated: {addListener: fn => state.listeners.add(fn), removeListener: fn => state.listeners.delete(fn)},
      create: options.create || (async () => ({id: 21})),
      get: async () => ({id: 21, status: options.loading ? 'loading' : 'complete', url: options.url || official}),
      remove: async id => { state.removed.push(id); }
    }, scripting: {executeScript: async request => {
      state.injections++;
      assert.equal(request.world, 'MAIN');
      assert.equal(request.target.tabId, 21);
      // Execute the actual injected function, with synthetic native values.
      const page = vm.createContext({location: {origin: new URL(official).origin}, qb_external: {
        getGuid: () => options.emptyMachine ? undefined : 'synthetic-native',
        getFavSyncGuid: () => undefined, getQ16: () => '', getVersion: () => '22.3'
      }});
      return [{result: vm.runInContext(`(${request.func.toString()})()`, page)}];
    }}}
  });
  vm.runInContext(source('device.js'), context);
  return {state, read: () => context.readQBBrowserDevice()};
}

function optionsFixture(overrides = {}) {
  const elements = Object.fromEntries(['version', 'officialLogin', 'status', 'serverUrl', 'protocolToken', 'pushNow']
    .map(id => [id, {value: '', textContent: '', disabled: false, style: {}, dataset: {}}]));
  const sent = [];
  const context = vm.createContext({URL, Date,
    setTimeout: (fn, ms) => setTimeout(fn, ms === 40000 ? 30 : ms), clearTimeout,
    document: {getElementById: id => elements[id]},
    chrome: {
      account: {},
      runtime: {getManifest: () => ({version: '1.2.0'}), sendMessage: async msg => {
        sent.push(msg);
        return overrides.send ? overrides.send(msg) : {done: true, lastStatus: 'synthetic saved', lastKind: 'success'};
      }},
      storage: {local: {get: async () => ({}), set: overrides.set || (async () => {})}, onChanged: {addListener() {}}}
    }
  });
  vm.runInContext(source('options.js'), context);
  return {elements, sent};
}

(async () => {
  const manual = worker();
  const saved = await manual.message(true);
  assert.equal(saved.lastKind, 'success');
  assert.equal(manual.records.deviceReads, 1);
  assert.equal(manual.records.requests[0].options.redirect, 'error');
  assert.equal(JSON.parse(manual.records.requests[0].options.body).user_id, 'official-device');
  assert.ok(!JSON.stringify(manual.records.stored).includes('synthetic-ctk'), 'ctk must never be persisted');
  await manual.message(false);
  assert.equal(manual.records.deviceReads, 1, 'periodic push uses cached native device');

  const gate = defer();
  const concurrent = worker({fetch: () => gate.promise});
  const first = concurrent.message(true), second = concurrent.message(true);
  await tick();
  assert.equal(concurrent.records.deviceReads, 1, 'concurrent pushes share one operation');
  gate.resolve({ok: true, status: 200, json: async () => ({code: 0})});
  assert.equal((await first).lastKind, 'success');
  assert.equal((await second).lastKind, 'success');

  for (const overrides of [
    {get: async () => { throw new Error('synthetic storage failure'); }},
    {set: async () => { throw new Error('synthetic storage failure'); }},
    {fetch: async () => ({ok: false, status: 502, json: async () => { throw new Error('non-json'); }})},
    {fetch: async () => ({ok: false, status: 401, json: async () => ({msg: 'private-upstream-body'})})},
    {fetch: (_url, {signal}) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new Error('abort'))))},
    {fetch: async (_url, {signal}) => ({ok: true, status: 200, json: () => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new Error('abort'))))})}
  ]) {
    const failed = await worker(overrides).message(true);
    assert.equal(failed.done, true);
    assert.equal(failed.lastKind, 'error');
    assert.ok(!failed.lastStatus.includes('private-upstream-body'));
  }

  const normal = deviceFixture();
  assert.equal((await normal.read()).machine, 'synthetic-native');
  assert.deepEqual(normal.state.removed, [21]);
  assert.equal(normal.state.listeners.size, 0);
  for (const settings of [{url: 'https://untrusted.test/'}, {emptyMachine: true}, {loading: true}]) {
    const fixture = deviceFixture(settings);
    await assert.rejects(fixture.read());
    assert.deepEqual(fixture.state.removed, [21]);
    assert.equal(fixture.state.listeners.size, 0);
    if (settings.url || settings.loading) assert.equal(fixture.state.injections, 0);
  }
  const late = defer(), delayed = deviceFixture({create: () => late.promise});
  await assert.rejects(delayed.read(), /超时/);
  late.resolve({id: 21});
  await tick();
  assert.deepEqual(delayed.state.removed, [21], 'late-created temporary tab must also close');
  assert.equal(delayed.state.injections, 0);

  const completion = defer();
  const ui = optionsFixture({send: () => completion.promise});
  await tick();
  ui.elements.serverUrl.value = 'http://yyb.test';
  const click = ui.elements.pushNow.onclick();
  assert.equal(ui.elements.pushNow.disabled, true);
  assert.match(ui.elements.status.textContent, /正在/);
  await ui.elements.pushNow.onclick();
  await tick();
  assert.equal(ui.sent.length, 1);
  completion.resolve({done: true, lastStatus: 'synthetic saved', lastKind: 'success'});
  await click;
  assert.equal(ui.elements.pushNow.disabled, false);
  assert.equal(ui.elements.status.dataset.kind, 'success');
  for (const overrides of [
    {set: async () => { throw new Error('storage unavailable'); }},
    {send: async () => { throw new Error('worker unavailable'); }},
    {send: async () => undefined},
    {send: () => new Promise(() => {})}
  ]) {
    const failed = optionsFixture(overrides);
    await tick();
    failed.elements.serverUrl.value = 'http://yyb.test';
    await failed.elements.pushNow.onclick();
    assert.equal(failed.elements.pushNow.disabled, false);
    assert.equal(failed.elements.status.dataset.kind, 'error');
    assert.ok(failed.elements.status.textContent);
  }
  console.log('QQ extension device, worker, timeout and button regression passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
