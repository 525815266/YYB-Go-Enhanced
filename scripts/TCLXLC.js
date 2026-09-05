// name: 同程旅行里程
// cron: 21 8 * * *
const axios = require("axios");
const dns = require("dns");
const https = require("https");
const { filterAccounts, markFromError, setStatus } = require("./yyb-account-guard");

dns.setDefaultResultOrder("ipv4first");

// ====================== YYB Go 账号（环境变量 YYB_SERVER = 地址@微信账号标识，多行） ======================
const SERVERS = (process.env.YYB_SERVER || "")
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

const APP = {
    name: "同程旅行里程签到",
    appid: "wx336dcaf6a1ecf632"
};

const ACTIVE_SERVERS = filterAccounts(SERVERS, value => parseYybGoEntry(value).ref, { appId: APP.appid, log: console.log });

const USER_AGENT =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
    "AppleWebKit/537.36 (KHTML, like Gecko) " +
    "MicroMessenger/3.9.12 " +
    "MiniProgramEnv/Windows " +
    "WindowsWechat/WMPF";

const httpsAgent = new https.Agent({ keepAlive: true, family: 4 });

function short(value, max = 220) {
    if (value === undefined || value === null) return "";
    let text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > max ? text.slice(0, max) + "..." : text;
}

function formatDate(date = new Date()) {
    const pad = n => String(n).padStart(2, "0");
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
}

function getFiveDays() {
    let arr = [];
    for (let i = -2; i <= 2; i++) {
        let d = new Date();
        d.setDate(d.getDate() + i);
        arr.push(formatDate(d));
    }
    return arr;
}

async function request(options) {
    let retry = 3;
    while (retry > 0) {
        try {
            const timeout = options.url.includes("wxUser/login") ? 60000 : 30000;
            const res = await axios.request({
                timeout,
                httpsAgent,
                family: 4,
                validateStatus: () => true,
                ...options,
                headers: {
                    "User-Agent": USER_AGENT,
                    Accept: "application/json, text/plain, */*",
                    Connection: "keep-alive",
                    ...(options.headers || {})
                }
            });
            return { status: res.status, headers: res.headers || {}, data: res.data };
        } catch (e) {
            retry--;
            console.log("请求失败 " + options.url + " 剩余重试:" + retry + " " + e.message);
            if (retry <= 0) throw e;
            await new Promise(r => setTimeout(r, 3000));
        }
    }
}

