// name: 慕斯
// cron: 48 10 * * *
const axios = require("axios");
const { filterAccounts, markFromError, setStatus, refOf } = require("./yyb-account-guard");
/* __YYB_SERVER_DOLLAR_SHIM__ */
if (typeof $ === 'undefined') {
  const __path = require('path');
  global.$ = {
    name: (typeof __filename !== 'undefined' ? __path.basename(__filename) : 'script'),
    isNode: () => true,
    msg: (...a) => { try { console.log(...a); } catch (e) {} },
    log: (...a) => { try { console.log(...a); } catch (e) {} },
    getdata: (k) => process.env[k] || '',
    setdata: () => {},
    SendMsg: async () => {},
    logs: [],
    time: (fmt) => {
      const d = new Date();
      const p = (n, l = 2) => String(n).padStart(l, '0');
      const m = { yyyy: d.getFullYear(), yy: String(d.getFullYear()).slice(-2), MM: p(d.getMonth()+1), M: d.getMonth()+1, dd: p(d.getDate()), d: d.getDate(), HH: p(d.getHours()), H: d.getHours(), mm: p(d.getMinutes()), m: d.getMinutes(), ss: p(d.getSeconds()), s: d.getSeconds() };
      return String(fmt).replace(/yyyy|yy|MM|M|dd|d|HH|H|mm|m|ss|s/g, (k) => m[k]);
    },
    httpRequest: async (opt) => {
      const axios = require('axios');
      const method = (opt.method || 'GET').toUpperCase();
      const data = opt.body !== undefined ? opt.body : (opt.data !== undefined ? opt.data : opt.json);
      const r = await axios({ method, url: opt.url, headers: opt.headers || {}, data, timeout: opt.timeout || 30000, validateStatus: () => true });
      return { status: r.status, headers: r.headers, body: typeof r.data === 'string' ? r.data : JSON.stringify(r.data) };
    },
  };
}

// ====================== YYB Go 账号（环境变量 YYB_SERVER = 地址@微信账号标识，多行） ======================
let SERVERS = (process.env.YYB_SERVER || "")
    .split(/\r?\n/)
    .map(s => s.trim())
    .filter(Boolean);
