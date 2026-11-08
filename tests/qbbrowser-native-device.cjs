// Opt-in real QQ Browser test in a disposable, unsigned, logged-out profile.
// Reads device metadata only. No account API, YYB push, or login is performed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  assert.ok(process.env.YYB_QB_EXECUTABLE, 'Set YYB_QB_EXECUTABLE to the official QQ Browser executable');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'yyb-native-device-'));
  const extension = path.resolve(__dirname, '../extensions/qbbrowser-token');
  const context = await chromium.launchPersistentContext(profile, {
    executablePath: process.env.YYB_QB_EXECUTABLE, headless: true,
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]
  });
  try {
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    const page = await context.newPage();
    await page.goto(`chrome-extension://${new URL(worker.url()).host}/options.html`);
    await page.waitForFunction(() => document.querySelector('#version').textContent === 'v1.2.0');
    const missingConfig = await page.evaluate(() => chrome.runtime.sendMessage({type: 'push-now'}));
    assert.equal(missingConfig.done, true);
    assert.equal(missingConfig.lastKind, 'error');
    assert.match(missingConfig.lastStatus, /服务地址/);
    await page.fill('#serverUrl', 'invalid-url');
    await page.click('#pushNow');
    await page.waitForFunction(() => document.querySelector('#status').dataset.kind === 'error' && !document.querySelector('#pushNow').disabled);
    assert.match(await page.locator('#status').textContent(), /请填写完整/);
    for (const [name, width] of [['desktop', 1280], ['mobile', 390]]) {
      await page.setViewportSize({width, height: 900});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (process.env.YYB_TEST_SHOTS) await page.screenshot({path: path.join(process.env.YYB_TEST_SHOTS, `qb-extension-1.2.0-${name}.png`), fullPage: true});
    }
    const result = await worker.evaluate(async () => {
      const before = (await chrome.tabs.query({})).map(tab => tab.id);
      const device = await readQBBrowserDevice();
      const after = (await chrome.tabs.query({})).map(tab => tab.id);
      return {accountApiPresent: typeof chrome.account?.getLoginInfo === 'function',
        machinePresent: typeof device.machine === 'string' && !!device.machine,
        versionPresent: typeof device.version === 'string' && !!device.version,
        originalTabsPreserved: before.every(id => after.includes(id)),
        temporaryTabClosed: after.every(id => before.includes(id))};
    });
    assert.ok(Object.values(result).every(Boolean));
    console.log(JSON.stringify({officialNativeDevice: result}));
  } finally { await context.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
