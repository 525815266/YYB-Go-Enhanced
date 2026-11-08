// Synthetic official API shapes only; no browser profile or upstream requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'resource/static/js/qbbrowser-bridge.js'), 'utf8');
assert.equal(source, fs.readFileSync(path.join(root, 'extensions/qbbrowser-token/bridge.js'), 'utf8'));
const run = info => {
  const context = vm.createContext({setTimeout, clearTimeout,
    chrome:{account:{getLoginInfo: callback => callback(info)}},
    qb_external:{getGuid:()=> 'machine-id',getFavSyncGuid:()=> 'browser-guid',getQ16:()=> 'q16',getVersion:()=> '22.3'}});
  vm.runInContext(source,context);
  return context;
};
(async()=>{
  const c = run({mode:3,ctk:'synthetic-ctk',openid:'synthetic-openid'});
  const result = await c.readQBBrowserCredentials();
  assert.equal(result.credentials.access_token, 'synthetic-ctk');
  assert.equal(result.credentials.user_id, 'machine-id');
  assert.equal(result.credentials.openid, 'synthetic-openid');
  assert.equal(result.credentials.guid, 'browser-guid');
  assert.match(result.credentials.qbrn_cookie,/GAME_H5_DEVICE_MACHINE_ID=machine-id/);
  for (const info of [{mode:5,ctk:'synthetic',openid:'other'},{mode:3,accessToken:'wrong-token-alias',openid:'id'}]) {
    await assert.rejects(run(info).readQBBrowserCredentials());
  }
  delete c.qb_external;
  await assert.rejects(c.readQBBrowserCredentials(),/机器标识/);
  const background = await c.readQBBrowserCredentials(result.device);
  assert.equal(background.credentials.guid,'browser-guid');
  c.qb_external={getGuid:()=>'',getFavSyncGuid:()=>''};
  assert.equal((await c.readQBBrowserCredentials(result.device)).credentials.user_id,'machine-id');
  c.chrome.qbExternal={getGuid:cb=>cb('async-machine'),getFavSyncGuid:cb=>cb(''),getQ16:cb=>cb('async-q16'),getVersion:cb=>cb('22.3')};
  const asynchronous=await c.readQBBrowserCredentials();
  assert.equal(asynchronous.credentials.user_id,'async-machine');
  assert.equal(asynchronous.credentials.guid,'','optional sync guid must not block official exchange');
  assert.match(asynchronous.credentials.qbrn_cookie,/QIMEI36=async-q16/);
  console.log('QQ Browser bridge regression passed');
})();
