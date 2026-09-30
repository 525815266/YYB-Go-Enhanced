// Run against an isolated server with YYB_TEST_URL/USER/PASS and Playwright installed.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const base = process.env.YYB_TEST_URL;
  if (!base) throw new Error('Set YYB_TEST_URL to an isolated test server');
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {
    const context = await browser.newContext({reducedMotion:'reduce'});
    const login = await context.request.post(base + '/login', {data:{username:process.env.YYB_TEST_USER, password:process.env.YYB_TEST_PASS}});
    assert.equal((await login.json()).code, 0);
    for (const route of ['/health', '/healthz']) {
      const result = await context.request.get(base + route);
      assert.equal(result.status(), 200);
      assert.equal((await result.json()).data.ok, true);
    }
    const now = Math.floor(Date.now()/1000);
    const accounts = ['晨风','山岚','星河','听雨','远舟','拾光'].map((nickname,i) => ({id:i+1, nickname, remark:'', openid:`demo-account-${i+1}`, status:['alive','unknown','expired'][i%3], refresh_token_observed_at:now-(i*4+1)*86400, credential_expires_at:now+5820, credential_expires_in:7200}));
    const envelope = data => ({status:200,contentType:'application/json',body:JSON.stringify({code:0,msg:'success',data})});
    await context.route('**/api/auth/me',route=>route.fulfill(envelope({auth_enabled:true,user:{username:'demo',display_name:'演示管理员',role:'admin'}})));
    await context.route('**/api/version',route=>route.fulfill(envelope({version:require('node:fs').readFileSync(path.join(__dirname,'../VERSION'),'utf8').trim()})));
    let proxyReads=0;
    await context.route('**/accounts', route => route.fulfill(envelope(accounts)));
    await context.route('**/accounts/proxy?*', route => {proxyReads++; return route.fulfill(envelope({configured:false}));});
    await context.route('**/accounts/avatar?*', route => route.fulfill({status:404,body:''}));
    await context.route('**/accounts/status', route => {
      const body = route.request().postDataJSON();
      assert.equal(body.ref, '2');
      const account = accounts.find(a => String(a.id) === body.ref);
      account.status = body.status;
      return route.fulfill(envelope({account}));
    });
    const page = await context.newPage();
    const errors=[];
    page.on('pageerror', error => errors.push(error.message));
    for (const width of [1440,1024,768,390,320]) {
      await page.setViewportSize({width,height:1000});
      await page.goto(base + '/');
      await page.locator('.account-card').first().waitFor();
      await page.locator('#accountProxyStatus').filter({hasText:'直连'}).waitFor();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth+1),false,`overflow ${width}`);
      assert.equal(await page.locator('button button').count(),0);
      const time = await page.evaluate(() => [0,59,60,3599,3600,5820,86399,86400].map(lifecycleDuration));
      assert.deepEqual(time,['已到期','不足1分钟','1分钟','59分钟','1小时0分钟','1小时37分钟','23小时59分钟','1天0小时']);
      const expired = page.locator('.account-card.expired').first();
      assert.equal(await expired.locator('.lifecycle-track').count(),0);
      assert.equal((await expired.innerText()).includes('可参与保活'),false);
      const before=proxyReads;
      await page.locator('#accountSearch').fill('星河');
      assert.equal(await page.locator('.account-card').count(),1);
      assert.equal(await page.locator('#selectedAccountName').innerText(),'晨风');
      await page.locator('#accountSearch').fill('no-match');
      assert.equal(await page.locator('.account-card').count(),0);
      await page.locator('#accountSearch').fill('');
      await page.locator('#accountStatusFilter').selectOption('expired');
      assert.equal(await page.locator('.account-card').count(),2);
      assert.equal(proxyReads,before,'filter must not perform network reads');
      await page.locator('#accountStatusFilter').selectOption('all');
      if (width < 861) {
        await page.locator('#platformMenu').click();
        assert.equal(await page.locator('#platformMenu').getAttribute('aria-expanded'),'true');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#platformMenu').getAttribute('aria-expanded'),'false');
        assert.equal(await page.evaluate(()=>document.activeElement.id),'platformMenu');
      }
      if (process.env.YYB_SCREENSHOT_DIR && [1440,390].includes(width)) {
        await page.locator('h2').first().click();
        await page.waitForFunction(()=>[...document.querySelectorAll('.avatar img')].every(img=>img.complete));
        await page.screenshot({path:path.join(process.env.YYB_SCREENSHOT_DIR, `console-demo-${width}.png`)});
      }
    }
    await page.locator('.account-card').nth(1).getByRole('button',{name:'仍有效',exact:true}).click();
    assert.equal(await page.locator('#selectedAccountName').innerText(),'晨风','confirmation must not change selection');
    await page.waitForFunction(()=>document.querySelectorAll('[data-confirm-status="alive"]').length===1);
    for (const route of ['/?focus=test','/runs?view=logs','/proxies','/account-links','/users','/maintenance']) {
      await page.goto(base+route);
      await page.locator('.platform-shell').waitFor();
      if (route.includes('focus')) assert.equal(await page.locator('.platform-page-title').innerText(),'接口测试');
      if (route.includes('view')) assert.equal(await page.locator('.platform-page-title').innerText(),'调用记录');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth+1),false,route);
    }
    await context.route('**/api/auth/me',route=>route.fulfill(envelope({auth_enabled:true,user:{username:'演示用户',role:'user'}})));
    await page.goto(base+'/');
    await page.waitForFunction(()=>document.querySelector('.account-maintenance').hidden);
    await page.waitForFunction(()=>document.querySelector('#platformUserRole').textContent === '普通用户');
    assert.equal(await page.locator('.platform-nav a[href="/users"]').isVisible(),false);
    assert.deepEqual(errors,[]);
    console.log('PASS: responsive layout, filtering, duration, expired state, confirmation, navigation and user visibility');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
