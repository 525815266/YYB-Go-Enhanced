// name: 战马能量星球
// cron: 12 8,20 * * *
/**
 * 战马能量星球 — YYB Go 适配版
 * 接口域名：warhorsechina.cojoy.com.cn
 * 功能：签到、摸马儿、喂马、偷饲料、分享马儿、互助点赞、完善个人信息
 */

const $ = Env('战马能量星球');
const fs = require('fs');
const path = require('path');
const { filterAccounts, markFromError, setStatus, refOf } = require('./yyb-account-guard');

// ====================== YYB Go 账号（环境变量 YYB_SERVER = 地址@微信账号标识，多行） ======================
let SERVERS = (process.env.YYB_SERVER || "")
    .split(/\r?\n/)
    .map(s => s.trim())
    .filter(Boolean);

function parseYybGoEntry(raw) {
    if (!raw || !raw.includes("@")) return { server: null, ref: null };
    const [server, ref] = raw.split("@", 2);
    let s = server.trim().replace(/^https?:\/\//, "").replace(/\/+$/, "");
    return { server: s, ref: ref.trim() };
}
// ========================================================================================================

const WARHORSE_APP_ID = 'wx94dca6ef07a54c55';
SERVERS = filterAccounts(SERVERS, refOf, { appId: WARHORSE_APP_ID });
const TOKEN_DIR = path.join(__dirname, 'token_caches');
const TOKEN_FILE = path.join(TOKEN_DIR, 'zmnlxq_token_cache.json');
const BASE_URL = 'https://warhorsechina.cojoy.com.cn/app/api/custom';
const LOGIN_URL = 'https://warhorsechina.cojoy.com.cn/app/api/wxphonelogin';
const TOKEN_HEADER = 'cGvnZetrWSWfLcdYaN40mLdFx6ObkRltdZmhS5hQkgDbuZd9bLcQevwBVEjx-war-horse-zm-2025';

const ganta = 1;
const addFriend = 1;

let zmnlxq;
let zmnlxqSkey;
let zmnlxqArr = [];
let frinds = [];
let totalAccountsfrinds = [];
let msg = '';
let ok = '';

// ===================== JSON 文件读写 =====================
function readTokenFile() {
    try {
        if (fs.existsSync(TOKEN_FILE)) {
            return JSON.parse(fs.readFileSync(TOKEN_FILE, 'utf8'));
        }
    } catch (e) {
        console.log('读取 token 文件失败，将创建新文件');
    }
    return { accounts: {} };
}

function writeTokenFile(tokenData) {
    try {
        fs.mkdirSync(TOKEN_DIR, { recursive: true });
        fs.writeFileSync(TOKEN_FILE, JSON.stringify(tokenData, null, 2), 'utf8');
    } catch (e) {
        console.log('写入 token 文件失败:', e);
    }
}

function getCommonHeaders() {
    return {
        'host': 'warhorsechina.cojoy.com.cn',
        [TOKEN_HEADER]: zmnlxqSkey || TOKEN_HEADER,
        'customappid': WARHORSE_APP_ID,
        'referer': `https://servicewechat.com/${WARHORSE_APP_ID}/182/page-frame.html`,
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; MI 8 Build/QKQ1.190828.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 XWEB/3235 MMWEBSDK/20220204 Mobile Safari/537.36 MMWEBID/6242 MicroMessenger/8.0.20.2080(0x28001435) Process/appbrand0 WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 miniProgram/wx532ecb3bdaaf92f9'
    };
}

// ===================== YYB Go 接口 =====================
function getWxCode(server, ref) {
    return new Promise((resolve) => {
        $.post({
            url: `http://${server}/wxapp/getCode`,
            json: { app_id: WARHORSE_APP_ID, ref: String(ref) },
            headers: { 'content-type': 'application/json' }
        }, (err, resp, data) => {
            try {
                const json = JSON.parse(data);
                if (json.code === 0 && json.data?.result?.code) resolve(json.data.result.code);
                else {
                    const message = JSON.stringify(data);
                    markFromError(ref, message, WARHORSE_APP_ID);
                    console.log(server + " 获取code失败: " + message);
                    resolve(null);
                }
            } catch (e) { console.log(e); resolve(null); }
        });
    });
}

function getPhoneEncrypted(server, ref) {
    return new Promise((resolve) => {
        $.post({
            url: `http://${server}/wxapp/getPhoneNumber`,
            json: { app_id: WARHORSE_APP_ID, ref: String(ref) },
            headers: { 'content-type': 'application/json' }
        }, (err, resp, data) => {
            try {
                const json = JSON.parse(data);
                if (json.code === 0 && json.data?.result?.encryptedData && json.data.result.iv) {
                    resolve({ encryptedData: json.data.result.encryptedData, iv: json.data.result.iv });
                } else {
                    markFromError(ref, data, WARHORSE_APP_ID);
                    console.log('获取手机加密数据失败:', data);
                    resolve(null);
                }
            } catch (e) { console.log(e); resolve(null); }
        });
    });
}

function loginWarHorse(code, encryptedData, iv) {
    return new Promise((resolve) => {
        $.post({
            url: LOGIN_URL,
            json: { profile: {} },
            headers: {
                'host': 'warhorsechina.cojoy.com.cn',
                'x-wx-code': code,
                'x-wx-encrypted-data': encryptedData,
                'x-wx-iv': iv,
                [TOKEN_HEADER]: TOKEN_HEADER,
                'customappid': WARHORSE_APP_ID,
                'referer': `https://servicewechat.com/${WARHORSE_APP_ID}/182/page-frame.html`,
                'user-agent': 'Mozilla/5.0 (Linux; Android 10; MI 8 Build/QKQ1.190828.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 XWEB/3235 MMWEBSDK/20220204 Mobile Safari/537.36 MMWEBID/6242 MicroMessenger/8.0.20.2080(0x28001435) Process/appbrand0 WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 miniProgram/wx532ecb3bdaaf92f9'
            }
        }, (err, resp, data) => {
            try {
                const json = JSON.parse(data);
                if (json.status === 'ok' && json.desc?.data?.f1safe) {
                    resolve({ safe: json.desc.data.f1safe, skey: json.desc.data.skey || '' });
                } else {
                    console.log('战马登录失败:', data);
                    resolve(null);
                }
            } catch (e) { console.log('登录解析错误:', e, '原始数据:', data); resolve(null); }
        });
    });
}

function testSafeValid(safe, skey) {
    return new Promise((resolve) => {
        const oldSkey = zmnlxqSkey;
        zmnlxqSkey = skey;
        const headers = getCommonHeaders();
        zmnlxqSkey = oldSkey;

        $.get({
            url: `${BASE_URL}/getusercenter?safe=${safe}`,
            headers: headers,
            timeout: 5000
        }, (err, resp, data) => {
            if (err) { resolve(true); return; }
            if (!data) { resolve(true); return; }
            try {
                const json = JSON.parse(data);
                if (json.status == 1) resolve(true);
                else { console.log(`safe 无效: ${json.msg || '未知错误'}`); resolve(false); }
            } catch (e) { resolve(true); }
        });
    });
}

async function getOrRefreshSafe(server, ref, tokenStore) {
    const cached = tokenStore.accounts[ref];
    if (cached?.safe) {
        const isValid = await testSafeValid(cached.safe, cached.skey || TOKEN_HEADER);
        if (isValid) {
            console.log(`账号 ${ref} 使用缓存凭证`);
            return { safe: cached.safe, skey: cached.skey || '' };
        } else {
            console.log(`账号 ${ref} 缓存失效，重新登录`);
        }
    }

    const code = await getWxCode(server, ref);
    if (!code) return null;

    await $.wait(1000);

    const phoneData = await getPhoneEncrypted(server, ref);
    if (!phoneData) return null;

    const result = await loginWarHorse(code, phoneData.encryptedData, phoneData.iv);
    if (result?.safe) {
        const valid = await testSafeValid(result.safe, result.skey);
        if (valid) {
            console.log(`账号 ${ref} 登录成功，safe: ${result.safe}`);
            tokenStore.accounts[ref] = { safe: result.safe, skey: result.skey };
            writeTokenFile(tokenStore);
            return { safe: result.safe, skey: result.skey };
        } else {
            console.log(`账号 ${ref} 新 safe 无效`);
        }
    }
    return null;
}

// ===================== 初始化 =====================
async function Envs() {
    if (!SERVERS.length) {
        console.log('❌ 未配置环境变量 YYB_SERVER');
        return false;
    }
    console.log(`✅ 读取到 ${SERVERS.length} 个 YYB Go 账号`);

    const tokenStore = readTokenFile();
    if (!tokenStore.accounts) tokenStore.accounts = {};

    for (const entry of SERVERS) {
        const { server, ref } = parseYybGoEntry(entry);
        if (!server || !ref) {
            console.log(`❌ 格式错误: ${entry}`);
            continue;
        }
        const cred = await getOrRefreshSafe(server, ref, tokenStore);
        if (cred) {
            zmnlxqArr.push(cred);
            setStatus(ref, 'ready', '', WARHORSE_APP_ID);
        }
        await $.wait(2000);
    }

    if (zmnlxqArr.length === 0) {
        console.log('没有成功获取任何账号的凭证');
        return false;
    }
    console.log(`\n成功获取 ${zmnlxqArr.length} 个有效账号，开始执行任务`);
    return true;
}

// ===================== 主流程 =====================
!(async () => {
    if (!(await Envs())) return;

    console.log(`目前实现功能：日常签到、摸马儿、喂马、偷饲料、分享马儿、喂饲料、互助点赞、完善个人信息`);
    console.log(`\n=========================================\n脚本执行 - 北京时间(UTC+8)：${new Date(new Date().getTime() + 8*3600000).toLocaleString()}\n=========================================\n`);
    console.log(`\n=================== 共找到 ${zmnlxqArr.length} 个账号 ===================`);

    if (addFriend) {
        for (let i = 0; i < zmnlxqArr.length; i++) {
            zmnlxq = zmnlxqArr[i].safe;
            zmnlxqSkey = zmnlxqArr[i].skey;
            console.log('去加好友');
            await $.wait(200);
            for (let j = 0; j < zmnlxqArr.length; j++) {
                if (i !== j) {
                    await getranklist(true, zmnlxqArr[j].safe);
                    await $.wait(2000);
                }
            }
        }
    }

    for (let i = 0; i < zmnlxqArr.length; i++) {
        zmnlxq = zmnlxqArr[i].safe;
        zmnlxqSkey = zmnlxqArr[i].skey;
        console.log(`\n========= 开始【第 ${i+1} 个账号】执行任务=========\n`);

        console.log('获取信息');
        await getuser();
        await $.wait(2000);

        if (ok == 1) {
            console.log('签到');
            await checkin();
            await $.wait(2000);

            console.log('查询题库');
            await gettiku();
            await $.wait(2000);

            console.log('分享任务');
            await getshare();
            await $.wait(2000);

            console.log('加入排行榜');
            await joinxcx();
            await $.wait(2000);

            console.log('领取小马儿');
            await getmaer();
            await $.wait(2000);

            console.log('摸一摸');
            await getmoyimo();
            await $.wait(2000);

            console.log('马儿分享任务');
            await checkslgift();
            await $.wait(2000);

            console.log('去喂马');
            await getweima();
            await $.wait(2000);

            console.log('去点赞');
            await getranklist();
            await $.wait(2000);
        }
    }

    if (ganta) {
        for (let i = 0; i < zmnlxqArr.length; i++) {
            zmnlxq = zmnlxqArr[i].safe;
            zmnlxqSkey = zmnlxqArr[i].skey;
            console.log(`\n========= 开始【第 ${i+1} 个账号】饲料互助=========\n`);
            let isCompletedTousiliao = false, isCompletedSongsiliao = false;
            for (let j = 0; j < totalAccountsfrinds.length; j++) {
                await getotherhorseinfo(totalAccountsfrinds[j]?.id);
                if (!isCompletedTousiliao) {
                    console.log('去偷饲料');
                    isCompletedTousiliao = await tousiliao(j);
                    await $.wait(2000);
                }
                if (!isCompletedSongsiliao) {
                    console.log('去送饲料');
                    isCompletedSongsiliao = await songsiliao(j);
                }
                if (isCompletedSongsiliao && isCompletedTousiliao) {
                    console.log('今日已到上限');
                    break;
                }
            }
        }
    }

    if (msg) console.log(msg);
})()
    .catch((e) => console.log(e))
    .finally(() => $.done());

// ===================== 业务函数 =====================
async function getuser(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/getusercenter?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, async (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                if (result.status == 1) {
                    ok = 1;
                    console.log(`用户：${result.nickname} 当前能量：${result.nowscore}`);
                    if (result.isgzhkl == 0) {
                        console.log('公众号口令任务：未完成，开始完成');
                        await gzhkl();
                    }
                    if (result.isinfo == 0) {
                        console.log('完善个人资料：未完成，开始完成');
                        let tel = await getTel();
                        if (tel) {
                            await saveuserinfo(result.headimgurl, result.nickname, Math.random().toFixed(0),
                                new Date().getFullYear() + '-' + (new Date().getMonth()+1) + '-' + new Date().getDate(), tel);
                        } else {
                            console.log('未授权手机号，无法完善');
                        }
                    }
                } else {
                    console.log('信息获取失败');
                }
            } catch (e) { console.log(e) }
            finally { resolve(); }
        });
    });
}

