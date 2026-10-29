// Run against an isolated test service. QR responses below use only fake data.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async()=>{
  const base=process.env.YYB_TEST_URL;
  if(!base) throw new Error('Set YYB_TEST_URL to an isolated service');
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try {
    const context=await browser.newContext();
    const login=await context.request.post(base+'/login',{data:{username:process.env.YYB_TEST_USER,password:process.env.YYB_TEST_PASS}});
    assert.equal((await login.json()).code,0);
    const page=await context.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/settings');
    await page.locator('#globalScanField').waitFor({state:'visible'});
    await page.selectOption('#globalScan','pcmgr');
    await page.selectOption('#personalScan','');
    await page.click('#saveScan');
    await page.locator('#scanNotice').filter({hasText:'已保存'}).waitFor();
    let settings=(await (await context.request.get(base+'/api/scan-settings')).json()).data;
    assert.equal(settings.effective,'pcmgr');
    await page.selectOption('#personalScan','appstore');
    await page.click('#saveScan');
    await page.waitForFunction(()=>!document.getElementById('saveScan').disabled);
    settings=(await (await context.request.get(base+'/api/scan-settings')).json()).data;
    assert.equal(settings.effective,'appstore');
    const json=data=>({status:200,contentType:'application/json',body:JSON.stringify({code:0,msg:'success',data})});
    let sequence=0,selected=[],cancelled=[],releaseOldPoll;
    await context.route('**/qr?as_base64=true',route=>{
      const body=route.request().postDataJSON();selected.push(body.product);sequence++;
      return route.fulfill(json({session_id:'fake-'+sequence,product:body.product,image_base64:'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="280" height="280"><rect width="280" height="280" fill="%23eeeeee"/></svg>'}));
    });
    await context.route('**/qr/*/cancel',route=>{cancelled.push(route.request().url());return route.fulfill(json({cancelled:true}));});
    await context.route('**/qr/*/poll',async route=>{
      if(route.request().url().includes('/fake-1/')) await new Promise(resolve=>releaseOldPoll=resolve);
      return route.fulfill(json({status:'pending'}));
    });
    await page.goto(base+'/scan');
    await page.waitForFunction(()=>document.querySelector('#qrWrap img'));
    assert.equal(selected.at(-1),'appstore');
    await page.waitForFunction(()=>!!pollBusy);
    await page.selectOption('#scanProduct','pcmgr');
    await page.waitForFunction(()=>sid==='fake-2');
    releaseOldPoll();
    await page.waitForTimeout(100);
    assert.equal(selected.at(-1),'pcmgr');
    assert(cancelled.some(url=>url.includes('/fake-1/')));
    assert.equal(await page.evaluate(()=>sid),'fake-2');
    assert.equal(await page.locator('#quickPane').isVisible(),false);
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`scan overflow ${width}`);
    }
    // Switching an update link requires explicit consent before allocating a QR.
    let update=true,linkSequence=0,linkBodies=[];
    await context.route('**/a/fake-demo',route=>route.fulfill({status:200,contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../resource/templates/account-link.html'),'utf8')}));
    await context.route('**/account-link/fake-demo/meta',route=>route.fulfill(json({product:'pcmgr',source_product:'pcmgr',switch_confirmed:false,locked:false,kind:update?'update':'add',target:'演示账号',account_id:42,expires_at:Math.floor(Date.now()/1000)+600})));
    await context.route('**/account-link/fake-demo/qr',route=>{linkBodies.push(route.request().postDataJSON());return route.fulfill(json({session_id:'link-'+(++linkSequence),link_expires_at:Math.floor(Date.now()/1000)+600}));});
    await context.route('**/account-link/fake-demo/qr/*/poll',route=>route.fulfill(json({status:'pending'})));
    await context.route('**/account-link/fake-demo/qr/*/cancel',route=>route.fulfill(json({cancelled:true})));
    await page.goto(base+'/a/fake-demo');
    await page.locator('#intro').filter({hasText:'更新 演示账号'}).waitFor();
    await page.waitForFunction(()=>sid==='link-1');
    assert.equal(await page.locator('#scanProduct').isDisabled(),false);
    await page.selectOption('#scanProduct','appstore');
    await page.locator('.scan-switch-dialog').waitFor();
    assert.match(await page.locator('.scan-switch-dialog').innerText(),/无法仅凭 OpenID/);
    assert.equal(linkSequence,1,'no QR allocated before consent');
    await page.getByRole('button',{name:'保留原渠道',exact:true}).click();
    assert.equal(await page.locator('#scanProduct').inputValue(),'pcmgr');
    assert.equal(linkSequence,1,'cancelling preserves original QR');
    await page.selectOption('#scanProduct','appstore');
    await page.getByRole('button',{name:'确认切换',exact:true}).click();
    await page.waitForFunction(()=>sid==='link-2');
    assert.deepEqual(linkBodies.at(-1),{product:'appstore',confirm_switch:true});
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`update overflow ${width}`);
    }
    update=false;
    await page.reload();
    await page.locator('#intro').filter({hasText:'可扫码添加'}).waitFor();
    assert.equal(await page.locator('#scanProduct').isDisabled(),false);
    await page.selectOption('#scanProduct','appstore');
    await page.waitForFunction(()=>sid==='link-4');
    assert.equal(await page.locator('.scan-switch-dialog').count(),0,'new account channel needs no rebinding confirmation');
    assert.deepEqual(errors,[]);
    console.log('PASS scan channels: defaults, permissions UI, cancellation, stale polling, mobile and short links');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
