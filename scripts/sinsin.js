// name: sinsin
// cron: 0 6,18 * * *
const axios = require("axios");
const fs = require("fs");
const path = require("path");
const { filterAccounts, markFromError, setStatus, refOf } = require("./yyb-account-guard");
// ====================== YYB Go 账号（环境变量 YYB_SERVER = 地址@微信账号标识，多行） ======================
let SERVERS = (process.env.YYB_SERVER || "")
    .split(/\r?\n/)
    .map(s => s.trim())
    .filter(Boolean);
if (!SERVERS.length) {
    console.error("未配置环境变量 YYB_SERVER，请设置后重试（格式：地址@微信账号标识，多行换行）");
    process.exit(1);
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
        const { data } = await axios.post(url, { ref, app_id: MINI_APP_ID }, { timeout: 20000, proxy: false });
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

const querystring = require("querystring");

const MINI_APP_ID = "wxdc40acf03fc92e6f";
SERVERS = filterAccounts(SERVERS, refOf, { appId: MINI_APP_ID });
const KDT_ID = "126240508";
const USER_VERSION = "2.235.5.101";
const API_BASE = "https://h5.youzan.com";
const UIC_BASE = "https://uic.youzan.com";
const TOKEN_CACHE_FILE = path.join(__dirname, "token_caches", "sinsin_token_cache.json");
try { fs.mkdirSync(path.dirname(TOKEN_CACHE_FILE), { recursive: true }); } catch (e) {}
const USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) MicroMessenger/3.9.12 MiniProgramEnv/Windows WindowsWechat/WMPF";

function readTokenCache() {
    try {
        if (!fs.existsSync(TOKEN_CACHE_FILE)) return {};
        return JSON.parse(fs.readFileSync(TOKEN_CACHE_FILE, "utf8")) || {};
    } catch (e) {
        return {};
    }
}

function writeTokenCache(cache) {
    try {
        fs.writeFileSync(TOKEN_CACHE_FILE, JSON.stringify(cache, null, 2), "utf8");
    } catch (e) {
        console.log(`写入token缓存失败: ${e.message || e}`);
    }
}

function maskPhone(phone = "") {
    return String(phone).replace(/^(\d{3})\d{4}(\d{4})$/, "$1****$2");
}

function maskToken(token = "") {
    if (!token) return "";
    return token.length > 12 ? `${token.slice(0, 6)}***${token.slice(-6)}` : `${token.slice(0, 3)}***`;
}

class Task {
    constructor(openid) {
        this.server = openid;
        const _yyb = parseYybGoEntry(this.server);
        this.ref = _yyb.ref;
        this.openid = _yyb.ref;
        this.index = userIdx++;
        this.openid = String(openid || "").trim();
        this.token = {};
        this.checkinId = "";
    }

    async run() {
        const cached = this.getCachedToken();
        if (cached) {
            this.token = cached;
            console.log(`账号[${this.index}] 使用缓存session: ${maskToken(this.sessionId)}`);
            if (!(await this.checkToken())) {
                this.removeCachedToken();
                console.log(`账号[${this.index}] 缓存session失效，重新code登录`);
            }
        }

        if (!this.sessionId) await this.loginByWxCode();
        if (!this.sessionId) return;

        await this.getPoints("签到前");
        await this.getCheckinInfo();
        await this.doCheckin();
        await this.getPoints("签到后");
    }

    getCachedToken() {
        const cache = readTokenCache();
        return cache[this.openid] || null;
    }

    saveCachedToken() {
        if (!this.sessionId) return;
        const cache = readTokenCache();
        cache[this.openid] = {
            ...this.token,
            updatedAt: new Date().toISOString(),
        };
        writeTokenCache(cache);
    }

    removeCachedToken() {
        const cache = readTokenCache();
        if (cache[this.openid]) {
            delete cache[this.openid];
            writeTokenCache(cache);
        }
        this.token = {};
    }

    get accessToken() {
        return this.token.accessToken || this.token.access_token || "";
    }

    get sessionId() {
        return this.token.sessionId || this.token.session_id || "";
    }

    buildExtraData() {
        return JSON.stringify({
            is_weapp: 1,
            sid: this.sessionId,
            version: USER_VERSION,
            client: "weapp",
            bizEnv: "wsc",
        });
    }

    buildUrl(apiPath, query = {}) {
        const pathname = apiPath.replace(/^\/+/, "");
        const params = querystring.stringify({
            store_id: "",
            app_id: MINI_APP_ID,
            kdt_id: KDT_ID,
            access_token: this.accessToken,
            ...query,
        });
        return `${API_BASE}/${pathname}?${params}`;
    }

