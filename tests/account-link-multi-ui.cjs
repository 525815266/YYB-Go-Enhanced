// Isolated service only, seeded with tests/seed-multi-channel-ui.py.
// QR/OAuth responses are synthetic; no upstream login or real account is used.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.YYB_TEST_URL;
if (!base) throw new Error('Set YYB_TEST_URL to an isolated service');
const reply = data => ({status:200,contentType:'application/json',body:JSON.stringify({code:0,data})});
(async () => {
  const browser = await chromium.launch({headless:true,...(process.env.YYB_TEST_CHROME ? {executablePath:process.env.YYB_TEST_CHROME} : {})});
  try {
    const context = await browser.newContext({viewport:{width:1280,height:1000}});
    const login = await context.request.post(base+'/login',{data:{username:process.env.YYB_TEST_USER,password:process.env.YYB_TEST_PASS}});
    assert.equal((await login.json()).code,0);
    const accounts = (await (await context.request.get(base+'/accounts')).json()).data;
    const alice = accounts.find(a => a.openid === 'oAliceAppStore0000000000000004');
    assert(alice,'seed test accounts first');
    const links = (await (await context.request.get(base+'/api/account-links')).json()).data.links;
    for (const link of links.filter(l => l.account_id === alice.id)) await context.request.delete(base+'/api/account-links/'+link.id);
    const page = await context.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(base+'/?ref='+alice.id);
    await page.locator('#updateAccountLinkBtn').click();
    await page.locator('#accountLinkMulti').check();
    assert(await page.locator('#accountLinkProduct').isDisabled());
    await page.locator('#accountLinkGenerateBtn').click();
    await page.waitForFunction(() => document.getElementById('accountLinkURL').value.includes('/account-link/'));
    const share = await page.locator('#accountLinkURL').inputValue();
    const url = share.match(/https?:\/\/\S+/)[0];
    const meta = (await (await context.request.get(url+'/meta')).json()).data;
    const channels = (await (await context.request.get(base+'/api/scan-channels')).json()).data.channels;
    assert.equal(meta.allow_multi_channel,true);
    await page.locator('#accountLinkDoneBtn').click();
    const pending = new Map(), saved = new Map(); let seq=0, rejected=true, finished=false, lost=false, confirmAttempts=0;
    const tokenPath = new URL(url).pathname;
    await context.route('**'+tokenPath+'/**',async route => {
      const pathname = new URL(route.request().url()).pathname.slice(tokenPath.length);
      if (pathname === '/meta') return route.fulfill(reply({...meta,completed_channels:[...saved].map(([product,account_id]) => ({product,account_id}))}));
      if (pathname === '/finish') {finished=true;return route.fulfill(reply({completed:true}));}
      if (pathname === '/qr') {
        const product = route.request().postDataJSON().product, sid = 'fixture-'+(++seq);
        pending.set(sid,{sid,product,status:'pending'});
        return route.fulfill(reply({session_id:sid,product,image_url:tokenPath+'/qr/'+sid+'/image'}));
      }
      const [,sid,action] = pathname.split('/').filter(Boolean), session = pending.get(sid);
      if (action === 'image') return route.fulfill({status:200,contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="260" height="260"><rect width="260" height="260" fill="#f0f4f5"/><text x="54" y="135" font-size="22" fill="#4e666a">测试二维码</text></svg>'});
      if (action === 'cancel') return route.fulfill(reply({cancelled:true}));
      if (action === 'poll') return route.fulfill(reply({status:session.status}));
      if (action === 'confirm') {
        if (['qbbrowser','gameassist'].includes(session.product) && !route.request().postDataJSON().confirm_same_wechat) return route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({msg:'请确认同一微信，无需重新扫码',data:{identity_confirmation_required:true}})});
        if (session.product === 'pcmgr' && rejected) return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({msg:'请使用原微信扫码：微信身份不一致，账号未修改'})});
        if (!saved.has(session.product)) saved.set(session.product,500+saved.size);
        if (session.product === 'gameassist') {confirmAttempts++;if (!lost){lost=true;return route.abort('failed');}}
        return route.fulfill(reply({id:saved.get(session.product),scan_product:session.product}));
      }
      return route.continue();
    });
    await page.goto(url);
    await page.waitForSelector('.link-channel[data-state=waiting]');
    await page.waitForFunction(n => document.querySelectorAll('.link-channel[data-state=waiting]').length === n, channels.length);
    assert(await page.locator('#linkFinish').isDisabled());
    // A rejected channel stops itself without cancelling other channels.
    [...pending.values()].find(s => s.product === 'pcmgr').status='authorized';
    await page.waitForFunction(() => document.querySelector('[data-product=pcmgr] .state').textContent.includes('身份不一致'));
    assert.equal(await page.locator('.link-channel[data-state=done]').count(),0);
    rejected=false;const beforeRetry=seq;await page.locator('[data-product=pcmgr]').getByRole('button',{name:'重新生成二维码'}).click();
    await page.waitForFunction(n => document.querySelector('[data-product=pcmgr] img')?.getAttribute('src')?.includes('fixture-'+n), beforeRetry+1);
    assert(seq>beforeRetry);
    [...pending.values()].filter(s => s.product === 'pcmgr').at(-1).status='authorized';
    await page.waitForSelector('[data-product=pcmgr][data-state=done]');
    // A lost success response is retried, then represented as one saved channel.
    const beforeGameConsent=seq;
    [...pending.values()].find(s => s.product === 'gameassist').status='authorized';
    await page.waitForSelector('[data-product=gameassist][data-state=identity-confirmation]');
    assert.equal(await page.locator('[data-product=gameassist] .qr img').count(),0);
    assert.match(await page.locator('[data-product=gameassist] .qr').textContent(),/无需重新扫码/);
    assert.equal(await page.locator('[data-product=gameassist] .state').evaluate(el => el.classList.contains('error')),false);
    await page.locator('[data-product=gameassist] button', {hasText:'确认使用同一微信'}).click();
    await page.waitForSelector('[data-product=gameassist][data-state=done]',{timeout:15000});
    assert.equal(seq,beforeGameConsent,'missing GameAssist UnionID must not require another QR');
    assert.equal(confirmAttempts,2);
    assert.equal(saved.size,2);
    // Cross-platform identities require explicit consent using the SAME QR.
    const beforeConsent=seq, qb=[...pending.values()].find(s => s.product === 'qbbrowser');
    qb.status='authorized';
    await page.waitForSelector('[data-product=qbbrowser][data-state=identity-confirmation]');
    assert.equal(saved.has('qbbrowser'),false);
    assert.equal(await page.locator('[data-product=qbbrowser] .qr img').count(),0);
    if(process.env.YYB_TEST_SHOTS){fs.mkdirSync(process.env.YYB_TEST_SHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.YYB_TEST_SHOTS,'identity-consent.png'),fullPage:true});}
    await page.locator('[data-product=qbbrowser] button', {hasText:'确认使用同一微信'}).click();
    await page.waitForSelector('[data-product=qbbrowser][data-state=done]');
    assert.equal(seq,beforeConsent,'consent must not allocate another QR');
    await page.reload();
    await page.waitForSelector('[data-product=pcmgr][data-state=done]');
    assert.equal(await page.locator('.link-channel[data-state=done]').count(),3);
    await page.locator('#linkMultiToggle').uncheck();
    await page.waitForFunction(() => document.querySelectorAll('.link-channel').length === 1);
    await page.locator('#linkMultiToggle').check();
    await page.waitForFunction(n => document.querySelectorAll('.link-channel').length === n, channels.length);
    for (const width of [1280,768,390,320]) {
      await page.setViewportSize({width,height:1000});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),'horizontal overflow at '+width);
      if (process.env.YYB_TEST_SHOTS) {fs.mkdirSync(process.env.YYB_TEST_SHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.YYB_TEST_SHOTS,'link-multi-'+width+'.png'),fullPage:true});}
    }
    await page.locator('#linkFinish').click();
    await page.waitForFunction(() => document.getElementById('state').textContent.includes('链接已关闭'));
    assert(finished);assert(await page.locator('#linkMultiToggle').isDisabled());
    assert.deepEqual(errors,[]);
    await context.close();
    console.log('PASS multi-link registry, identity conflict, explicit same-QR consent, lost-response retry, persisted progress, mode toggle, finish, 320–1280px');
  } finally {await browser.close();}
})().catch(e => {console.error(e);process.exitCode=1;});