function getTel(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/getuserinfo?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                console.log(result.msg);
                resolve(result.tel);
            } catch (e) { console.log(e); resolve(null); }
        });
    });
}

function checkin(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/checkin?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try { console.log(JSON.parse(data).msg); } catch (e) { console.log(e); }
            finally { resolve(); }
        });
    });
}

function joinxcx(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/joinxcx?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                if (result.status == 1) console.log('加入排行榜成功');
                else console.log('加入排行榜：', result.msg);
            } catch (e) { console.log(e); }
            finally { resolve(); }
        });
    });
}

function getranklist(addFr = false, fromsafe = '', timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/getranklist?safe=${zmnlxq}&type=1&fromsafe=${fromsafe}`,
            headers: getCommonHeaders(),
            timeout
        }, async (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                if (result.status == 1) {
                    if (addFr) {
                        console.log('添加好友成功');
                    } else {
                        frinds = result?.data?.filter(item => item?.ismy != 1 && item?.id != 0) || [];
                        totalAccountsfrinds = [...totalAccountsfrinds, ...result?.data?.filter(item => item?.id != 0)];
                        const notLiked = frinds.filter(item => item?.iszan == 0);
                        console.log(`获取到${frinds.length}个好友，${notLiked.length}个可点赞`);
                        let isComplete = false;
                        for (const f of notLiked) {
                            if (!isComplete) isComplete = await like(f.id);
                            await getotherhorseinfo(f.id);
                            await $.wait(500);
                        }
                    }
                } else console.log('获取好友失败：', result.msg);
            } catch (e) { console.log(e); }
            finally { resolve(); }
        });
    });
}

function getotherhorseinfo(likeUserId, timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/getotherhorseinfo?safe=${zmnlxq}&friendid=${likeUserId}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                if (result.status == 1) console.log('加为互助好友成功');
                else console.log('加互助好友失败：', result.msg);
            } catch (e) { console.log(e); }
            finally { resolve(); }
        });
    });
}

function like(likeUserId, timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/subrank?safe=${zmnlxq}&id=${likeUserId}&type=1`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                if (result.status == 1) console.log('点赞成功');
                else {
                    console.log('点赞失败：', result.msg);
                    resolve(result?.msg?.includes('已到上限'));
                    return;
                }
            } catch (e) { console.log(e); }
            resolve(false);
        });
    });
}

