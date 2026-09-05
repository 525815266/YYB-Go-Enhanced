"use strict";

// Node 版公共缓存；业务脚本在遍历 YYB_SERVER 前调用 filterAccounts。
const fs = require("fs");
const os = require("os");
const path = require("path");

const STATUS_FILE = process.env.YYB_ACCOUNT_STATUS_FILE ||
  (fs.existsSync("/ql/data/config") ? "/ql/data/config/yyb_account_status.json" :
    path.join(__dirname, "yyb_account_status.json"));
const TTL = {
  unbound: 24 * 60 * 60,
  unregistered: 24 * 60 * 60,
  code_unavailable: 30 * 60,
  temporary_error: 10 * 60,
  ready: 6 * 60 * 60,
  unknown: 15 * 60,
};
const UNBOUND = /未授权手机号|手机号未授权|未完成手机号授权|请先.{0,16}授权手机号|尚未注册.{0,16}(会员|小程序)|未注册(会员|小程序)|未绑定(此小程序|手机号|会员|账号)|尚未绑定|未登录.{0,12}(会员|账号)|微信授权成功.{0,20}未登录/i;
const TEMPORARY = /timeout|timed out|超时|HTTP\s*[45]\d\d|\b(502|503|504)\b|服务端异常|系统繁忙|活动太火爆|风控|third login fds limit|登录过期|token.{0,8}(失效|过期)/i;

function refOf(value) {
  if (value && typeof value === "object") value = value.ref || value.id || value.openid || "";
  const text = String(value || "").trim();
  return text.includes("@") ? text.slice(text.lastIndexOf("@") + 1).trim() : text;
}
function read() {
  try {
    const data = JSON.parse(fs.readFileSync(STATUS_FILE, "utf8"));
    if (data && data.accounts && typeof data.accounts === "object") return data;
  } catch (_) {}
  return { version: 1, updated_at: 0, accounts: {} };
}
function write(data) {
  fs.mkdirSync(path.dirname(STATUS_FILE), { recursive: true });
  const temp = path.join(path.dirname(STATUS_FILE), `.${path.basename(STATUS_FILE)}.${process.pid}.${Date.now()}.tmp`);
  data.version = 1;
  data.updated_at = Math.floor(Date.now() / 1000);
  fs.writeFileSync(temp, JSON.stringify(data, null, 2) + os.EOL, "utf8");
  fs.renameSync(temp, STATUS_FILE);
}
function classifyError(message) {
  const text = String(message || "").replace(/[\r\n]+/g, " ").trim();
  if (!text) return null;
  if (TEMPORARY.test(text)) return { status: "temporary_error", reason: text.slice(0, 240) };
  if (UNBOUND.test(text)) return { status: /注册|会员/.test(text) ? "unregistered" : "unbound", reason: text.slice(0, 240) };
  return null;
}
function setStatus(ref, status, reason = "", appId = "") {
  const key = refOf(ref);
  if (!key) return;
  const data = read();
  const storageKey = appId ? `${key}::${appId}` : key;
  data.accounts[storageKey] = { ref: key, status, reason: String(reason || "").slice(0, 240), app_id: appId || "", checked_at: Math.floor(Date.now() / 1000) };
  write(data);
}
function markFromError(ref, message, appId = "") {
  const result = classifyError(message);
  if (!result) return null;
  setStatus(ref, result.status, result.reason, appId);
  return result.status;
}
function shouldSkip(ref, appId = "") {
  const key = refOf(ref);
  if (!key || process.env.YYB_GUARD_BYPASS === "1") return { skip: false, reason: "" };
  const accounts = read().accounts;
  const item = accounts[appId ? `${key}::${appId}` : key];
  if (!item) return { skip: false, reason: "" };
  if (item.status === "disabled") return { skip: true, reason: item.reason || "账号已停用" };
  const ttl = TTL[item.status] || TTL.unknown;
  const checked = Number(item.checked_at || 0);
  if (checked && Date.now() / 1000 - checked < ttl && ["unbound", "unregistered", "code_unavailable", "temporary_error"].includes(item.status)) {
    return { skip: true, reason: item.reason || item.status };
  }
  return { skip: false, reason: "" };
}
function filterAccounts(values, refGetter = value => value, options = {}) {
  const appId = typeof options === "string" ? options : options.appId || "";
  const log = typeof options === "object" ? (options.log || console.log) : console.log;
  return values.filter(value => {
    const ref = refOf(refGetter(value));
    const result = shouldSkip(ref, appId);
    if (result.skip && log) log(`账号 ${ref} 已由 YYB 公共缓存跳过：${result.reason}`);
    return !result.skip;
  });
}

module.exports = { STATUS_FILE, classifyError, filterAccounts, markFromError, refOf, setStatus, shouldSkip };
