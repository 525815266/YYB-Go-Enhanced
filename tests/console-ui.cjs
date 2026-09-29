const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.YYB_PLAYWRIGHT || 'playwright');

const base = process.env.YYB_UI_TEST_URL || 'http://127.0.0.1:18016';
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Only isolated loopback test servers are allowed');
const output = process.env.YYB_UI_TEST_OUTPUT || '.codex-tmp/vben-test-20260929/screenshots';
const username = process.env.YYB_UI_TEST_USER || 'uiadmin';
const password = process.env.YYB_UI_TEST_PASSWORD || 'ui-test-password';
const browserPath = process.env.YYB_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const now = Math.floor(Date.now() / 1000);
const accounts = Array.from({ length: 18 }, (_, index) => ({
  id: index + 1,
  openid: 'test-openid-' + (index + 1),
  nickname: ['张三', '李四', '王五'][index % 3] + (index > 2 ? index + 1 : ''),
  remark: index === 2 ? '王五的备用微信' : '',
  uin: 10000 + index,
  status: index === 1 ? 'expired' : index === 4 ? 'unknown' : 'alive',
  rescan_recommended: index === 3,
  last_checked_at: now,
  refresh_token_observed_at: now - 86400,
  credential_expires_at: now + 7200,
  credential_expires_in: 7200
}));
let currentAccounts = accounts;
const mutations = [];
let logRequests = 0;
let jobEnabled = true;
let qrFailure = false;
let logFailure = false;
const proxyConfigs = new Map();
const defaultProxy = { mode: 'direct', proxy_type: 'http', configured: false, refresh_ahead_minutes: 5 };
const job = () => ({ name: '测试签到', script_key: 'test_sign.py', schedule: '0 9 * * *', provisioned: true, enabled: jobEnabled, running: false });
const run = { name: '测试签到', script_key: 'test_sign.py', log_key: 'test-log-1', started_at: now, size: 5120, status: '已完成', running: false };

async function fixtures(page) {
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== new URL(base).origin) return route.abort();
    const pathname = url.pathname;
    const method = request.method();
    const body = request.postDataJSON();
    const reply = data => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'success', data }) });
    if (pathname === '/accounts') return reply(currentAccounts);
    if (pathname === '/accounts/avatar') return route.fulfill({ status: 404, body: '' });
    if (pathname === '/accounts/proxy') {
      if (method === 'PUT') { mutations.push({ path: pathname, body }); proxyConfigs.set(String(body.ref), { ...body, configured: body.mode !== 'direct' }); return reply(proxyConfigs.get(String(body.ref))); }
      return reply(proxyConfigs.get(url.searchParams.get('ref')) || defaultProxy);
    }
    if (pathname === '/accounts/remark') { mutations.push({ path: pathname, body }); const account = accounts.find(account => String(account.id) === body.ref); account.remark = body.remark; return reply({ account }); }
    if (pathname === '/api/proxy-profiles' || pathname === '/api/account-links') return reply([]);
    if (pathname === '/api/proxy-profiles/areas/provinces') return reply([{ code: 'all', name: '全国' }, { code: '370000', name: '山东' }]);
    if (pathname === '/api/qinglong/config') return reply({ type: 'qinglong', configured: true, url: 'http://test-panel:5700' });
    if (pathname === '/api/qinglong/status') return reply({ connected: true, configured: true });
    if (pathname === '/api/qinglong/jobs') return reply({ jobs: [job()] });
    if (pathname === '/api/qinglong/jobs/enable') { mutations.push({ path: pathname, body }); jobEnabled = body.enabled; return reply({ enabled: body.enabled }); }
    if (pathname === '/api/qinglong/jobs/run') { mutations.push({ path: pathname, body }); return reply({ submitted_at: now - 1 }); }
    if (pathname === '/api/qinglong/runs') return reply({ runs: [run] });
    if (pathname === '/api/qinglong/runs/log') {
      if (logFailure) return route.fulfill({ status: 200, contentType: 'application/json', body: '' });
      logRequests += 1;
      return reply({ log: Array.from({ length: 100 + logRequests }, (_, index) => '账号 1 测试日志 ' + index).join('\n') });
    }
    if (pathname === '/api/qinglong/push') {
      if (method === 'PUT') mutations.push({ path: pathname, body });
      return reply(body || { channel: 'none', token_configured: false });
    }
    if (pathname === '/api/maintenance') return reply({ version: '0.2.14', latest_version: '0.2.15', has_update: true, managed_update: true, runtime: { label: '测试实例', managed_update: true }, agent: { job: { running: false } } });
    if (pathname === '/qr' && method === 'POST') {
      if (qrFailure) return route.fulfill({ status: 504, contentType: 'application/json', body: JSON.stringify({ code: 504, msg: '测试网络超时', data: null }) });
      return reply({ session_id: 'test-session', image_base64: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=' });
    }
    if (pathname === '/qr/test-session/poll') return reply({ status: 'pending' });
    if (pathname === '/qr/test-session/cancel') return reply({});
    if (pathname.startsWith('/wx') || pathname.startsWith('/quick-login')) return reply({});
    return route.continue();
  });
}

