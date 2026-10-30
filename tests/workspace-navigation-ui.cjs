// Synthetic responses only: no running server, credentials or upstream calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../resource');
const origin = 'http://yyb-issue-lab.test';
const accounts = [
  {id:1,openid:'synthetic-first',nickname:'First test account',status:'alive',scan_product:'appstore'},
  {id:22,openid:'pcmgr:synthetic-selected',nickname:'Selected test account',status:'alive',scan_product:'pcmgr'}
];
(async()=>{
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {
    for (const width of [1440,390]) {
    const context = await browser.newContext({viewport:{width,height:900}});
    const requests = [], errors = [];
    let delayed, releaseRequest;
    await context.route(origin+'/**', async route=>{
      const request = route.request(), url = new URL(request.url());
      if(url.pathname === '/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'templates/index.html'),'utf8')});
      if(url.pathname.startsWith('/static/')) {
        const file = path.join(root, url.pathname.slice(1));
        if(fs.existsSync(file))return route.fulfill({path:file});
      }
      let data = {};
      if(url.pathname === '/accounts')data=accounts;
      else if(url.pathname === '/api/auth/me')data={auth_enabled:true,user:{username:'synthetic-admin',role:'admin'}};
      else if(url.pathname === '/api/version')data={version:'0.2.53'};
      else if(url.pathname === '/accounts/proxy')data={mode:'direct',configured:false};
      else if(url.pathname === '/api/qinglong/config')data={type:'qinglong',configured:false};
      else if(url.pathname === '/wx/code' || url.pathname === '/wx/cloud') {
        requests.push({route:url.pathname,body:request.postDataJSON()});
        if(delayed)await delayed;
        data={result:{code:'synthetic-result'}};
      }
      return route.fulfill({contentType:'application/json',body:JSON.stringify({code:0,msg:'success',data})});
    });
    const page = await context.newPage();
    page.on('pageerror',error=>errors.push(error.message));
    const nav=async label=>{
      if(width<=860)await page.locator('#platformMenu').click();
      await page.locator(`.platform-nav a[data-label="${label}"]`).click();
      assert.equal(await page.locator('#platformMenu').getAttribute('aria-expanded'),'false');
      assert.equal(await page.locator('.platform-page-title').textContent(),label);
      assert.equal(await page.locator('.platform-nav a[aria-current="page"]').getAttribute('data-label'),label);
    };
    const selected=async name=>page.locator('#selectedAccountName').filter({hasText:name}).waitFor();
    await page.goto(origin+'/');
    await selected('First test account');
    await page.evaluate(()=>window.__workspaceIdentity='synthetic-marker');
    await page.locator('#appidInput').fill('wx8d41cdc44c8aeaab');
    await page.locator('#protocolTokenInput').fill('synthetic-memory-token');
    await nav('我的微信账号');
    await page.locator('[data-openid="pcmgr:synthetic-selected"] .account-select').click();
    await selected('Selected test account');
    await nav('接口测试');
    await page.locator('#callBtn').click();
    await page.waitForFunction(()=>!document.querySelector('#callBtn').disabled);
    await nav('我的微信账号');
    const afterNavigation=await page.locator('#selectedAccountName').textContent();
    const appidAfter=await page.locator('#appidInput').inputValue();
    await page.locator('#callBtn').click();
    await page.waitForFunction(()=>!document.querySelector('#callBtn').disabled);
    assert.deepEqual(requests[0],requests[1]);
    assert.equal(afterNavigation,'Selected test account');
    assert.equal(appidAfter,'wx8d41cdc44c8aeaab');
    // Selecting a different account replaces the current entry. Back must use
    // the earlier entry's ref, including the initial default without a ref.
    await page.locator('[data-openid="synthetic-first"] .account-select').click();
    await page.goBack();
    await selected('Selected test account');
    assert.equal(await page.locator('.platform-page-title').textContent(),'接口测试');
    await page.goBack();
    await selected('Selected test account');
    assert.equal(await page.locator('.platform-page-title').textContent(),'我的微信账号');
    await page.goBack();
    await selected('First test account');
    assert.equal(await page.locator('.platform-page-title').textContent(),'我的控制台');
    await page.goForward();
    await selected('Selected test account');
    await page.locator('#featureSel').selectOption('cloud');
    await page.locator('#payloadInput').fill('{"api_name":"synthetic_no_business_action","data":{"example":true}}');
    const payloadBefore=await page.locator('#payloadInput').inputValue();
    delayed=new Promise(resolve=>releaseRequest=resolve);
    await page.locator('#callBtn').click();
    await page.waitForFunction(()=>document.querySelector('#callBtn').disabled);
    await nav('接口测试');
    assert.equal(requests.length,3);
    assert.equal(await page.locator('#featureSel').inputValue(),'cloud');
    assert.equal(await page.locator('#payloadInput').inputValue(),payloadBefore);
    releaseRequest();
    delayed=null;
    await page.waitForFunction(()=>!document.querySelector('#callBtn').disabled);
    assert.equal(requests.length,3);
    assert.equal(await page.evaluate(()=>window.__workspaceIdentity),'synthetic-marker');
    assert.equal(await page.locator('#protocolTokenInput').inputValue(),'synthetic-memory-token');
    assert.equal(new URL(page.url()).searchParams.get('ref'),'22');
    assert.equal(page.url().includes('synthetic-memory-token'),false);
    assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);
    // Modified clicks and links to other pages must retain normal browser
    // behavior. Prevent the default only after recording the handler result.
    const modifiedPrevented=await page.evaluate(()=>{
      let prevented;
      document.addEventListener('click',event=>{prevented=event.defaultPrevented;event.preventDefault();},{once:true});
      document.querySelector('.platform-nav a[href="/"]').dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,ctrlKey:true,button:0}));
      return prevented;
    });
    assert.equal(modifiedPrevented,false);
    if(width<=860)await page.locator('#platformMenu').click();
    await page.locator('.platform-nav a[data-label="运行日志"]').click();
    await page.waitForURL(origin+'/logs');
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({width,requests:requests.length,account_and_target_preserved:true,history_account_restored:true,inflight_not_replayed:true,token_memory_only:true,modified_click_and_other_pages_preserved:true,page_errors:errors}));
    await context.close();
    }
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
