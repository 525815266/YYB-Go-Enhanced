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
    const accounts=[{id:1,openid:'fake-yyb',nickname:'演示 YYB',status:'alive',scan_product:'appstore'},{id:2,openid:'fake-pcmgr',nickname:'演示 PCMgr',status:'alive',scan_product:'pcmgr'},{id:3,openid:'fake-session-only',nickname:'演示授权受限',status:'session_only',scan_product:'appstore',rescan_recommended:true}];
    await context.route('**/accounts',route=>route.fulfill(json(accounts)));
    await context.route('**/accounts/proxy?*',route=>route.fulfill(json({configured:false})));
    await context.route('**/accounts/avatar?*',route=>route.fulfill({status:404,body:''}));
    let refusal='',qrProduct='',lastRequest;
    await context.route('**/wx/**',route=>{
      lastRequest={url:route.request().url(),body:route.request().postDataJSON()};
      return route.fallback();
    });
    await context.route('**/wx/getlatestuserkey',route=>{
      lastRequest={url:route.request().url(),body:route.request().postDataJSON()};
      return route.fulfill(json({result:{data:JSON.stringify({err_no:-12003,err_msg:'invalid api_name'})}}));
    });
    await context.route('**/wx/code',route=>{
      lastRequest={url:route.request().url(),body:route.request().postDataJSON()};
      const failure=refusal==='ticket'?{code:502,msg:'fetch fresh login buffer: PCMgr GetLoginBuffer rejected: code=1',data:null}
        : refusal==='application'?{code:502,msg:'call failed: shortlink application rejected: code=-14',data:{request_rejected:true,upstream_errors:[{stage:'shortlink_application',upstream_code:-14}]}}
        : refusal==='expired'?{code:409,msg:'account expired',data:{rescan_required:true,upstream_errors:[]}}:null;
      return route.fulfill(failure?{status:failure.code,contentType:'application/json',body:JSON.stringify(failure)}:json({result:{code:'fake-login-code'}}));
    });
    await context.route('**/wx/qrcodeauth',route=>{qrProduct=route.request().postDataJSON().product;return route.fulfill(json({session_id:'fake-qr',product:qrProduct}));});
    await context.route('**/wx/oauth',route=>route.fulfill(json({authorization_required:true,code:null,mode:'authorization_url',url:'https://example.test/'})));
    const page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:900});
      await page.goto(base+'/');
      await page.locator('.account-card').last().waitFor();
      assert.match(await page.locator('.account-card').last().innerText(),/需补授权/);
      assert.match(await page.locator('.account-card').last().innerText(),/仅尝试原会话/);
      assert.equal(await page.locator('.account-card').last().locator('.lifecycle-track,.account-status-toggle').count(),0);
      await page.locator('.account-card').nth(1).locator('.account-select').click();
      await page.selectOption('#featureSel','getPhoneNumber');
      assert.match(await page.locator('#featureHint').innerText(),/PCMgr.*1141.*单个目标实测/);
      assert.equal(await page.locator('#testProvider').innerText(),'PCMgr');
      assert.match(await page.locator('#featureSel option:checked').innerText(),/PCMgr.*1141/);
      await page.locator('.account-card').first().locator('.account-select').click();
      assert.match(await page.locator('#featureHint').innerText(),/YYB.*1020/);
      assert.equal(await page.locator('#featureSel').inputValue(),'getPhoneNumber');
      assert.match(await page.locator('#commandPreview').textContent(),/fake-yyb/);
      await page.locator('.account-card').nth(1).locator('.account-select').click();
      await page.selectOption('#featureSel','latestUserKey');
      assert.equal(JSON.parse(await page.locator('#payloadInput').inputValue()).api_name,'webapi_getuserencryptkey');
      await page.click('#callBtn');
      await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.equal(lastRequest.body.payload.api_name,'webapi_getuserencryptkey');
      await page.locator('#testCommand summary').click();
      await page.selectOption('#commandShell','bash');
      const command=await page.locator('#commandPreview').textContent();
      assert.ok(command.includes(base+'/wx/getlatestuserkey'));
      assert.ok(command.includes(JSON.stringify(lastRequest.body)));
      assert.ok(command.includes('${YYB_PROTOCOL_TOKEN}'));
      await page.locator('#protocolTokenInput').fill('fake-ui-token-must-not-be-in-command');
      assert.ok(!(await page.locator('#commandPreview').textContent()).includes('fake-ui-token'));
      assert.match(await page.locator('#resultMeta').innerText(),/上游拒绝.*-12003/);
      assert.equal(await page.locator('#resultBox').evaluate(element=>element.classList.contains('error')),true);
      await page.selectOption('#featureSel','cloud');
      assert.equal(await page.locator('#payloadInput').inputValue(),'');
      const custom={api_name:'custom',data:{kept:true,text:"O'Brien $(echo test)"}};
      await page.locator('#payloadInput').fill(JSON.stringify(custom));
      await page.selectOption('#featureSel','latestUserKey');
      assert.match(await page.locator('#payloadInput').inputValue(),/custom/,'custom payload kept');
      assert.ok((await page.locator('#commandPreview').textContent()).includes("O'\"'\"'Brien $(echo test)"));
      await page.selectOption('#commandShell','powershell');
      assert.ok((await page.locator('#commandPreview').textContent()).includes("O''Brien $(echo test)"));
      assert.ok((await page.locator('#commandPreview').textContent()).includes('$env:YYB_PROTOCOL_TOKEN'));
      await page.click('#callBtn');await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.deepEqual(lastRequest.body.payload,custom);
      await page.locator('#payloadInput').fill('[]');
      assert.equal(await page.locator('#copyCommandBtn').isDisabled(),true);
      assert.match(await page.locator('#commandPreview').textContent(),/必须是对象/);
      await page.locator('#payloadInput').fill('{bad');
      assert.match(await page.locator('#commandPreview').textContent(),/格式不正确/);
      await page.locator('#payloadInput').fill('');
      await page.selectOption('#featureSel','getCode');refusal='ticket';
      await page.click('#callBtn');await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.match(await page.locator('#resultBox').innerText(),/本次接口未执行/);
      assert.equal(await page.locator('#resultMeta').innerText(),'渠道取票据失败');
      refusal='application';
      await page.click('#callBtn');await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.equal(await page.locator('#resultMeta').innerText(),'应用请求被拒绝，账号授权已保留');
      assert.match(await page.locator('#resultBox').innerText(),/shortlink application rejected: code=-14/);
      refusal='expired';
      await page.click('#callBtn');await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.equal(await page.locator('#resultMeta').innerText(),'调用失败 409');
      assert.match(await page.locator('#resultBox').innerText(),/rescan_required/);
      refusal='';
      await page.selectOption('#featureSel','qrcodeauth');await page.click('#callBtn');
      await page.waitForFunction(()=>!document.getElementById('callBtn').disabled);
      assert.equal(qrProduct,'pcmgr');
      assert.match(await page.locator('#commandPreview').textContent(),/"product":"pcmgr"/);
      await page.selectOption('#featureSel','oauth');
      assert.equal(await page.locator('#copyCommandBtn').isDisabled(),true);
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
    console.log(JSON.stringify({capability_hints:true,provider_qr:true,inner_error_feedback:true,oauth_url_only:true,command_body_matches_call:true,bash_powershell_quoting:true,token_not_in_command:true,request_rejection_vs_expiry:true,widths:[1440,390,320]}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
