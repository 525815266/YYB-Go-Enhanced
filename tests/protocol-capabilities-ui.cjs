// Uses fake accounts and intercepted responses; never calls upstream APIs.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');

(async()=>{
  const base=process.env.YYB_TEST_URL;
  if(!base)throw new Error('Set YYB_TEST_URL to an isolated service');
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const context=await browser.newContext({reducedMotion:'reduce'});
    await context.request.post(base+'/login',{data:{username:process.env.YYB_TEST_USER,password:process.env.YYB_TEST_PASS}});
    const json=data=>({status:200,contentType:'application/json',body:JSON.stringify({code:0,msg:'success',data})});
    const accounts=[{id:1,openid:'fake-yyb',nickname:'演示 YYB',status:'alive',scan_product:'appstore'},{id:2,openid:'fake-pcmgr',nickname:'演示 PCMgr',status:'alive',scan_product:'pcmgr'}];
    await context.route('**/accounts',route=>route.fulfill(json(accounts)));
    await context.route('**/accounts/proxy?*',route=>route.fulfill(json({configured:false})));
    await context.route('**/accounts/avatar?*',route=>route.fulfill({status:404,body:''}));
    let refusal=false,qrProduct='';
    await context.route('**/wx/getlatestuserkey',route=>{
      assert.equal(route.request().postDataJSON().payload.api_name,'webapi_getuserencryptkey');
      return route.fulfill(json({result:{data:JSON.stringify({err_no:-12003,err_msg:'invalid api_name'})}}));
    });
    await context.route('**/wx/code',route=>route.fulfill(refusal?{status:502,contentType:'application/json',body:JSON.stringify({code:502,msg:'fetch fresh login buffer: PCMgr GetLoginBuffer rejected: code=1',data:null})}:json({result:{code:'fake-login-code'}})));
    await context.route('**/wx/qrcodeauth',route=>{qrProduct=route.request().postDataJSON().product;return route.fulfill(json({session_id:'fake-qr',product:qrProduct}));});
    await context.route('**/wx/oauth',route=>route.fulfill(json({authorization_required:true,code:null,mode:'authorization_url',url:'https://example.test/'})));
    const page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:900});
      await page.goto(base+'/');
      await page.locator('.account-card').last().waitFor();
      await page.locator('.account-card').nth(1).locator('.account-select').click();
      await page.selectOption('#featureSel','getPhoneNumber');
      assert.match(await page.locator('#featureHint').innerText(),/PCMgr.*1141.*待实测/);
      await page.locator('.account-card').first().locator('.account-select').click();
      assert.match(await page.locator('#featureHint').innerText(),/YYB.*1020/);
      await page.locator('.account-card').nth(1).locator('.account-select').click();
      await page.selectOption('#featureSel','latestUserKey');
      assert.equal(JSON.parse(await page.locator('#payloadInput').inputValue()).api_name,'webapi_getuserencryptkey');
      await page.click('#callBtn');
      await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.match(await page.locator('#resultMeta').innerText(),/上游拒绝.*-12003/);
      assert.equal(await page.locator('#resultBox').evaluate(element=>element.classList.contains('error')),true);
      await page.selectOption('#featureSel','cloud');
      assert.equal(await page.locator('#payloadInput').inputValue(),'');
      await page.locator('#payloadInput').fill('{"api_name":"custom","data":{"kept":true}}');
      await page.selectOption('#featureSel','latestUserKey');
      assert.match(await page.locator('#payloadInput').inputValue(),/custom/,'custom payload kept');
      await page.selectOption('#featureSel','getCode');refusal=true;
      await page.click('#callBtn');await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.match(await page.locator('#resultBox').innerText(),/本次接口未执行/);
      assert.equal(await page.locator('#resultMeta').innerText(),'渠道取票据失败');
      refusal=false;
      await page.selectOption('#featureSel','qrcodeauth');await page.click('#callBtn');
      await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.equal(qrProduct,'pcmgr');
      await page.selectOption('#featureSel','oauth');
      await page.locator('#oauthRedirectInput').fill('https://example.test/callback');
      await page.click('#callBtn');await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.match(await page.locator('#resultMeta').innerText(),/已生成链接，待微信授权/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`overflow ${width}`);
      if(process.env.YYB_SCREENSHOT_DIR){
        fs.mkdirSync(process.env.YYB_SCREENSHOT_DIR,{recursive:true});
        await page.locator('#featureSel').scrollIntoViewIfNeeded();
        await page.screenshot({path:path.join(process.env.YYB_SCREENSHOT_DIR,`protocol-demo-${width}.png`)});
      }
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({capability_hints:true,provider_qr:true,inner_error_feedback:true,oauth_url_only:true,custom_payload_kept:true,widths:[1440,390,320]}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