    getHeaders(extra = {}) {
        return {
            "User-Agent": USER_AGENT,
            "Referer": `https://servicewechat.com/${MINI_APP_ID}/54/page-frame.html`,
            "Extra-Data": this.buildExtraData(),
            "Cookie": this.sessionId ? `KDTSESSIONID=${this.sessionId}; yz_log_seqb=1` : "",
            "Accept": "application/json, text/plain, */*",
            ...extra,
        };
    }

    async request({ method = "GET", apiPath, query = {}, data = {} }) {
        const options = {
            method,
            url: this.buildUrl(apiPath, query),
            headers: this.getHeaders(method === "POST" ? { "Content-Type": "application/json" } : {}),
            timeout: 15000,
            validateStatus: () => true,
        };
        if (method !== "GET") options.data = data;

        const { status, data: result } = await axios.request(options);
        if (status !== 200) throw new Error(`HTTP ${status}: ${typeof result === "string" ? result.slice(0, 200) : JSON.stringify(result)}`);
        if (!result || result.code !== 0) {
            const err = new Error(result?.msg || result?.message || JSON.stringify(result));
            err.code = result?.code;
            throw err;
        }
        return result.data;
    }

    async getWxCode() {
        return await getCode(this.server);
    }

    async loginByWxCode() {
        try {
            const code = await this.getWxCode();
            const { status, data } = await axios.post(
                `${UIC_BASE}/passport/general/auth.json`,
                {
                    appId: MINI_APP_ID,
                    code,
                    platformName: "weapp",
                    signature: "Windows",
                },
                {
                    headers: {
                        "Content-Type": "application/json",
                        "User-Agent": USER_AGENT,
                        "Referer": `https://servicewechat.com/${MINI_APP_ID}/54/page-frame.html`,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout: 15000,
                    validateStatus: () => true,
                }
            );
            if (status !== 200 || data?.code !== 0 || !data?.data?.sessionId) {
                throw new Error(`UIC登录失败: HTTP ${status} ${typeof data === "string" ? data.slice(0, 200) : JSON.stringify(data)}`);
            }
            this.token = data.data || {};
            this.saveCachedToken();
            console.log(`账号[${this.index}] 登录成功: ${data.data?.nickName || data.data?.nickname || ""} ${maskPhone(data.data?.mobile)}`);
        } catch (e) {
            console.log(`账号[${this.index}] 登录失败: ${e.message || e}`);
        }
    }

    async checkToken() {
        try {
            await this.getPoints("缓存校验");
            return true;
        } catch (e) {
            return false;
        }
    }

    async getPoints(label = "积分") {
        const data = await this.request({ apiPath: "wscump/integral/user_points.json" });
        const points = data?.current_points ?? data?.real_points ?? data?.total_points ?? "未知";
        console.log(`账号[${this.index}] ${label}: ${points}积分`);
        return data;
    }

    async getCheckinInfo() {
        const data = await this.request({ apiPath: "wscump/checkin/show_checkin_page_v2.json" });
        this.checkinId = data?.checkinId || data?.checkin_id || "";
        console.log(`账号[${this.index}] 签到活动: checkinId=${this.checkinId || "未获取"} isShow=${data?.isShow} showPage=${data?.showPage}`);
        return data;
    }

    async doCheckin() {
        if (!this.checkinId) {
            console.log(`账号[${this.index}] 未获取到签到活动，跳过`);
            return;
        }
        try {
            const data = await this.request({
                apiPath: "wscump/checkin/checkinV2.json",
                query: { checkinId: this.checkinId },
            });
            const award = (data?.list || [])
                .map((item) => item?.infos?.title || item?.infos?.desc || "")
                .filter(Boolean)
                .join(", ");
            console.log(`账号[${this.index}] 签到成功: ${data?.desc || ""}${award ? ` ${award}` : ""}`);
        } catch (e) {
            const message = String(e.message || e);
            if (/已签到|已经签到|重复|今日.*签|参与次数|最大参与次数/.test(message)) {
                console.log(`账号[${this.index}] 今日已签到`);
                return;
            }
            if (/手机号未授权|mobile.*authorized/i.test(message)) {
                markFromError(this.ref, message, MINI_APP_ID);
                console.log(`账号[${this.index}] 签到失败: 用户手机号未授权，请先在小程序内完成手机号授权`);
                return;
            }
            console.log(`账号[${this.index}] 签到失败: ${message}`);
            if (e.code === -1 || e.code === 40010 || e.code === 40009 || /登录|token|session|access/i.test(message)) this.removeCachedToken();
        }
    }
}

!(async () => {
    
    for (const openid of SERVERS) {
        const task = new Task(openid);
        await task.run();
        setStatus(task.ref, "ready", "", MINI_APP_ID);
    }
})()
    .catch((e) => console.log(e.message || e))