async function login(page) {
  await page.goto(base + '/login');
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(password);
  await page.locator('#submit').click();
  await page.locator('.platform-shell').waitFor();
  await page.waitForFunction(() => document.querySelector('#platformUserName')?.textContent !== '正在读取');
}

async function layout(page, name) {
  const result = await page.evaluate(() => ({
    width: innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    main: document.querySelector('.platform-main > main')?.getBoundingClientRect().toJSON(),
    badButtons: [...document.querySelectorAll('button')].filter(button => !button.closest('[hidden]') && getComputedStyle(button).display !== 'none' && button.clientWidth > 0 && button.scrollWidth > button.clientWidth + 3).map(button => button.id || button.textContent.slice(0, 30)),
    nestedButtons: document.querySelectorAll('button button').length
  }));
  assert(result.scrollWidth <= result.width + 1, name + ': horizontal overflow ' + JSON.stringify(result));
  assert.equal(result.nestedButtons, 0, name + ': nested interactive buttons');
  assert.equal(result.badButtons.length, 0, name + ': button text overflow ' + result.badButtons.join(','));
  await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true });
  return result;
}

async function runTests(browser) {
  const summary = [];
  for (const [name, viewport] of [
    ['desktop', { width: 1440, height: 960 }],
    ['tablet', { width: 768, height: 1024 }],
    ['mobile', { width: 390, height: 844 }],
    ['narrow', { width: 320, height: 720 }]
  ]) {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await fixtures(page);
    await login(page);
    await page.locator('.account-card').first().waitFor();
    assert.equal(await page.locator('.account-card.expired').evaluate(element => getComputedStyle(element).animationName), 'none');
    summary.push({ name, route: '/', layout: await layout(page, name + '-accounts') });
    await page.locator('#accountSearch').fill('王五的备用');
    assert.equal(await page.locator('.account-card:visible').count(), 1);
    await page.locator('#accountSearch').fill('找不到的账号');
    assert(await page.locator('#accountFilterEmpty').isVisible());
    await page.locator('#accountSearch').fill('');
    await page.locator('#accountStatusFilter').selectOption('expired');
    assert.equal(await page.locator('.account-card:visible').count(), 1);
    await page.locator('#accountStatusFilter').selectOption('');
    await page.locator('.account-select').nth(2).click();
    await page.waitForFunction(() => document.querySelector('#selectedAccountName').textContent.includes('王五'));

    if (name === 'desktop') {
      await page.locator('#platformMenu').click();
      assert(await page.locator('body').evaluate(body => body.classList.contains('platform-collapsed')));
      await layout(page, 'desktop-collapsed');
      await page.reload();
      assert(await page.locator('body').evaluate(body => body.classList.contains('platform-collapsed')));
      await page.locator('#platformMenu').click();
    } else {
      await page.locator('#platformMenu').click();
      assert(await page.locator('body').evaluate(body => body.classList.contains('platform-nav-open')));
      await page.keyboard.press('Escape');
      assert(!(await page.locator('body').evaluate(body => body.classList.contains('platform-nav-open'))));
    }

    for (const route of ['/proxies?ref=3', '/runs', '/runs?view=logs', '/runs?view=push', '/scan', '/users', '/settings', '/account-links', '/maintenance']) {
      await page.goto(base + route);
      await page.locator('.platform-shell').waitFor();
      await page.waitForLoadState('networkidle');
      summary.push({ name, route, layout: await layout(page, name + '-' + route.slice(1).replace(/[?=&]/g, '-')) });
      if (route.startsWith('/proxies')) {
        await page.waitForFunction(() => document.querySelector('#proxyAccountName').textContent.includes('王五'));
        await page.locator('label[for="proxyModeStatic"]').click();
        await page.locator('#staticProxy').fill('user:pass@127.0.0.1:8080');
        await page.locator('#saveProxy').click();
        await page.waitForFunction(() => document.querySelector('#proxyFeedback').textContent.includes('已保存'));
        assert.equal(mutations.at(-1).body.ref, '3');
      }
      if (route === '/runs?view=logs') {
        await page.locator('[data-action="run-log"]').first().click();
        await page.waitForFunction(() => document.querySelector('#logDrawerContent').textContent.includes('测试日志'));
        const pre = page.locator('#logDrawerContent');
        await pre.evaluate(element => { element.scrollTop = 200; });
        const previous = await pre.evaluate(element => element.scrollTop);
        await page.locator('#refreshLogBtn').click();
        await page.waitForFunction(n => document.querySelector('#logDrawerContent').textContent.split('\n').length > n, 100 + logRequests - 1);
        assert.equal(await pre.evaluate(element => element.scrollTop), previous, name + ': log scroll moved on refresh');
        await layout(page, name + '-log-open');
        await page.locator('#closeLogDrawer').click();
        assert(!(await page.locator('#logDrawer').evaluate(dialog => dialog.open)));
      }
      if (route === '/runs?view=push') {
        await page.locator('#pushForm').waitFor();
        await page.locator('#pushChannel').selectOption('pushplus');
        await page.locator('#pushToken').fill('test-push-token');
        await page.locator('#savePushBtn').click();
        await page.waitForFunction(() => document.querySelector('#pushSummary').textContent.includes('PushPlus'));
        assert(mutations.some(entry => entry.path === '/api/qinglong/push' && entry.body.channel === 'pushplus'));
      }
      if (route === '/scan') {
        assert(await page.locator('#qrWrap img').isVisible());
        qrFailure = true;
        await page.locator('#refreshBtn').click();
        await page.waitForFunction(() => document.querySelector('#status').textContent.includes('获取二维码失败'));
        qrFailure = false;
        await page.locator('#refreshBtn').click();
        await page.locator('#qrWrap img').waitFor();
      }
      if (route === '/users') {
        await page.locator('#showCreateUser').click();
        assert(await page.locator('#createUserDialog').isVisible());
        assert.equal(await page.locator('#createUserDialog').evaluate(element => element.tagName), 'SECTION');
        await layout(page, name + '-create-user');
        await page.locator('#createUserDialog [data-close-dialog]').first().click();
        assert(!(await page.locator('#createUserDialog').isVisible()));
      }
    }
    await page.locator('#platformBuild').click();
    await page.waitForFunction(() => !document.querySelector('#platformUpdateApply').disabled);
    await layout(page, name + '-update-dialog');
    await page.locator('#platformUpdateClose').click();
    assert(await page.locator('.platform-tab').count() <= 8);
    const tabCount = await page.locator('.platform-tab').count();
    await page.locator('.platform-tab:not([aria-current]) .platform-tab-close').first().click();
    assert.equal(await page.locator('.platform-tab').count(), tabCount - 1);
    assert.deepEqual(errors, [], name + ': browser errors');
    await context.close();
  }
  return summary;
}

