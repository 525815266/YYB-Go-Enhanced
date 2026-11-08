// Isolated service only. All QR replies are synthetic; no upstream login calls.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const base = process.env.YYB_TEST_URL;
if (!base) throw new Error('Set YYB_TEST_URL to an isolated test service');
(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.YYB_TEST_BROWSER ? {channel: process.env.YYB_TEST_BROWSER} : {})});
  try {
    const context = await browser.newContext({viewport: {width: 1280, height: 1000}});
    const auth = await context.request.post(base + '/login', {data: {username: process.env.YYB_TEST_USER, password: process.env.YYB_TEST_PASS}});
    assert.equal((await auth.json()).code, 0);
    let qbQRRequests = 0, tokenRequests = 0;
    await context.route('**/qr**', async route => {
      const req = route.request();
      if (req.method() === 'POST' && req.postData()?.includes('qbbrowser')) qbQRRequests++;
      const data = req.url().includes('/poll') ? {status: 'pending'} : {
        session_id: 'synthetic-qr', image_base64: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="280" height="280"><rect width="280" height="280" fill="gray"/></svg>'
      };
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({code: 0, data})});
    });
    await context.route('**/api/qbbrowser/token', route => { tokenRequests++; return route.abort(); });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    for (const [name, viewport] of [['desktop', {width: 1280, height: 1000}], ['mobile', {width: 390, height: 844}]]) {
      await page.setViewportSize(viewport);
      await page.goto(base + '/scan');
      await page.waitForFunction(() => Array.from(document.querySelector('#scanProduct').options).some(o => o.value === 'qbbrowser'));
      await page.selectOption('#scanProduct', 'qbbrowser');
      await page.waitForFunction(() => document.querySelector('#status').textContent.includes('QQ'));
      assert(await page.locator('#qrPane').isVisible());
      assert.match(await page.locator('#qrWrap').innerText(), /尚不能独立完成/);
      await page.locator('label[for="multiScanSwitch"]').click();
      await page.waitForFunction(() => document.querySelector('[data-product="qbbrowser"] .multi-line')?.textContent.includes('QQ'));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (process.env.YYB_TEST_SHOTS) await page.screenshot({path: path.join(process.env.YYB_TEST_SHOTS, `yyb-qb-ui-${name}-20261108.png`), fullPage: true});
    }
    assert.equal(qbQRRequests, 0, 'QQ bridge must not call the QR endpoint');
    assert.equal(tokenRequests, 0, 'missing native APIs must not submit guessed credentials');
    assert.deepEqual(errors, []);
    console.log('QQ channel desktop/mobile regression passed');
  } finally { await browser.close(); }
})();