if (!SERVERS.length) {
    console.error("未配置环境变量 YYB_SERVER，请设置后重试（格式：地址@微信账号标识，多行换行）");
    process.exit(1);
}
SERVERS = filterAccounts(SERVERS, value => refOf(value), { appId: 'wx03527497c5369a2c' });
if (!SERVERS.length) {
    console.log("公共缓存中没有可执行账号，本轮结束");
    process.exit(0);
}
function parseYybGoEntry(rawValue) {
    const value = String(rawValue || "").trim();
    if (!value) return { server: "", ref: "" };
    const atIndex = value.indexOf("@");
    if (atIndex === -1) {
        console.log("YYB_SERVER 格式应为 地址@微信账号标识，当前值: " + value);
        return { server: "", ref: "" };
    }
    let server = value.slice(0, atIndex).trim();
    const ref = value.slice(atIndex + 1).trim();
    if (server.startsWith("http://")) server = server.slice(7);
    else if (server.startsWith("https://")) server = server.slice(8);
    server = server.replace(/\/+$/, "");
    if (!server || !ref) return { server: "", ref: "" };
    return { server, ref };
}
async function getCode(server) {
    const { server: parsedServer, ref } = parseYybGoEntry(server);
    if (!parsedServer || !ref) return null;
    const url = "http://" + parsedServer + "/wxapp/getCode";
    try {
        const { data } = await axios.post(url, { ref, app_id: 'wx03527497c5369a2c' }, { timeout: 20000, proxy: false });
        const code = data && data.data && data.data.result && data.data.result.code;
        if (!data || data.code !== 0 || !code) {
            console.log(parsedServer + " 获取code失败: " + JSON.stringify(data));
            return null;
        }
        console.log(parsedServer + " 获取code成功");
        return code;
    } catch (e) {
        console.log(parsedServer + " 获取code异常: " + e.message);
        return null;
    }
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
let userIdx = 1;

const strSplitor = "#";

const defaultUserAgent = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.31(0x18001e31) NetType/WIFI Language/zh_CN miniProgram"

class Task {
    constructor(env) {
        this.server = env;
        const _yyb = parseYybGoEntry(this.server);
        this.ref = _yyb.ref;
        this.openid = _yyb.ref;
        this.index = userIdx++
        this.user = env.split(strSplitor);
        this.activedAuthToken = null
        this.wcsid = this.openid
        this.openId = null
    }

    async run() {
        //随机延迟5-30s 模拟人工操作
       await await sleep(Math.floor(Math.random() * 20 + 5) * 1000);
        let code = await getCode(this.server)
        if (code) {
            await this.getUserToken(code)
        }
        if (!this.activedAuthToken) {
            console.log(`账号[${this.index}] 获取用户Token失败❌`)
            return
        }

        await this.getUserInfo()
        if (!this.customId) return
        await this.getJob()
        if (!this.isSigned) {
            await this.doSign()
        }
    }
    async getUserToken(code) {
        const timestamp = new Date().getTime();
        let options = {
            method: 'POST',
            url: `https://atom.musiyoujia.com/user/wechatlogin/applets`,
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': defaultUserAgent,
                "api_client_code": "65",
                "api_version": "1.0.0",
                'api_timestamp': timestamp,
                'api_token': '',

                'api_sign': this.MD5_Encrypt(`api_client_code=65&api_version=1.0.0&api_timestamp=${timestamp}`)?.toUpperCase()

            }
            ,
            data:
            {
                'appId': 'wx03527497c5369a2c',
                'appType': 'WECHAT_MINI_PROGRAM',
                'code': '' + code,
                'systemCode': '65'
            }
        }
        let {
            data: result
        } = await axios.request(options);

        if (result?.code == '0') {
            this.openId = result.data.openId
            this.activedAuthToken = result.data.token
            console.log(`🌸账号[${this.index}] 获取用户Token成功:${this.activedAuthToken}`)
        } else {
            console.log(`🌸账号[${this.index}] 获取用户Token-失败:${result.msg}❌`)
        }
    }

    MD5_Encrypt(str) {
        const crypto = require("crypto")
        return crypto.createHash('md5').update(str).digest('hex');
    }
    async getUserInfo() {
        try {
            const timestamp = new Date().getTime();
            let options = {
                method: 'POST',
                url: `https://atom.musiyoujia.com/member/wechatlogin/selectuserinfo`,
                headers: {
                    'Content-Type': 'application/json',
                    'User-Agent': defaultUserAgent,
                    "api_client_code": "65",
                    "api_version": "1.0.0",
                    'api_timestamp': timestamp,
                    'api_token': this.activedAuthToken,

                    'api_sign': this.MD5_Encrypt(`api_client_code=65&api_version=1.0.0&api_timestamp=${timestamp}`)?.toUpperCase()

                },
                data: { "appId": "wx03527497c5369a2c", "appType": "WECHAT_MINI_PROGRAM", "openId": `${this.openId}` }
            }
            let { data: result } = await axios.request(options)

            if (result?.msg === "success") {
                const memberInfo = result?.data?.memberInfo || result?.data?.resMemberInfo
                this.customId = memberInfo?.memberId
                this.valid = Boolean(this.customId)
                if (!this.valid) {
                    console.log(`账号[${this.index}] 查询个人信息失败：返回数据缺少 memberId`)
                    return
                }
                console.log(`账号[${this.index}] 查询个人信息成功，积分：${result?.data?.memberInfo?.pointInfo?.point}`)
            } else {
                console.log(`账号[${this.index}] 查询个人信息失败：${result?.msg || JSON.stringify(result)}`)
                this.valid = false
            }

        } catch (e) {
            console.log(e)
        }
    }

    async getJob() {
        try {
            const timestamp = new Date().getTime();
            let options = {
                method: "POST",
                url: `https://atom.musiyoujia.com/member/memberbehavior/getBehaviorInfos`,
                headers: {
                    'Content-Type': 'application/json',
                    'User-Agent': defaultUserAgent,
                    "api_client_code": "65",
                    "api_version": "1.0.0",
                    'api_token': this.activedAuthToken,

                    'api_timestamp': timestamp,
                    'api_sign': this.MD5_Encrypt(`api_token=${this.activedAuthToken}&api_client_code=65&api_version=1.0.0&api_timestamp=${timestamp}`)?.toUpperCase()

                },
                data: { "appId": "wx03527497c5369a2c", "appType": "WECHAT_MINI_PROGRAM", "behaviorIds": [1, 2, 10203, 10204, 10205, 5], "sourceChannel": "会员小程序", "source": `${this.customId}`, "openId": `${this.openId}` }
            }
            let { data: result } = await axios.request(options)

            if (result?.msg === "success") {
                this.isSigned = result?.data[0].acts['每天已获得积分次数'] === 1;
                console.log(`账号[${this.index}] 获取任务列表成功，${this.isSigned ? '已签到' : '未签到'}`)
            } else {
                console.log(`账号[${this.index}] 获取任务列表失败：${result?.msg || JSON.stringify(result)}`)
            }

        } catch (e) {
            console.log(e)
        }
    }

    async doSign() {
        try {
            const timestamp = new Date().getTime();
            const eventAttr2 = $.time('yyyy.MM.dd')
            let options = {
                method: 'POST',
                url: `https://atom.musiyoujia.com/member/memberbehavior/add`,
                headers: {
                    'Content-Type': 'application/json',
                    'User-Agent': defaultUserAgent,
                    "api_client_code": "65",
                    'api_token': this.activedAuthToken,
                    "api_version": "1.0.0",
                    'api_timestamp': timestamp,
                    'api_sign': this.MD5_Encrypt(`api_token=${this.activedAuthToken}&api_client_code=65&api_version=1.0.0&api_timestamp=${timestamp}`)?.toUpperCase()

                },
                data: { "appId": "wx03527497c5369a2c", "appType": "WECHAT_MINI_PROGRAM", "osType": "windows", "model": "microsoft", "browser": "微信小程序", "platform": "1", "sourceType": "5", "sourceChannel": "会员小程序", "siteId": "", "visitorId": "", "deviceId": "", "spotId": "", "campaignId": "", "deviceType": "", "eventLabel": "", "eventValue": "", "eventAttr2": `${eventAttr2}`, "eventAttr3": "", "eventAttr4": "", "eventAttr5": "", "eventAttr6": "", "googleCampaignName": "", "googleCampaignSource": "", "googleCampaignMedium": "", "googleCampaignContent": "", "memberType": "DeRUCCI", "customId": `${this.customId}`, "locationUrl": "/pages/user/signIn", "url": "/pages/user/signIn", "pageTitle": "每日签到", "logType": "event", "behaviorIds": [1, 3], "eventCategory": "用户签到", "eventAction": "签到", "eventAttr1": 2, "openId": `${this.openId}` }
            }
            let { data: result } = await axios.request(options)

            if (result?.msg === "success") {
                console.log(`账号[${this.index}] 签到成功，获得积分：${result?.data?.point}`)
            } else {
                console.log(`账号[${this.index}] 签到失败：${result?.msg || JSON.stringify(result)}`)
            }

        } catch (e) {
            console.log(e)
        }
    }

}

!(async () => {
    if (true) {
        for (let user of SERVERS) {
            const ref = refOf(user);
            try {
                await new Task(user).run();
                setStatus(ref, "ready", "", "wx03527497c5369a2c");
            } catch (error) {
                markFromError(ref, error && error.message ? error.message : error, "wx03527497c5369a2c");
                console.log(`账号 ${ref} 已记录公共状态：${error && error.message ? error.message : error}`);
            }
        }
    } else {
        
        console.log(`${"YYB_SERVER"}未配置微信SERVER配置 搭建可看仓库目录下的readme.md❌`)
        return
    }

})()
    .catch((e) => console.log(e))
    