async function edgeCases(browser) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await fixtures(page);
  await page.goto(base + '/login');
  await page.locator('#username').fill(username);
  await page.locator('#password').fill('deliberately-wrong-password');
  await page.locator('#submit').click();
  await page.waitForFunction(() => document.querySelector('#error').textContent.length > 0);
  assert(await page.locator('#submit').isEnabled());
  await page.locator('.reveal').click();
  assert.equal(await page.locator('#password').getAttribute('type'), 'text');
  await page.locator('.reveal').click();
  assert.equal(await page.locator('#password').getAttribute('type'), 'password');
  await layout(page, 'mobile-login-error');
  await login(page);
  currentAccounts = [];
  await page.goto(base);
  await page.waitForFunction(() => document.querySelector('#accountStrip').textContent.includes('暂无账号'));
  await layout(page, 'mobile-empty-accounts');
  currentAccounts = accounts;
  await page.goto(base + '/runs');
  await page.locator('#jobsBody .toggle').first().click();
  await page.waitForFunction(() => document.querySelector('#jobsBody').textContent.includes('定时已停用'));
  assert(mutations.some(entry => entry.path === '/api/qinglong/jobs/enable' && entry.body.enabled === false));
  await page.locator('#jobsBody [data-action="run"]').first().click();
  await page.locator('[data-action="run-log"]').first().waitFor();
  assert(mutations.some(entry => entry.path === '/api/qinglong/jobs/run' && entry.body.ref === '1'));
  await page.locator('[data-action="run-log"]').first().click();
  await page.waitForFunction(() => document.querySelector('#logDrawerContent').textContent.includes('测试日志'));
  logFailure = true;
  await page.locator('#refreshLogBtn').click();
  await page.waitForFunction(() => document.querySelector('#logDrawerContent').textContent.includes('服务返回空响应'));
  await layout(page, 'mobile-log-error');
  logFailure = false;
  await page.locator('#refreshLogBtn').click();
  await page.waitForFunction(() => document.querySelector('#logDrawerContent').textContent.includes('测试日志'));
  await page.locator('#closeLogDrawer').click();
  await page.goto(base + '/users');
  await page.locator('#showCreateUser').click();
  const member = 'ui-member-' + Date.now();
  await page.locator('#newUsername').fill(member);
  await page.locator('#newDisplayName').fill('测试普通用户');
  await page.locator('#newPassword').fill('member-test-password');
  await page.locator('#createUserSubmit').click();
  await page.waitForFunction(() => document.querySelector('#createUserDialog').hidden);
  await page.locator('#searchUsers').fill(member);
  await page.locator('#usersBody tr').filter({ hasText: member }).waitFor();
  assert.equal(await page.locator('#usersBody tr').count(), 1);
  await page.locator('#platformMenu').click();
  await page.locator('#platformLogout').click();
  await page.locator('#username').waitFor();
  await page.locator('#username').fill(member);
  await page.locator('#password').fill('member-test-password');
  await page.locator('#submit').click();
  await page.locator('.platform-shell').waitFor();
  await page.waitForFunction(() => document.querySelector('#platformUserRole').textContent.includes('普通用户'));
  assert.equal(await page.locator('.platform-nav a[data-admin-only="true"]:visible').count(), 0);
  const forbidden = await context.request.get(base + '/api/auth/users');
  assert.equal(forbidden.status(), 403);
  await page.goto(base + '/register');
  await layout(page, 'mobile-register');
  assert.deepEqual(errors, []);
  await context.close();
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: browserPath, headless: true });
  try {
    const summary = await runTests(browser);
    await edgeCases(browser);
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ summary, mutations, logRequests }, null, 2));
    console.log(JSON.stringify({ passed: true, pages: summary.length, logRequests, mutations: mutations.length, output }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