async function getCode(server, appid) {
    const { server: parsedServer, ref } = parseYybGoEntry(server);
    if (!parsedServer || !ref) return null;
    const url = "http://" + parsedServer + "/wxapp/getCode";
    try {
        const { data } = await axios.post(url, {
            ref,
            app_id: appid || APP.appid
        }, { timeout: 20000, proxy: false });
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

class Tongcheng {
    constructor(serverEntry) {
        this.serverEntry = serverEntry;
        const { ref } = parseYybGoEntry(serverEntry);
        this.ref = ref;
        this.loginInfo = {};
    }

    wait(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    headers(extra = {}) {
        const sectoken = this.loginInfo.sectoken || "";
        return {
            apmat: `${this.loginInfo.openId || ""}|${new Date().toISOString().slice(0, 16).replace(/[-T:]/g, "")}|${Math.floor(Math.random() * 1000000)}`,
            TCSecTk: sectoken,
            tcsectk: sectoken,
            TCxcxVersion: "10.8.7",
            platform: "WX_MP",
            osType: "2",
            secToken: sectoken,
            "TC-MALL-PLATFORM-CODE": "WX_MP",
            "TC-MALL-USER-TOKEN": sectoken,
            ...extra
        };
    }

    async login() {
        const code = await getCode(this.serverEntry, APP.appid);
        if (!code) throw new Error("获取code失败");

        const res = await request({
            method: "POST",
            url: "https://wx.17u.cn/wechatappapi/wxUser/login",
            headers: {
                "content-type": "application/json",
                Referer: "https://servicewechat.com/wx336dcaf6a1ecf632/"
            },
            data: { code, scene: 1001 }
        });

        console.log("登录接口 HTTP=" + res.status);
        console.log("登录返回=" + short(res.data, 300));

        const content = res.data?.content || res.data?.data || {};
        if (res.status !== 200 || !content.openId) {
            throw new Error("登录失败 HTTP " + res.status + ": " + short(res.data));
        }

        this.loginInfo = {
            openId: content.openId,
            encryOpenId: content.encryOpenId,
            aesOpenId: content.aesOpenId,
            unionId: content.unionId,
            aesUnionId: content.aesUnionId,
            memberId: content.memberId,
            sectoken: content.sectoken
        };

        return "openId=" + content.openId + " memberId=" + (content.memberId || "");
    }

    async query() {
        const member = await request({
            method: "GET",
            url: "https://wx.17u.cn/wechatmypubapi/myInfo/memberInfo",
            headers: this.headers()
        });

        const mileage = await request({
            method: "POST",
            url: "https://tcmobileapi.17usoft.com/mallgatewayapi/userApi/mileages/remain",
            headers: this.headers({
                "content-type": "application/json",
                "TC-MALL-DEPT-CODE": "iH3PGf9ZucSMMEYi4keylA==",
                "TC-MALL-CLIENT": "API_CLIENT",
                "TC-MALL-OS-TYPE": "Android"
            }),
            data: { osType: 2 }
        });

        const remain = mileage.data?.data?.remainBalance ?? mileage.data?.data?.balance ?? mileage.data?.remainBalance;
        const content = member.data?.content || member.data?.data?.content || {};

        return "会员=" + short(content.memberBanner || content.memberRights || content, 100) + " 里程=" + (remain ?? short(mileage.data, 100));
    }

    async sign() {
        const days = getFiveDays();

        const calendar = await request({
            method: "POST",
            url: "https://wx.17u.cn/wxmpsign/sign/signCalendar",
            headers: this.headers({ "content-type": "application/json" }),
            data: { beginDate: days[0], endDate: days[4] }
        });

        const signInfo = await request({
            method: "POST",
            url: "https://wx.17u.cn/wxmpsign/sign/getSignInfo",
            headers: this.headers({ "content-type": "application/json" }),
            data: {}
        });

        const info = signInfo.data?.data || {};
        const cal = calendar.data?.data || {};

        if (info.todaySigned || cal.todaySigned) {
            return "今日已签到，连续=" + (info.periodContinuedSignDays ?? cal.periodContinuedSignDays ?? "未知") + "天";
        }

        const sign = await request({
            method: "POST",
            url: "https://wx.17u.cn/wxmpsign/sign/saveSignInfo",
            headers: this.headers({ "content-type": "application/json" }),
            data: {}
        });

        return "签到接口返回:" + short(sign.data);
    }

    async getTaskList(schemeGuid = "task-2025-nflygijg") {
        const res = await request({
            method: "POST",
            url: "https://wx.17u.cn/qiushiinnerapi/task/detailList",
            headers: this.headers({
                "content-type": "application/json",
                "tcreferer": encodeURIComponent("page/AC/sign/msindex/msindex"),
                "tcxcxversion": "8.1.5",
                "ostype": "0",
                "tcprivacy": "1",
                "Referer": "https://servicewechat.com/wx336dcaf6a1ecf632/920/page-frame.html"
            }),
            data: { detailGuid: "", pageNum: 1, pageSize: 999, schemeGuid: schemeGuid }
        });

        if (res.data.code !== 0 || !res.data.data?.taskDetails) {
            throw new Error("获取任务列表失败: " + short(res.data));
        }

        return { schemeGuid: res.data.data.taskScheme?.schemeGuid || schemeGuid, list: res.data.data.taskDetails };
    }

    async getTaskDetail(detailGuid, schemeGuid) {
        const res = await request({
            method: "POST",
            url: "https://wx.17u.cn/qiushiinnerapi/task/detail",
            headers: this.headers({
                "content-type": "application/json",
                "tcreferer": encodeURIComponent("page/home/mall/mall"),
                "tcxcxversion": "8.1.5",
                "ostype": "0",
                "tcprivacy": "1",
                "Referer": "https://servicewechat.com/wx336dcaf6a1ecf632/920/page-frame.html"
            }),
            data: { detailGuid, schemeGuid }
        });
        return res.data;
    }

    async finishTask(detailGuid, schemeGuid) {
        const res = await request({
            method: "POST",
            url: "https://wx.17u.cn/qiushiinnerapi/task/finishTask",
            headers: this.headers({
                "content-type": "application/json",
                "tcreferer": encodeURIComponent("page/home/mall/mall"),
                "tcxcxversion": "8.1.5",
                "ostype": "0",
                "tcprivacy": "1",
                "Referer": "https://servicewechat.com/wx336dcaf6a1ecf632/920/page-frame.html"
            }),
            data: { detailGuid, pageNum: 1, pageSize: 999, schemeGuid }
        });
        return res.data;
    }

    async sendTaskPrize(detailGuid, schemeGuid) {
        const res = await request({
            method: "POST",
            url: "https://wx.17u.cn/qiushiinnerapi/task/sendTaskPrize",
            headers: this.headers({
                "content-type": "application/json",
                "tcreferer": encodeURIComponent("page/home/mall/mall"),
                "tcxcxversion": "8.1.5",
                "ostype": "0",
                "tcprivacy": "1",
                "Referer": "https://servicewechat.com/wx336dcaf6a1ecf632/920/page-frame.html"
            }),
            data: { detailGuid, pageNum: 1, pageSize: 999, schemeGuid }
        });
        return res.data;
    }

    async doTasks() {
        try {
            const { schemeGuid, list } = await this.getTaskList();
            const todoTasks = list.filter(item => item.status === 0 || item.status === 2);

            if (todoTasks.length === 0) {
                return "今日所有任务已完成并领取";
            }

            const result = [];
            for (const task of todoTasks) {
                try {
                    const reward = task.prizeTitle + "里程";

                    if (task.status === 0) {
                        const finishRes = await this.finishTask(task.detailGuid, schemeGuid);
                        if (finishRes.code !== 0) {
                            result.push("❌ " + task.title + "：完成失败 - " + short(finishRes, 80));
                            await this.wait(2000);
                            continue;
                        }
                        await this.wait(1000);
                    }

                    const prizeRes = await this.sendTaskPrize(task.detailGuid, schemeGuid);
                    await this.wait(1000);

                    const detailRes = await this.getTaskDetail(task.detailGuid, schemeGuid);
                    const finalStatus = detailRes.data?.taskDetails?.[0]?.status;

                    if (prizeRes.code === 0 && finalStatus === 3) {
                        result.push("✅ " + task.title + "：完成并领取成功，获得" + reward);
                    } else if (prizeRes.code === 0) {
                        result.push("⚠️ " + task.title + "：领取接口成功，最终状态=" + finalStatus);
                    } else {
                        result.push("❌ " + task.title + "：领取失败 - " + short(prizeRes, 80));
                    }
                } catch (e) {
                    result.push("❌ " + task.title + "：执行异常 - " + e.message);
                }
                await this.wait(2000 + Math.floor(Math.random() * 1000));
            }

            return "共" + todoTasks.length + "个待处理任务\n" + result.join("\n");
        } catch (e) {
            return "任务模块执行失败：" + e.message;
        }
    }
}

// ====================== 主流程 ======================
async function main() {
    console.log("┌─────────────────────────────┐");
    console.log("│ 同程旅行里程签到 │");
    console.log("└─────────────────────────────┘");

    for (let i = 0; i < ACTIVE_SERVERS.length; i++) {
        const { server, ref } = parseYybGoEntry(ACTIVE_SERVERS[i]);
        if (!server || !ref) {
            console.log("✗ YYB_SERVER 第" + (i + 1) + "行格式无效，跳过");
            continue;
        }

        console.log("\n========== 账号[" + (i + 1) + "] " + ref + " ==========");

        const runner = new Tongcheng(ACTIVE_SERVERS[i]);

        try {
            console.log("登录：" + await runner.login());
            console.log("查询：" + await runner.query());
            console.log("签到：" + await runner.sign());
            console.log("任务：" + await runner.doTasks());
            setStatus(ref, "ready", "", APP.appid);
        } catch (e) {
            console.log("执行失败：" + (e.stack || JSON.stringify(e)));
            markFromError(ref, e.message || e, APP.appid);
        }

        if (i < ACTIVE_SERVERS.length - 1) {
            const waitTime = Math.floor(Math.random() * 5000) + 5000;
            console.log("等待 " + (waitTime / 1000) + " 秒后执行下一个账号");
            await new Promise(resolve => setTimeout(resolve, waitTime));
        }
    }

    console.log("\n┌─────────────────────────────┐");
    console.log("│ 所有账户任务处理完成 │");
    console.log("└─────────────────────────────┘");
}

if (require.main === module) {
    main().catch(err => {
        console.log("✗ 脚本执行出错:", err);
    });
}
