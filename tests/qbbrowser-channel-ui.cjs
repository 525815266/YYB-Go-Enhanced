// Isolated service only. All QR replies are synthetic; no upstream login calls.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
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
      if (req.method() === 'POST' && new URL(req.url()).pathname === '/qr' && req.postData()?.includes('qbbrowser')) qbQRRequests++;
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
      await page.waitForFunction(() => document.querySelector('#qrWrap img'));
      assert(await page.locator('#qrPane').isVisible());
      assert.equal(await page.locator('#refreshBtn').textContent(), '重新生成');
      assert.equal(await page.locator('script[src*="qbbrowser-bridge"]').count(), 0);
      await page.locator('label[for="multiScanSwitch"]').click();
      await page.waitForFunction(() => document.querySelector('[data-product="qbbrowser"] img'));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (process.env.YYB_TEST_SHOTS) await page.screenshot({path: path.join(process.env.YYB_TEST_SHOTS, `yyb-qb-ui-${name}-20261108.png`), fullPage: true});
    }
    assert(qbQRRequests >= 4, 'QQ must use the QR endpoint in single and multi mode');
    // Public links cannot read the authenticated channel registry. Their
    // single picker and multi cards must both expose standalone QQ too.
    let linkMulti = false, publicQQRequests = 0;
    await context.route(base+'/account-link/qq-fixture', route => route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:fs.readFileSync(path.join(__dirname,'../resource/templates/account-link.html'),'utf8')}));
    await context.route('**/account-link/qq-fixture/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith('/meta')) return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({code:0,data:{product:'qbbrowser',kind:'new',allow_multi_channel:linkMulti,expires_at:Math.floor(Date.now()/1000)+300}})});
      if (pathname.endsWith('/qr')) {
        const product = route.request().postDataJSON().product;
        if (product === 'qbbrowser') publicQQRequests++;
        return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({code:0,data:{session_id:product,image_url:base+'/account-link/qq-fixture/qr/'+product+'/image'}})});
      }
      if (pathname.endsWith('/image')) return route.fulfill({status:200,contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="260" height="260"/>'});
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({code:0,data:{status:'pending'}})});
    });
    await page.goto(base+'/account-link/qq-fixture');
    await page.waitForSelector('#qr img');
    assert.equal(await page.locator('#scanProduct').inputValue(),'qbbrowser');
    linkMulti = true;
    await page.reload();
    await page.waitForSelector('[data-product="qbbrowser"] img');
    assert(publicQQRequests >= 2, 'public single and multi links must request QQ QR');
    assert.equal(tokenRequests, 0, 'standalone scan must not depend on browser token pushes');
    assert.deepEqual(errors, []);
    console.log('QQ channel desktop/mobile regression passed');
  } finally { await browser.close(); }
})();