function getshare(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/share?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try { console.log(JSON.parse(data).msg); } catch (e) { console.log('数据异常：', data); }
            finally { resolve(); }
        });
    });
}

function checkslgift(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/checkslgift?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try { console.log(JSON.parse(data).msg); } catch (e) { console.log('数据异常：', data); }
            finally { resolve(); }
        });
    });
}

function saveuserinfo(avatar, nickname, sex, birthday, tel, timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/saveuserinfo?safe=${zmnlxq}&avatar=${encodeURIComponent(avatar)}&nickname=${encodeURIComponent(nickname)}&uname=${encodeURIComponent(nickname)}&sex=${sex}&birthday=${birthday}&tel=${tel}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try { console.log(JSON.parse(data).msg); } catch (e) { console.log('数据异常：', data); }
            finally { resolve(); }
        });
    });
}

function gzhkl(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/gzhkl?safe=${zmnlxq}&kl=${encodeURIComponent('有能量 当燃战马！')}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try { console.log(JSON.parse(data).msg); } catch (e) { console.log(e); }
            finally { resolve(); }
        });
    });
}

async function gettiku(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/getquesbackstatus?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, async (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                if (result.status == 1) {
                    console.log(result.msg);
                    console.log('刷新题目');
                    await $.wait(2000);
                    await getques();
                    console.log('开始答题（固定答案）');
                    await $.wait(2000);
                    await ques1();
                    await $.wait(2000);
                    await ques2();
                    await $.wait(2000);
                    await ques3();
                } else console.log(result.msg);
            } catch (e) { console.log(e); }
            finally { resolve(); }
        });
    });
}

