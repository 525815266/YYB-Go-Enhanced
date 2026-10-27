// Use an isolated server with YYB_TEST_URL/USER/PASS/PROTOCOL_TOKEN and a
// synthetic expired account (ID 1). No WeChat or merchant calls are made.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const base = process.env.YYB_TEST_URL;
  const token = process.env.YYB_TEST_PROTOCOL_TOKEN;
  assert.ok(base && token, 'Set isolated test URL and protocol token');
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {
    const context = await browser.newContext({reducedMotion:'reduce'});
    const login = await context.request.post(base + '/login', {data:{username:process.env.YYB_TEST_USER, password:process.env.YYB_TEST_PASS}});
    assert.equal((await login.json()).code, 0);
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const managementHeaders = [];
    page.on('request', request => {
      if (!/^\/wx(?:app)?\//.test(new URL(request.url()).pathname)) {
        managementHeaders.push(request.headers().authorization);
      }
    });
    for (const width of [1280,390,320]) {
      await page.setViewportSize({width,height:1000});
      await page.goto(base + '/?focus=test');
      await page.locator('.account-card').first().waitFor();
      const input = page.getByLabel('协议令牌（可选）');
      assert.equal(await input.getAttribute('type'), 'password');
      assert.equal(await input.inputValue(), '');
      await page.locator('#featureSel').selectOption('getUserInfo');
      await page.getByRole('button',{name:'执行调用',exact:true}).click();
      await page.locator('#resultMeta').filter({hasText:'调用失败 401'}).waitFor();
      assert.equal(await input.getAttribute('aria-invalid'), 'true');
      assert.match(await page.locator('#resultBox').innerText(), /协议令牌缺失或错误/);
      await input.fill('wrong');
      await page.getByRole('button',{name:'执行调用',exact:true}).click();
      await page.locator('#resultMeta').filter({hasText:'调用失败 401'}).waitFor();
      await input.fill(' Bearer ' + token + ' ');
      await page.locator('#featureSel').selectOption('oauth');
      await page.locator('#appidInput').fill('wx1234567890abcdef');
      await page.locator('#oauthRedirectInput').fill('https://example.invalid/callback');
      const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname === '/wx/oauth');
      await page.getByRole('button',{name:'执行调用',exact:true}).click();
      const response = await responsePromise;
      assert.equal(response.status(), 200);
      assert.equal(response.request().headers().authorization, 'Bearer ' + token);
      assert.equal(response.request().postData().includes(token), false);
      await page.locator('#resultMeta').filter({hasText:'调用完成'}).waitFor();
      assert.match(await page.locator('#resultBox').innerText(), /authorization_url/);
      assert.equal((await page.locator('#resultBox').innerText()).includes(token), false);
      assert.equal(await input.getAttribute('aria-invalid'), null);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+1), false);
      if (process.env.YYB_SCREENSHOT_DIR) {
        await page.waitForFunction(() => !document.querySelector('#callBtn').disabled);
        await input.evaluate(element => element.scrollIntoView({block:'center',behavior:'instant'}));
        await page.screenshot({path:path.join(process.env.YYB_SCREENSHOT_DIR, `protocol-auth-${width}.png`)});
      }
      await page.getByRole('button',{name:'清除令牌',exact:true}).click();
      assert.equal(await input.inputValue(), '');
      assert.equal(await input.evaluate(element => element === document.activeElement), true);
      await input.fill(token);
      await page.reload();
      assert.equal(await input.inputValue(), '', 'reload must discard the console token');
      assert.equal(await page.evaluate(secret => [localStorage,sessionStorage].some(storage => Object.values(storage).some(value => String(value).includes(secret))), token), false);
    }

    await page.setViewportSize({width:1280,height:1000});
    await page.goto(base + '/docs/index.html');
    await page.locator('.auth-wrapper button.authorize').click();
    await page.locator('.auth-container input').fill(token);
    await page.locator('.auth-btn-wrapper button.authorize').click();
    await page.locator('.auth-btn-wrapper button.btn-done').click();
    const userInfo = page.locator('.opblock.opblock-get').filter({has:page.locator('.opblock-summary-path').filter({hasText:/^\/wx\/getuserinfo$/})});
    await userInfo.locator('.opblock-summary').click();
    await userInfo.getByRole('button',{name:'Try it out',exact:true}).click();
    await userInfo.locator('input[placeholder="ref"]').fill('1');
    const swaggerResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/wx/getuserinfo');
    await userInfo.getByRole('button',{name:'Execute',exact:true}).click();
    const authorized = await swaggerResponse;
    assert.equal(authorized.request().headers().authorization, 'Bearer ' + token);
    assert.equal(authorized.status(), 409, 'synthetic expired account proves token passed before account validation');
    const health = page.locator('.opblock').filter({has:page.locator('.opblock-summary-path').filter({hasText:/^\/health$/})});
    await health.locator('.opblock-summary').click();
    await health.getByRole('button',{name:'Try it out',exact:true}).click();
    const healthResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/health');
    await health.getByRole('button',{name:'Execute',exact:true}).click();
    assert.equal((await healthResponse).request().headers().authorization, undefined);
    assert.equal(managementHeaders.some(Boolean), false, 'protocol token must not leak to management or static requests');
    assert.equal(await page.evaluate(secret => [localStorage,sessionStorage].some(storage => Object.values(storage).some(value => String(value).includes(secret))), token), false);
    assert.equal((await context.request.get(base + '/wx/getuserinfo?ref=1')).status(), 401, 'browser cookie must not bypass token gate');
    assert.deepEqual(errors, []);
    console.log('PASS: desktop/mobile token input, missing/wrong/valid token, header scoping, clear/reload, no storage, Swagger Authorize and unchanged anonymous rejection');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
