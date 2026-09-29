const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.YYB_PLAYWRIGHT || 'playwright');
const base = process.env.YYB_UI_TEST_URL || 'http://127.0.0.1:18018';
assert(['127.0.0.1','localhost','[::1]'].includes(new URL(base).hostname), 'Only loopback test instances are allowed');
const output = process.env.YYB_UI_TEST_OUTPUT || '.codex-tmp/vben-native-20260929/screenshots';
const accounts = Array.from({length:18}, (_, index) => ({id:index+1,openid:`test-openid-${index+1}`,nickname:['张三','李四','王五'][index%3]+(index>2?index+1:''),remark:'',status:index===1?'expired':'alive'}));
const mutations = [];
let enabled = true, logRequests = 0, logFailure = false, qrFailure = false, empty = false;
async function fixture(page) {
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method();
    if (url.origin !== new URL(base).origin) return route.abort();
    const body = request.postDataJSON(), p = url.pathname;
    const reply = data => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({code:0,msg:'success',data})});
    if (p === '/accounts') return reply(empty ? [] : accounts);
    if (p === '/accounts/avatar' || p === '/favicon.ico') return route.fulfill({status:204,body:''});
    if (['/accounts/remark','/api/qinglong/sync','/api/qinglong/sync-all','/accounts/refresh','/accounts/resync'].includes(p)) { mutations.push({path:p,body}); if(p==='/accounts/remark') accounts.find(a=>String(a.id)===body.ref).remark=body.remark; return reply({account:body}); }
    if (p === '/accounts/proxy') { if (method === 'PUT') mutations.push({path:p,body}); return reply(body ? {...body,configured:body.mode!=='direct'} : {mode:'direct',configured:false,refresh_ahead_minutes:5}); }
    if (p === '/api/proxy-profiles' || p === '/api/account-links') return reply([]);
    if (p === '/api/proxy-profiles/areas/provinces') return reply([{code:'all',name:'全国'}]);
    if (p === '/api/qinglong/config') return reply({type:'qinglong',configured:true,url:'http://test-panel:5700'});
    if (p === '/api/qinglong/status') return reply({connected:true,configured:true});
    if (p === '/api/qinglong/jobs') return reply({jobs:[{name:'测试签到',script_key:'test_sign.py',schedule:'0 9 * * *',provisioned:true,enabled,running:false}]});
    if (p === '/api/qinglong/runs') return reply({runs:[{name:'测试签到',script_key:'test_sign.py',log_key:`log-${url.searchParams.get('ref')}`,started_at:Math.floor(Date.now()/1000),size:10240,status:'运行中',running:true}]});
    if (p === '/api/qinglong/runs/log') {
      if (logFailure) return route.fulfill({status:200,contentType:'application/json',body:''});
      ++logRequests; return reply({log:Array.from({length:180+logRequests},(_,i)=>`账号 ${body.ref} 测试日志 ${i}`).join('\n')});
    }
    if (p === '/api/qinglong/jobs/run' || p === '/api/qinglong/jobs/enable') { mutations.push({path:p,body}); if ('enabled' in body) enabled=body.enabled; return reply({submitted_at:Math.floor(Date.now()/1000)}); }
    if (p === '/api/qinglong/push') { if (method==='PUT') mutations.push({path:p,body}); return reply(body || {channel:'none',token_configured:false}); }
    if (p === '/api/maintenance') return reply({version:'test',latest_version:'test',runtime:{label:'隔离实例'}});
    if (p === '/qr' && method === 'POST') {
      if (qrFailure) return route.fulfill({status:504,contentType:'application/json',body:JSON.stringify({code:504,msg:'测试网络超时',data:null})});
      return reply({session_id:'test-session',image_base64:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='});
    }
    if (p.startsWith('/qr/test-session')) return reply({status:'pending'});
    if (p.startsWith('/wx')) { mutations.push({path:p,body}); return reply({code:'test-code'}); }
    return route.continue();
  });
}
async function login(page, user='uiadmin', password='ui-test-password') {
  await page.goto(base+'/console/login');
  await page.locator('#username').fill(user); await page.locator('#password').fill(password);
  await page.locator('button[type=submit]').click();
  await page.locator('[data-layout-region=layout]').waitFor();
}
async function shot(page, name) {
  await page.waitForTimeout(180);
  const dimensions = await page.evaluate(() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,buttons:[...document.querySelectorAll('button')].filter(b=>b.clientWidth>0 && b.scrollWidth>b.clientWidth+3).map(b=>b.textContent)}));
  assert(dimensions.scroll<=dimensions.width+1, name+': page overflows '+JSON.stringify(dimensions));
  assert.equal(dimensions.buttons.length,0,name+': button content overflows '+JSON.stringify(dimensions.buttons));
  await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
}
function field(page, text) { return page.locator('.ant-form-item').filter({has:page.locator('label').filter({hasText:new RegExp('^'+text+'$')})}).locator('input').first(); }
async function matrix(browser) {
  let pages=0;
  for (const [name, width] of [['desktop',1440],['tablet',768],['mobile',390],['narrow',320]]) {
    const context=await browser.newContext({viewport:{width,height:900}}), page=await context.newPage(), errors=[];
    page.on('pageerror',error=>errors.push(error.message)); await fixture(page); await login(page);
    await page.locator('.account-row').first().waitFor(); await shot(page,name+'-accounts'); ++pages;
    await page.getByRole('searchbox').count();
    await page.getByRole('textbox',{name:'搜索账号'}).fill('王五'); assert.equal(await page.locator('.account-row').count(),6);
    await page.getByRole('textbox',{name:'搜索账号'}).fill('');
    if(width<768) { await page.getByRole('button',{name:'打开导航',exact:true}).click(); await page.locator('[data-layout-region=sidebar-mask]').waitFor(); await page.keyboard.press('Escape'); await page.locator('[data-layout-region=sidebar-mask]').waitFor({state:'hidden'}); }
    else { await page.getByRole('button',{name:'切换导航'}).click(); assert.equal(await page.locator('[data-layout-region=layout]').getAttribute('data-sidebar-collapsed'),'true'); await page.reload(); await page.locator('.account-row').first().waitFor(); assert.equal(await page.locator('[data-layout-region=layout]').getAttribute('data-sidebar-collapsed'),'true'); await page.getByRole('button',{name:'切换导航'}).click(); }
    for(const route of ['users','settings','runs?ref=3','scan','proxies?ref=3','calls','account-links','maintenance']) {
      await page.goto(base+'/console/'+route); await page.locator('[data-layout-region=layout]').waitFor();
      if(await page.locator('iframe').count()) await page.frameLocator('iframe').locator('.platform-shell').waitFor();
      else await page.locator('.ant-skeleton').waitFor({state:'hidden'});
      await shot(page,name+'-'+route.replace('?','-').replace('=','-')); ++pages;
      if(route==='users') { await page.getByRole('button',{name:'新增用户',exact:true}).click(); assert.equal(await page.locator('section[aria-label="新增用户"]').count(),1); await shot(page,name+'-create-user'); }
      if(route==='runs?ref=3') {
        await page.getByRole('tab',{name:/运行日志/}).click(); await page.getByRole('button',{name:'查看日志'}).click(); await page.locator('.log-text').filter({hasText:'账号 3 测试日志'}).waitFor();
        await page.locator('.log-text').evaluate(el=>el.scrollTop=220); const top=await page.locator('.log-text').evaluate(el=>el.scrollTop);
        const requests=logRequests; await page.waitForFunction(n=>document.querySelector('.log-text').textContent.split('\n').length>180+n,requests);
        assert.equal(await page.locator('.log-text').evaluate(el=>el.scrollTop),top,'automatic refresh moved log scroll');
        await shot(page,name+'-logs'); await page.locator('.ant-drawer-close').click();
      }
    }
    assert.deepEqual(errors,[],name+': JavaScript errors'); await context.close();
  }
  return pages;
}
async function interactions(browser) {
  const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'}),page=await context.newPage();
  await fixture(page); await page.goto(base+'/console/accounts'); await page.waitForURL(url=>url.pathname==='/console/login',{waitUntil:'commit'}); await page.locator('#username').fill('uiadmin'); await page.locator('#password').fill('wrong-password'); await page.locator('button[type=submit]').click(); await page.getByRole('alert').waitFor(); assert(await page.locator('button[type=submit]').isEnabled());
  await login(page);
  await page.locator('#account-3 button[title="代理设置"]').click(); await page.waitForURL(url => url.pathname === '/console/proxies', {waitUntil:'commit'}); assert(new URL(page.url()).searchParams.get('ref')==='3');
  const frame=page.frameLocator('iframe'); await frame.locator('#proxyAccountName').filter({hasText:'王五'}).waitFor();
  await frame.locator('label[for=proxyModeStatic]').click(); await frame.locator('#staticProxy').fill('user:pass@127.0.0.1:8080'); await frame.locator('#saveProxy').click(); await frame.locator('#proxyFeedback').filter({hasText:'已保存'}).waitFor(); assert.equal(mutations.at(-1).body.ref,'3');
  await frame.locator('#backToAccount').click(); await page.locator('#account-3').waitFor(); assert(new URL(page.url()).pathname==='/console/accounts'); assert(new URL(page.url()).searchParams.get('ref')==='3');
  await page.locator('#account-3 button[title="修改备注"]').click(); await field(page,'备注').fill('王五的测试备注'); await page.getByRole('button',{name:'保存备注'}).click(); await page.locator('#account-3').filter({hasText:'王五的测试备注'}).waitFor(); assert(mutations.some(m=>m.path==='/accounts/remark'&&m.body.ref==='3'));
  await page.goto(base+'/console/calls'); await page.locator('#call-appid').fill('wx-test-app'); await page.getByRole('button',{name:'执行调用'}).click(); await page.locator('.result-text').filter({hasText:'test-code'}).waitFor(); assert(mutations.some(m=>m.path==='/wx/code'&&m.body.ref==='test-openid-1'&&m.body.app_id==='wx-test-app'));
  await page.goto(base+'/console/settings'); await field(page,'显示名称').fill('隔离测试管理员'); await page.getByRole('button',{name:'保存基本信息'}).click(); await page.locator('.user-name').filter({hasText:'隔离测试管理员'}).waitFor();
  await page.goto(base+'/console/runs?ref=3'); await page.getByRole('switch',{name:'启用测试签到'}).click(); await page.waitForFunction(()=>document.querySelector('[role=switch][aria-label="启用测试签到"]')?.getAttribute('aria-checked')==='false');
  assert(mutations.some(m=>m.path==='/api/qinglong/jobs/enable'&&m.body.ref==='3'&&!m.body.enabled));
  await page.getByRole('button',{name:'运行账号 3'}).click(); await page.getByRole('button',{name:'查看日志'}).waitFor(); assert(mutations.some(m=>m.path==='/api/qinglong/jobs/run'&&m.body.ref==='3'));
  await page.getByRole('button',{name:'查看日志'}).click(); await page.locator('.log-text').filter({hasText:'测试日志'}).waitFor(); logFailure=true; await page.getByRole('button',{name:'刷新日志'}).click(); await page.getByRole('alert').filter({hasText:'空响应'}).waitFor(); logFailure=false; await page.getByRole('button',{name:'刷新日志'}).click(); await page.getByRole('alert').filter({hasText:'空响应'}).waitFor({state:'hidden'}); await page.locator('.ant-drawer-close').click();
  await page.getByRole('button',{name:'配置推送'}).click(); await page.locator('.ant-select').nth(1).click(); await page.getByTitle('PushPlus',{exact:true}).click(); await field(page,'Token').fill('test-token'); await page.getByRole('button',{name:'保存推送设置'}).click(); await page.waitForTimeout(250); assert(mutations.some(m=>m.path==='/api/qinglong/push'&&m.body.ref==='3'&&m.body.token==='test-token'));
  await page.goto(base+'/console/scan'); await page.frameLocator('iframe').locator('#qrWrap img').waitFor(); qrFailure=true; await page.frameLocator('iframe').locator('#refreshBtn').click(); await page.frameLocator('iframe').locator('#status').filter({hasText:'获取二维码失败'}).waitFor(); qrFailure=false; await page.frameLocator('iframe').locator('#refreshBtn').click(); await page.frameLocator('iframe').locator('#qrWrap img').waitFor();
  await page.goto(base+'/console/users'); await page.getByRole('button',{name:'新增用户',exact:true}).click(); const member='vben-member-'+Date.now(); await field(page,'用户名').fill(member); await field(page,'显示名称').fill('测试普通用户'); await field(page,'初始密码').fill('member-test-password'); await page.getByRole('button',{name:'创建用户',exact:true}).click(); await page.locator('section[aria-label="新增用户"]').waitFor({state:'hidden'}); await page.getByRole('textbox',{name:'搜索用户'}).fill(member); await page.locator('tbody tr').filter({hasText:member}).waitFor();
  await page.getByRole('button',{name:'退出登录',exact:true}).click(); await page.locator('#username').waitFor(); await login(page,member,'member-test-password');
  await page.getByRole('button',{name:'打开导航',exact:true}).click(); assert.equal(await page.getByRole('menuitem',{name:'用户管理'}).count(),0); await page.keyboard.press('Escape');
  assert.equal((await context.request.get(base+'/api/auth/users')).status(),403); await page.goto(base+'/console/users'); await page.waitForURL('**/console/accounts');
  empty=true; await page.reload(); await page.getByText('尚未添加微信账号',{exact:true}).waitFor(); await shot(page,'mobile-empty-accounts'); empty=false;
  await context.close();
}
(async()=>{
  fs.mkdirSync(output,{recursive:true}); const browser=await chromium.launch({executablePath:process.env.YYB_BROWSER_PATH||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try { const pages=process.env.YYB_UI_TEST_INTERACTIONS_ONLY ? 0 : await matrix(browser); await interactions(browser); fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({passed:true,pages,logRequests,mutations},null,2)); console.log(JSON.stringify({passed:true,pages,logRequests,mutations:mutations.length,output})); }
  finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1});