function getques(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/getques?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try { console.log(JSON.parse(data).msg); } catch (e) { console.log(e); }
            finally { resolve(); }
        });
    });
}

function ques1() { return subques(126, 'C'); }
function ques2() { return subques(138, 'C'); }
function ques3() { return subques(119, 'A'); }

function subques(qid, val) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/subques?safe=${zmnlxq}&qid=${qid}&val=${val}`,
            headers: getCommonHeaders()
        }, (err, resp, data) => {
            try { console.log(JSON.parse(data).msg); } catch (e) { }
            finally { resolve(); }
        });
    });
}

function getmaer(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/starthorse?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try { console.log(JSON.parse(data).msg); } catch (e) { }
            finally { resolve(); }
        });
    });
}

function getmoyimo(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/strokehorse?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, (err, resp, data) => {
            try { console.log(JSON.parse(data).msg); } catch (e) { }
            finally { resolve(); }
        });
    });
}

async function getweima(timeout = 2000) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/horseeat?safe=${zmnlxq}`,
            headers: getCommonHeaders(),
            timeout
        }, async (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                if (result.status != 0) {
                    await getweima();
                    await $.wait(2000);
                } else {
                    console.log(result.msg);
                }
            } catch (e) { console.log(e); }
            finally { resolve(); }
        });
    });
}

