// Entire browser acceptance uses synthetic HTTP responses. No real login/code.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = 'http://yyb-appid.test';
const root = path.resolve(__dirname, '..');
const sample = 'wx1111111111111111';
(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.YYB_TEST_BROWSER || 'msedge' });
  try {
    for (const width of [1280, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      const errors = [];
      let learned = { [sample]: '临时取码限制：空码不代表应用类型' };
      let removed = 0;
      await context.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        assert.equal(url.origin, base, 'Unexpected external request');
        const ok = data => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ code: 0, data }) });
        if (url.pathname === '/settings') return route.fulfill({ status: 200, contentType: 'text/html', body: fs.readFileSync(path.join(root, 'resource/templates/settings.html'), 'utf8') });
        if (url.pathname.startsWith('/static/')) {
          const filename = path.join(root, 'resource', url.pathname);
          return route.fulfill({ status: 200, contentType: filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : 'image/svg+xml', body: fs.existsSync(filename) ? fs.readFileSync(filename) : '' });
        }
        if (url.pathname === '/api/appid-deny') {
          if (request.method() === 'POST') {
            const form = new URLSearchParams(request.postData());
            assert.equal(form.get('action'), 'clear_learned');
            assert.equal(form.get('app_id'), sample);
            learned = {}; removed++;
          }
          return ok({ builtin: {}, env_denied: {}, persisted_deny: {}, allow: [], learned, learned_count: Object.keys(learned).length,
            learned_state: { [sample]: { retry_at: Math.floor(Date.now()/1000)+900, checking: false, ready: false } } });
        }
        if (url.pathname === '/api/runtime-settings') return ok({ effective: { code_min_gap_ms: 3000, code_max_wait_ms: 15000, login_budget: 3, login_window_ms: 600000, fresh_session_window_ms: 300000 }, dynamic_deny: { threshold: 2, enabled: true } });
        if (url.pathname === '/api/scan-settings') return ok({ personal: '', global: 'appstore', has_personal_account: false, can_manage_global: false });
        if (url.pathname === '/api/scan-channels') return ok({ channels: [] });
        if (url.pathname === '/api/auth/me') return ok({ auth_enabled: false });
        if (url.pathname === '/api/version') return ok({ version: '0.3.3' });
        return ok({});
      });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base+'/settings');
      await page.locator('#learnedList').getByRole('button', { name: '解除本项' }).waitFor();
      assert.match(await page.locator('#learnedList').innerText(), /可复核时间/);
      assert.match(await page.locator('#rtDenyThreshold').evaluate(el => el.labels[0].textContent), /主账号组/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (process.env.YYB_TEST_SHOTS) await page.screenshot({ path: path.join(process.env.YYB_TEST_SHOTS, `appid-policy-${width}.png`), fullPage: true });
      await page.locator('#learnedList').getByRole('button', { name: '解除本项' }).click();
      await page.waitForFunction(() => document.querySelector('#learnedCount').textContent === '0');
      assert.equal(removed, 1);
      assert.match(await page.locator('#policyNotice').innerText(), /已解除临时限制/);
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log('AppID policy desktop/mobile acceptance passed; external requests 0');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
