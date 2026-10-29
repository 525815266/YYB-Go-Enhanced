// Uses synthetic accounts and intercepts every protocol/merge request.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');

(async()=>{
  const base=process.env.YYB_TEST_URL;
  if(!base)throw new Error('Set YYB_TEST_URL to an isolated service');
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const context=await browser.newContext({reducedMotion:'reduce'});
    await context.request.post(base+'/login',{data:{username:process.env.YYB_TEST_USER,password:process.env.YYB_TEST_PASS}});
    const json=data=>({status:200,contentType:'application/json',body:JSON.stringify({code:0,msg:'success',data})});
    let accounts=Array.from({length:24},(_,i)=>({id:i+1,nickname:`演示 ${i+1}`,openid:`fake-${i+1}`,scan_product:i===23?'pcmgr':'appstore',status:'alive'}));
    let slow=false,releaseAccounts;
    await context.route('**/accounts',async route=>{if(slow)await new Promise(resolve=>releaseAccounts=resolve);return route.fulfill(json(accounts));});
    await context.route('**/accounts/proxy?*',route=>route.fulfill(json({configured:false})));
    await context.route('**/accounts/avatar?*',route=>route.fulfill({status:404,body:''}));
    await context.route('**/wx/code',route=>route.fulfill(json({code:'synthetic-result'})));
    const requests=[];
    await context.route('**/accounts/merge',route=>{
      const body=route.request().postDataJSON();requests.push(body);
      const source=accounts.find(a=>String(a.id)===body.source_ref),target=accounts.find(a=>String(a.id)===body.target_ref);
      if(!body.confirm)return route.fulfill(json({mode:'preview',preview_key:'fake-preview',source,target,note:'保留目标设置，来源 ID 将删除。'}));
      assert.equal(body.preview_key,'fake-preview');
      const merged={...target,openid:source.openid,scan_product:source.scan_product};
      accounts=accounts.filter(a=>a.id!==source.id).map(a=>a.id===target.id?merged:a);
      return route.fulfill(json({mode:'merged',account:merged,removed_id:source.id}));
    });
    const page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:900});
      await page.goto(base+'/');
      await page.locator('.account-card').last().waitFor();
      await page.locator('#featureSel option').first().waitFor({state:'attached'});
      await page.selectOption('#featureSel','getCode');
      await page.locator('#callBtn').scrollIntoViewIfNeeded();
      const before=await page.evaluate(()=>scrollY);
      const controlTop=await page.locator('#callBtn').evaluate(element=>element.getBoundingClientRect().top);
      assert(before>500,'long account list places test controls below fold');
      slow=true;
      await page.click('#callBtn');
      await page.locator('#resultBox').filter({hasText:'synthetic-result'}).waitFor();
      await page.waitForTimeout(50);
      assert.equal(await page.locator('.account-card').count(),24,'background refresh keeps account list');
      const after=await page.evaluate(()=>scrollY);
      const controlAfter=await page.locator('#callBtn').evaluate(element=>element.getBoundingClientRect().top);
      assert(Math.abs(controlAfter-controlTop)<4,`test result moved viewport ${width}: scroll ${before} → ${after}; control ${controlTop} → ${controlAfter}`);
      releaseAccounts();slow=false;
      await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert(Math.abs((await page.locator('#callBtn').evaluate(element=>element.getBoundingClientRect().top))-controlTop)<4,`refresh moved viewport ${width}`);
      await page.locator('#mergeAccountsBtn').click();
      await page.selectOption('#mergeSourceAccount','24');await page.selectOption('#mergeTargetAccount','1');
      assert.equal(await page.locator('#mergeAccountConfirmBtn').isDisabled(),true);
      await page.click('#mergeAccountPreviewBtn');
      await page.waitForFunction(()=>!document.getElementById('mergeAccountConfirmBtn').disabled);
      assert.match(await page.locator('#mergeAccountPreview').innerText(),/ID 24.*ID 1/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`merge dialog overflow ${width}`);
      await page.selectOption('#mergeTargetAccount','2');
      assert.equal(await page.locator('#mergeAccountConfirmBtn').isDisabled(),true,'selection change invalidates preview');
      await page.click('#mergeAccountClose');
    }
    await page.click('#mergeAccountsBtn');
    await page.selectOption('#mergeSourceAccount','24');await page.selectOption('#mergeTargetAccount','1');
    await page.click('#mergeAccountPreviewBtn');await page.click('#mergeAccountConfirmBtn');
    await page.waitForFunction(()=>document.querySelectorAll('.account-card').length===23);
    assert.equal(await page.locator('.account-card[data-openid="fake-24"] .account-meta').innerText(),'ID 1 · 直连');
    assert.equal(requests.filter(r=>r.confirm).length,1);
    assert.deepEqual(errors,[]);
    console.log('PASS: merge preview/selection/confirmation, preserved target ID, responsive modal and no scroll jump during protocol refresh');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