function tousiliao(num2) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/subhorseplayer?safe=${zmnlxq}&friendid=${totalAccountsfrinds[num2]?.id}&type=1`,
            headers: getCommonHeaders()
        }, (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                console.log(result.msg);
                resolve(result?.msg?.includes('已到上限'));
            } catch (e) { resolve(false); }
        });
    });
}

function songsiliao(num2) {
    return new Promise((resolve) => {
        $.get({
            url: `${BASE_URL}/subhorseplayer?safe=${zmnlxq}&friendid=${totalAccountsfrinds[num2]?.id}&type=2`,
            headers: getCommonHeaders()
        }, (err, resp, data) => {
            try {
                const result = JSON.parse(data);
                console.log(result.msg);
                resolve(result?.msg?.includes('已到上限'));
            } catch (e) { resolve(false); }
        });
    });
}

// ===================== Env 框架 =====================
function Env(t, e) {
    "undefined" != typeof process && JSON.stringify(process.env).indexOf("xxxxxx") > -1 && process.exit(0);
    class s {
        constructor(t) { this.env = t }
        send(t, e = "GET") {
            t = "string" == typeof t ? { url: t } : t;
            let s = this.get;
            return "POST" === e && (s = this.post), new Promise((e, i) => { s.call(this, t, (t, s, r) => { t ? i(t) : e(s) }) })
        }
        get(t) { return this.send.call(this.env, t) }
        post(t) { return this.send.call(this.env, t, "POST") }
    }
    return new class {
        constructor(t, e) {
            this.name = t, this.http = new s(this), this.data = null, this.dataFile = "box.dat", this.logs = [], this.isMute = !1, this.isNeedRewrite = !1, this.logSeparator = "\n", this.startTime = (new Date).getTime(), Object.assign(this, e), this.log("", `🔔${this.name}, 开始!`)
        }
        isNode() { return "undefined" != typeof module && !!module.exports }
        isQuanX() { return "undefined" != typeof $task }
        isSurge() { return "undefined" != typeof $httpClient && "undefined" == typeof $loon }
        isLoon() { return "undefined" != typeof $loon }
        toObj(t, e = null) { try { return JSON.parse(t) } catch { return e } }
        toStr(t, e = null) { try { return JSON.stringify(t) } catch { return e } }
        getjson(t, e) { let s = e; const i = this.getdata(t); if (i) try { s = JSON.parse(this.getdata(t)) } catch { } return s }
        setjson(t, e) { try { return this.setdata(JSON.stringify(t), e) } catch { return !1 } }
        getScript(t) { return new Promise(e => { this.get({ url: t }, (t, s, i) => e(i)) }) }
        runScript(t, e) { return new Promise(s => { let i = this.getdata("@chavy_boxjs_userCfgs.httpapi"); i = i ? i.replace(/\n/g, "").trim() : i; let r = this.getdata("@chavy_boxjs_userCfgs.httpapi_timeout"); r = r ? 1 * r : 20, r = e && e.timeout ? e.timeout : r; const [o, h] = i.split("@"), n = { url: `http://${h}/v1/scripting/evaluate`, body: { script_text: t, mock_type: "cron", timeout: r }, headers: { "X-Key": o, Accept: "*/*" } }; this.post(n, (t, e, i) => s(i)).catch(t => this.logErr(t)) }) }
        loaddata() { if (!this.isNode()) return {}; { this.fs = this.fs ? this.fs : require("fs"), this.path = this.path ? this.path : require("path"); const t = this.path.resolve(this.dataFile), e = this.path.resolve(process.cwd(), this.dataFile), s = this.fs.existsSync(t), i = !s && this.fs.existsSync(e); if (!s && !i) return {}; { const i = s ? t : e; try { return JSON.parse(this.fs.readFileSync(i)) } catch (t) { return {} } } } }
        writedata() { if (this.isNode()) { this.fs = this.fs ? this.fs : require("fs"), this.path = this.path ? this.path : require("path"); const t = this.path.resolve(this.dataFile), e = this.path.resolve(process.cwd(), this.dataFile), s = this.fs.existsSync(t), i = !s && this.fs.existsSync(e), r = JSON.stringify(this.data); s ? this.fs.writeFileSync(t, r) : i ? this.fs.writeFileSync(e, r) : this.fs.writeFileSync(t, r) } }
        lodash_get(t, e, s) { const i = e.replace(/\[(\d+)\]/g, ".$1").split("."); let r = t; for (const t of i) if (r = Object(r)[t], void 0 === r) return s; return r }
        lodash_set(t, e, s) { return Object(t) !== t ? t : (Array.isArray(e) || (e = e.toString().match(/[^.[\]]+/g) || []), e.slice(0, -1).reduce((t, s, i) => Object(t[s]) === t[s] ? t[s] : t[s] = Math.abs(e[i + 1]) >> 0 == +e[i + 1] ? [] : {}, t)[e[e.length - 1]] = s, t) }
        getdata(t) { let e = this.getval(t); if (/^@/.test(t)) { const [, s, i] = /^@(.*?)\.(.*?)$/.exec(t), r = s ? this.getval(s) : ""; if (r) try { const t = JSON.parse(r); e = t ? this.lodash_get(t, i, "") : e } catch (t) { e = "" } } return e }
        setdata(t, e) { let s = !1; if (/^@/.test(e)) { const [, i, r] = /^@(.*?)\.(.*?)$/.exec(e), o = this.getval(i), h = i ? "null" === o ? null : o || "{}" : "{}"; try { const e = JSON.parse(h); this.lodash_set(e, r, t), s = this.setval(JSON.stringify(e), i) } catch (e) { const o = {}; this.lodash_set(o, r, t), s = this.setval(JSON.stringify(o), i) } } else s = this.setval(t, e); return s }
        getval(t) { return this.isSurge() || this.isLoon() ? $persistentStore.read(t) : this.isQuanX() ? $prefs.valueForKey(t) : this.isNode() ? (this.data = this.loaddata(), this.data[t]) : this.data && this.data[t] || null }
        setval(t, e) { return this.isSurge() || this.isLoon() ? $persistentStore.write(t, e) : this.isQuanX() ? $prefs.setValueForKey(t, e) : this.isNode() ? (this.data = this.loaddata(), this.data[e] = t, this.writedata(), !0) : this.data && this.data[e] || null }
        initGotEnv(t) { this.got = this.got ? this.got : require("got"), this.cktough = this.cktough ? this.cktough : require("tough-cookie"), this.ckjar = this.ckjar ? this.ckjar : new this.cktough.CookieJar, t && (t.headers = t.headers ? t.headers : {}, void 0 === t.headers.Cookie && void 0 === t.cookieJar && (t.cookieJar = this.ckjar)) }
        get(t, e = (() => { })) { t.headers && (delete t.headers["Content-Type"], delete t.headers["Content-Length"]), this.isSurge() || this.isLoon() ? (this.isSurge() && this.isNeedRewrite && (t.headers = t.headers || {}, Object.assign(t.headers, { "X-Surge-Skip-Scripting": !1 })), $httpClient.get(t, (t, s, i) => { !t && s && (s.body = i, s.statusCode = s.status), e(t, s, i) })) : this.isQuanX() ? (this.isNeedRewrite && (t.opts = t.opts || {}, Object.assign(t.opts, { hints: !1 })), $task.fetch(t).then(t => { const { statusCode: s, statusCode: i, headers: r, body: o } = t; e(null, { status: s, statusCode: i, headers: r, body: o }, o) }, t => e(t))) : this.isNode() && (this.initGotEnv(t), this.got(t).on("redirect", (t, e) => { try { if (t.headers["set-cookie"]) { const s = t.headers["set-cookie"].map(this.cktough.Cookie.parse).toString(); s && this.ckjar.setCookieSync(s, null), e.cookieJar = this.ckjar } } catch (t) { this.logErr(t) } }).then(t => { const { statusCode: s, statusCode: i, headers: r, body: o } = t; e(null, { status: s, statusCode: i, headers: r, body: o }, o) }, t => { const { message: s, response: i } = t; e(s, i, i && i.body) })) }
        post(t, e = (() => { })) { if (t.body && t.headers && !t.headers["Content-Type"] && (t.headers["Content-Type"] = "application/x-www-form-urlencoded"), t.headers && delete t.headers["Content-Length"], this.isSurge() || this.isLoon()) this.isSurge() && this.isNeedRewrite && (t.headers = t.headers || {}, Object.assign(t.headers, { "X-Surge-Skip-Scripting": !1 })), $httpClient.post(t, (t, s, i) => { !t && s && (s.body = i, s.statusCode = s.status), e(t, s, i) }); else if (this.isQuanX()) t.method = "POST", this.isNeedRewrite && (t.opts = t.opts || {}, Object.assign(t.opts, { hints: !1 })), $task.fetch(t).then(t => { const { statusCode: s, statusCode: i, headers: r, body: o } = t; e(null, { status: s, statusCode: i, headers: r, body: o }, o) }, t => e(t)); else if (this.isNode()) { this.initGotEnv(t); const { url: s, ...i } = t; this.got.post(s, i).then(t => { const { statusCode: s, statusCode: i, headers: r, body: o } = t; e(null, { status: s, statusCode: i, headers: r, body: o }, o) }, t => { const { message: s, response: i } = t; e(s, i, i && i.body) }) } }
        time(t, e = null) { const s = e ? new Date(e) : new Date; let i = { "M+": s.getMonth() + 1, "d+": s.getDate(), "H+": s.getHours(), "m+": s.getMinutes(), "s+": s.getSeconds(), "q+": Math.floor((s.getMonth() + 3) / 3), S: s.getMilliseconds() }; /(y+)/.test(t) && (t = t.replace(RegExp.$1, (s.getFullYear() + "").substr(4 - RegExp.$1.length))); for (let e in i) new RegExp("(" + e + ")").test(t) && (t = t.replace(RegExp.$1, 1 == RegExp.$1.length ? i[e] : ("00" + i[e]).substr(("" + i[e]).length))); return t }
        msg(e = t, s = "", i = "", r) { const o = t => { if (!t) return t; if ("string" == typeof t) return this.isLoon() ? t : this.isQuanX() ? { "open-url": t } : this.isSurge() ? { url: t } : void 0; if ("object" == typeof t) { if (this.isLoon()) { let e = t.openUrl || t.url || t["open-url"], s = t.mediaUrl || t["media-url"]; return { openUrl: e, mediaUrl: s } } if (this.isQuanX()) { let e = t["open-url"] || t.url || t.openUrl, s = t["media-url"] || t.mediaUrl; return { "open-url": e, "media-url": s } } if (this.isSurge()) { let e = t.url || t.openUrl || t["open-url"]; return { url: e } } } }; if (this.isMute || (this.isSurge() || this.isLoon() ? $notification.post(e, s, i, o(r)) : this.isQuanX() && $notify(e, s, i, o(r))), !this.isMuteLog) { let t = ["", "==============📣系统通知📣=============="]; t.push(e), s && t.push(s), i && t.push(i), console.log(t.join("\n")), this.logs = this.logs.concat(t) } }
        log(...t) { t.length > 0 && (this.logs = [...this.logs, ...t]), console.log(t.join(this.logSeparator)) }
        logErr(t, e) { const s = !this.isSurge() && !this.isQuanX() && !this.isLoon(); s ? this.log("", `❗️${this.name}, 错误!`, t.stack) : this.log("", `❗️${this.name}, 错误!`, t) }
        wait(t) { return new Promise(e => setTimeout(e, t)) }
        done(t = {}) { const e = (new Date).getTime(), s = (e - this.startTime) / 1e3; this.log("", `🔔${this.name}, 结束! 🕛 ${s} 秒`), this.log(), (this.isSurge() || this.isQuanX() || this.isLoon()) && $done(t) }
    }(t, e)
}
