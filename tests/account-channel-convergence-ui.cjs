// Isolated service only. All account/config/QR responses are synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.YYB_TEST_URL;
if (!base) throw new Error('Set YYB_TEST_URL to an isolated service');
const respond = (route,data,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify({code:status===200?0:status,data,msg:status===200?'ok':'合成读取失败'})});
(async () => {
  const browser = await chromium.launch({headless:true,...(process.env.YYB_TEST_CHROME ? {executablePath:process.env.YYB_TEST_CHROME} : {})});
  try {
    const context = await browser.newContext({viewport:{width:1280,height:1000},reducedMotion:'reduce'});
    assert.equal((await (await context.request.post(base+'/login',{data:{username:process.env.YYB_TEST_USER,password:process.env.YYB_TEST_PASS}})).json()).code,0);
    const accounts = [1,2].map(id=>({id,openid:'synthetic-'+id,nickname:'合成账号 '+id,scan_product:'appstore',status:'alive',fleet_primary_id:1,created_at:1,updated_at:1}));
    let master=true, preferences={1:true,2:false}, writes=0, reads=0, failRead=false, failSave=false;
    await context.route('**/accounts?*',route=>respond(route,accounts));
    await context.route('**/accounts/proxy?*',route=>respond(route,{mode:'direct',configured:false,keepalive_supported:true}));
    await context.route('**/accounts/circuit-settings?*',route=>{
      reads++; const id=new URL(route.request().url()).searchParams.get('ref');
      if (failRead) return respond(route,null,500);
      if (route.request().method()==='PUT') {if(failSave)return respond(route,null,500); writes++;preferences[id]=route.request().postDataJSON().enabled;}
      return respond(route,{global_enabled:master,account_enabled:preferences[id],effective:master&&preferences[id]});
    });
    const page = await context.newPage(), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base+'/?ref=1');
    await page.waitForFunction(()=>!document.querySelector('#saveAccountCircuit').disabled);
    assert(await page.locator('#accountCircuitEnabled').isChecked());
    // Cross-tab notification must redraw the effective state, keeping preference.
    const sibling=await context.newPage();await sibling.goto(base+'/settings');
    master=false;
    await sibling.evaluate(()=>localStorage.setItem('yyb-circuit-settings-changed',String(Date.now())));
    await page.waitForFunction(()=>document.querySelector('#accountCircuitStatus').textContent.includes('总控已关闭'));
    assert.equal(await page.locator('#accountCircuitEnabled').isChecked(),false);
    assert(await page.locator('#accountCircuitEnabled').isDisabled());
    assert(await page.locator('#saveAccountCircuit').isDisabled());
    assert.equal(preferences[1],true);assert.equal(writes,0);
    if(process.env.YYB_TEST_SHOTS){fs.mkdirSync(process.env.YYB_TEST_SHOTS,{recursive:true});await page.locator('#accountCircuitStatus').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(process.env.YYB_TEST_SHOTS,'circuit-disabled.png')});}
    master=true;
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    await page.waitForFunction(()=>!document.querySelector('#saveAccountCircuit').disabled);
    assert(await page.locator('#accountCircuitEnabled').isChecked());
    failSave=true;await page.locator('#accountCircuitEnabled').uncheck();await page.locator('#saveAccountCircuit').click();
    await page.waitForFunction(()=>document.querySelector('#accountCircuitStatus').textContent.includes('保存失败'));
    assert(await page.locator('#accountCircuitEnabled').isChecked());
    failSave=false;await page.locator('#accountCircuitEnabled').uncheck();await page.locator('#saveAccountCircuit').click();
    await page.waitForFunction(()=>!document.querySelector('#saveAccountCircuit').disabled);
    assert.equal(preferences[1],false);assert.equal(writes,1);
    failRead=true;await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    await page.waitForFunction(()=>document.querySelector('#accountCircuitStatus').textContent.includes('读取失败'));
    assert.equal(await page.locator('#accountCircuitEnabled').isChecked(),false);
    assert(await page.locator('#accountCircuitEnabled').isDisabled());
    failRead=false;
    await page.goto(base+'/?ref=2');
    await page.locator('#appidInput').fill('wxsynthetic');
    await page.waitForFunction(()=>document.querySelector('#commandPreview').textContent.includes('"ref":"1"'));
    let ordinaryQR=0, linkRequests=[], failLink=false;
    await context.route('**/qr?*',route=>{ordinaryQR++;return respond(route,null,500)});
    await context.route('**/api/account-links',route=>{
      if(route.request().method()!=='POST')return route.continue();
      linkRequests.push(route.request().postDataJSON());
      if(failLink)return respond(route,null,409);
      return respond(route,{url:base+'/account-link/synthetic-update'});
    });
    await context.route('**/account-link/synthetic-update',route=>route.fulfill({status:200,contentType:'text/html',body:'<p>合成更新链接</p>'}));
    await page.goto(base+'/scan?ref=2');
    await page.waitForURL(base+'/account-link/synthetic-update');
    assert.deepEqual(linkRequests,[{kind:'update',ref:'1',allow_multi_channel:true}]);
    assert.equal(ordinaryQR,0,'member supplement must route via root without creating a standalone QR');
    failLink=true;
    await page.goto(base+'/scan?ref=1');
    await page.locator('#qrWrap').filter({hasText:'未创建新的扫码授权'}).waitFor();
    assert(await page.locator('#multiScanSwitch').isDisabled());assert.equal(ordinaryQR,0);
    for(const width of [1280,768,390,320]){
      await page.setViewportSize({width,height:1000});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'overflow at '+width);
      if(process.env.YYB_TEST_SHOTS){fs.mkdirSync(process.env.YYB_TEST_SHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.YYB_TEST_SHOTS,'convergence-'+width+'.png'),fullPage:true});}
    }
    assert(reads>3);assert.deepEqual(errors,[]);
    await context.close();
    console.log('PASS circuit master precedence, cross-tab/focus, preference restore, failures; member supplement uses primary update link; failed link creates no QR; 320–1280px');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
