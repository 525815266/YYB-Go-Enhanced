<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { message } from "ant-design-vue";
import { Check, Copy, RefreshCw } from "@lucide/vue";
import { api, accountName, errorMessage, type Account, APIError } from "../api";
import { useSession } from "../session";
import { copyText } from "../clipboard";

interface Profile {
  id: number;
  name: string;
  provider: string;
}
interface Area {
  code: string;
  name: string;
}
interface Location {
  province: string;
  city: string;
  province_code: string;
  city_code: string;
  source: string;
}
interface QR {
  session_id: string;
  image_base64?: string;
  image_url: string;
  proxy?: string;
}
interface QuickConfig {
  session_id: string;
  ports: number[];
  appid: string;
  scope: string;
  redirect_uri: string;
  state: string;
}
interface QuickProfile {
  authorize_uuid: string;
  nickname?: string;
  headimgurl?: string;
}
const router = useRouter(),
  session = useSession();
const mode = ref<"qr" | "quick">("qr");
const qr = ref<QR | null>(null),
  status = ref(""),
  statusType = ref<"info" | "error" | "success">("info");
const imageFailed = ref(false),
  remaining = ref(0),
  loading = ref(false),
  confirming = ref(false);
const account = ref<Account | null>(null),
  remark = ref(""),
  actionBusy = ref(false),
  actionNote = ref("");
const proxy = reactive({
  mode: "direct",
  proxy_type: "http",
  static_proxy: "",
  api_url: "",
  provider_profile_id: null as number | null,
  province: "all",
  city: "",
});
const profiles = ref<Profile[]>([]),
  provinces = ref<Area[]>([]),
  cities = ref<Area[]>([]);
const proxyMessage = ref(""),
  proxyBusy = ref(false),
  location = ref<Location | null>(null);
const quickConfig = ref<QuickConfig | null>(null),
  quickProfile = ref<QuickProfile | null>(null),
  quickPort = ref(0);
const quickBusy = ref(false),
  quickState = ref("");
const proxyName = computed(() =>
  proxy.mode === "direct"
    ? "直连"
    : proxy.mode === "profile"
      ? `${profiles.value.find((item) => item.id === proxy.provider_profile_id)?.name || "供应商配置"} · ${cities.value.find((item) => item.code === proxy.city)?.name || provinces.value.find((item) => item.code === proxy.province)?.name || "全国"}`
      : proxy.mode === "static"
        ? "静态代理"
        : "自定义动态 API",
);
let pollTimer: number | undefined,
  expiryTimer: number | undefined,
  retryTimer: number | undefined;
let disposed = false,
  pollBusy = false,
  attempts = 0,
  generation = 0;
function clearTimers() {
  if (pollTimer) clearInterval(pollTimer);
  if (expiryTimer) clearInterval(expiryTimer);
  if (retryTimer) clearTimeout(retryTimer);
  pollTimer = expiryTimer = retryTimer = undefined;
}
async function retire() {
  const id = qr.value?.session_id;
  qr.value = null;
  if (id)
    try {
      await api(`/qr/${encodeURIComponent(id)}/cancel`, "POST");
    } catch {
      /* old session may already be gone */
    }
}
function payload() {
  const profileMode = proxy.mode === "profile";
  return {
    product: "appstore",
    mode: profileMode ? "api" : proxy.mode,
    proxy_type: proxy.proxy_type,
    static_proxy: proxy.static_proxy.trim(),
    api_url: proxy.api_url.trim(),
    provider_profile_id: profileMode ? proxy.provider_profile_id : null,
    region_code: profileMode ? proxy.city || proxy.province : "",
    region_province: profileMode
      ? provinces.value.find((item) => item.code === proxy.province)?.name || ""
      : "",
    region_city: profileMode
      ? cities.value.find((item) => item.code === proxy.city)?.name || ""
      : "",
    refresh_ahead_minutes: 5,
  };
}
async function loadCities(code: string) {
  cities.value =
    code === "all"
      ? []
      : await api<Area[]>(
          `/api/proxy-profiles/areas/cities?province=${encodeURIComponent(code)}`,
        );
  proxy.city = "";
}
async function loadProxyOptions() {
  try {
    const [items, areas] = await Promise.all([
      api<Profile[]>("/api/proxy-profiles"),
      api<Area[]>("/api/proxy-profiles/areas/provinces"),
    ]);
    profiles.value = items;
    provinces.value = areas;
    if (!proxy.provider_profile_id)
      proxy.provider_profile_id = items[0]?.id || null;
  } catch (cause) {
    proxyMessage.value = `代理配置读取失败：${errorMessage(cause)}`;
  }
}
async function recommend() {
  proxyBusy.value = true;
  proxyMessage.value = "正在匹配代理地区";
  let coordinates: Record<string, number> = {};
  if (window.isSecureContext && navigator.geolocation) {
    try {
      const point = await new Promise<GeolocationPosition>((resolve, reject) =>
        navigator.geolocation.getCurrentPosition(resolve, reject, {
          timeout: 8000,
          maximumAge: 300000,
        }),
      );
      coordinates = {
        latitude: point.coords.latitude,
        longitude: point.coords.longitude,
      };
    } catch {
      /* server can use public IP */
    }
  }
  try {
    location.value = await api<Location>(
      "/api/proxy-location/recommend",
      "POST",
      coordinates,
    );
    proxyMessage.value = "地区已推荐，采用后还需应用到授权";
  } catch (cause) {
    proxyMessage.value = errorMessage(cause);
  } finally {
    proxyBusy.value = false;
  }
}
async function useLocation() {
  if (!location.value) return;
  if (!profiles.value.length) {
    proxyMessage.value = "请先在代理设置中添加供应商配置";
    return;
  }
  proxy.mode = "profile";
  proxy.province = location.value.province_code;
  try {
    await loadCities(proxy.province);
    proxy.city = location.value.city_code || "";
    proxyMessage.value = "地区已预填，请点击应用到授权";
    location.value = null;
  } catch (cause) {
    proxyMessage.value = errorMessage(cause);
  }
}
async function testProxy() {
  proxyBusy.value = true;
  try {
    const result = await api<{
      resolved: boolean;
      proxy?: string;
      exit_region?: string;
      exit_city?: string;
      probe_error?: string;
    }>("/accounts/proxy/test", "POST", payload());
    proxyMessage.value = result.resolved
      ? `代理 ${result.proxy}；实际出口 ${[result.exit_region, result.exit_city].filter(Boolean).join(" · ") || result.probe_error || "未能检测"}`
      : "直连模式无需代理";
  } catch (cause) {
    proxyMessage.value = `代理测试失败：${errorMessage(cause)}`;
  } finally {
    proxyBusy.value = false;
  }
}
function setStatus(text: string, type: "info" | "error" | "success" = "info") {
  status.value = text;
  statusType.value = type;
}
async function newQR() {
  const request = ++generation;
  clearTimers();
  await retire();
  if (disposed || request !== generation) return;
  account.value = null;
  imageFailed.value = false;
  loading.value = true;
  remaining.value = 0;
  setStatus("正在获取二维码");
  try {
    const result = await api<QR>("/qr?as_base64=true", "POST", payload());
    if (disposed || request !== generation) return;
    qr.value = result;
    setStatus(result.proxy ? `等待扫码 · ${result.proxy}` : "等待扫码");
    remaining.value = 110;
    expiryTimer = window.setInterval(() => {
      remaining.value--;
      if (remaining.value <= 0) void newQR();
    }, 1000);
    pollTimer = window.setInterval(poll, 1500);
  } catch (cause) {
    if (request === generation)
      setStatus(`获取二维码失败：${errorMessage(cause)}`, "error");
  } finally {
    if (request === generation) loading.value = false;
  }
}
async function poll() {
  const id = qr.value?.session_id;
  if (!id || pollBusy || disposed) return;
  pollBusy = true;
  try {
    const result = await api<{ status: string }>(
      `/qr/${encodeURIComponent(id)}/poll`,
    );
    if (qr.value?.session_id !== id || disposed) return;
    if (result.status === "pending") setStatus("等待扫码");
    else if (result.status === "scanned")
      setStatus("已扫码，请在手机上确认授权");
    else if (result.status === "authorized" || result.status === "confirmed") {
      clearTimers();
      void confirm(id);
    } else if (result.status === "expired" || result.status === "unknown")
      void newQR();
    else if (result.status === "cancelled") {
      clearTimers();
      setStatus("授权已取消，请重新生成二维码", "error");
    }
  } catch {
    setStatus("网络暂时波动，正在自动重试");
  } finally {
    pollBusy = false;
  }
}
async function confirm(id: string) {
  if (confirming.value || qr.value?.session_id !== id || disposed) return;
  confirming.value = true;
  remaining.value = 0;
  setStatus("授权成功，正在保存账号");
  try {
    const result = await api<Account>(
      `/qr/${encodeURIComponent(id)}/confirm`,
      "POST",
    );
    if (disposed || qr.value?.session_id !== id) return;
    account.value = result;
    remark.value = result.remark || "";
    attempts = 0;
    setStatus(
      `账号添加成功 · ${proxyName.value}（已有账号保留原配置）`,
      "success",
    );
  } catch (cause) {
    if (
      cause instanceof APIError &&
      [409, 502].includes(cause.status) &&
      attempts++ < 12
    ) {
      setStatus("授权已确认，正在自动完成账号保存");
      retryTimer = window.setTimeout(() => void confirm(id), 1500);
    } else setStatus(`保存账号失败：${errorMessage(cause)}`, "error");
  } finally {
    confirming.value = false;
  }
}
async function saveRemark() {
  if (!account.value || actionBusy.value) return false;
  actionBusy.value = true;
  actionNote.value = "";
  try {
    const result = await api<{ account: Account; warning?: string }>(
      "/accounts/remark",
      "PUT",
      { ref: String(account.value.id), remark: remark.value },
    );
    account.value = result.account;
    actionNote.value = result.warning || "备注已保存";
    return true;
  } catch (cause) {
    actionNote.value = `备注保存失败：${errorMessage(cause)}`;
    return false;
  } finally {
    actionBusy.value = false;
  }
}
async function sync() {
  if (!account.value || !(await saveRemark())) return;
  actionBusy.value = true;
  try {
    const result = await api<{ added: boolean }>("/api/qinglong/sync", "POST", {
      ref: String(account.value.id),
    });
    actionNote.value = result.added
      ? `已添加到面板 YYB_SERVER：账号 ${account.value.id}`
      : `面板 YYB_SERVER 已包含账号 ${account.value.id}`;
  } catch (cause) {
    actionNote.value = `同步失败：${errorMessage(cause)}`;
  } finally {
    actionBusy.value = false;
  }
}
async function copy() {
  if (!account.value?.openid) return;
  try {
    await copyText(account.value.openid);
    message.success("OpenID 已复制");
  } catch {
    actionNote.value = "复制失败，请手动选择 OpenID";
  }
}
function switchMode(next: "qr" | "quick") {
  if (next === mode.value) return;
  mode.value = next;
  if (next === "quick") {
    ++generation;
    clearTimers();
    void retire();
    void detectQuick();
  } else void newQR();
}
async function localWechat(
  port: number,
  path: string,
  body: unknown,
  timeout = 3000,
) {
  const controller = new AbortController(),
    timer = window.setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(
      `https://localhost.weixin.qq.com:${port}${path}`,
      {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const value = await response.json();
    return typeof value === "string" ? JSON.parse(value) : value;
  } finally {
    clearTimeout(timer);
  }
}
async function detectQuick() {
  if (quickBusy.value || !session.pcLoginEnabled) return;
  quickBusy.value = true;
  quickState.value = "正在检测本机微信";
  quickConfig.value = null;
  quickProfile.value = null;
  try {
    const config = await api<QuickConfig>("/quick-login", "POST", payload());
    const matches = await Promise.all(
      config.ports.map(async (port) => {
        try {
          return {
            port,
            result: await localWechat(port, "/api/check-login", {
              apiname: "qrconnectchecklogin",
              jsdata: {
                appid: config.appid,
                scope: config.scope,
                redirect_uri: config.redirect_uri,
                state: config.state,
              },
            }),
          };
        } catch {
          return null;
        }
      }),
    );
    const match = matches.find(
      (item) =>
        item &&
        Number(item.result?.errcode) === 0 &&
        item.result?.jsdata?.authorize_uuid,
    );
    if (!match) throw new Error("未检测到可用的桌面微信");
    quickConfig.value = config;
    quickPort.value = match.port;
    quickProfile.value = match.result.jsdata;
    quickState.value = `${quickProfile.value?.nickname || "本机微信已就绪"}，可在桌面微信确认授权`;
  } catch (cause) {
    quickState.value = `${errorMessage(cause)}，请使用手机扫码`;
  } finally {
    quickBusy.value = false;
  }
}
async function authorizeQuick() {
  if (!quickConfig.value || !quickProfile.value || quickBusy.value) return;
  quickBusy.value = true;
  account.value = null;
  quickState.value = "等待微信确认";
  const config = quickConfig.value,
    profile = quickProfile.value;
  const x = Math.round((window.screenX || 0) + window.outerWidth / 2 - 180);
  const y = Math.round((window.screenY || 0) + window.outerHeight / 2 - 132);
  try {
    const result = await localWechat(
      quickPort.value,
      "/api/authorize",
      {
        apiname: "qrconnectfastauthorize",
        jsdata: {
          data: JSON.stringify({ x, y }),
          appid: config.appid,
          scope: config.scope,
          redirect_uri: config.redirect_uri,
          state: config.state,
          authorize_uuid: profile.authorize_uuid,
        },
      },
      120000,
    );
    if (Number(result.errcode) !== 0 || !result.jsdata?.redirect_url)
      throw new Error(
        Number(result.errcode) === 10050 ? "已拒绝授权" : "未返回有效授权结果",
      );
    const next = await api<Account>(
      `/quick-login/${config.session_id}/confirm`,
      "POST",
      { redirect_url: result.jsdata.redirect_url },
    );
    account.value = next;
    remark.value = next.remark || "";
    quickState.value = "账号添加成功";
  } catch (cause) {
    quickState.value = `快速授权失败：${errorMessage(cause)}`;
    quickConfig.value = null;
  } finally {
    quickBusy.value = false;
  }
}
onMounted(() => {
  void loadProxyOptions();
  if (session.pcLoginEnabled) switchMode("quick");
  else void newQR();
});
onBeforeUnmount(() => {
  disposed = true;
  ++generation;
  clearTimers();
  void retire();
});
</script>
<template>
  <main class="page">
    <div class="page-head">
      <h1>扫码添加</h1>
      <a-button @click="router.push('/accounts')">账号列表</a-button>
    </div>
    <section class="section">
      <h2>微信授权</h2>
      <details class="scan-proxy-options">
        <summary>登录网络代理 · {{ proxyName }}</summary>
        <div class="field-grid" style="margin-top: 16px">
          <a-form-item label="连接模式"
            ><a-select
              v-model:value="proxy.mode"
              :options="[
                { value: 'direct', label: '直连' },
                { value: 'static', label: '静态代理' },
                { value: 'api', label: '动态代理 API' },
                { value: 'profile', label: '供应商配置' },
              ]"
          /></a-form-item>
          <a-form-item
            v-if="proxy.mode === 'static' || proxy.mode === 'api'"
            label="代理协议"
            ><a-select
              v-model:value="proxy.proxy_type"
              :options="[
                { value: 'http', label: 'HTTP CONNECT' },
                { value: 'socks5', label: 'SOCKS5' },
              ]"
          /></a-form-item>
          <a-form-item
            v-if="proxy.mode === 'static'"
            class="wide"
            label="代理地址"
            ><a-input v-model:value="proxy.static_proxy" autocomplete="off"
          /></a-form-item>
          <a-form-item
            v-if="proxy.mode === 'api'"
            class="wide"
            label="代理提取 API"
            ><a-input v-model:value="proxy.api_url" autocomplete="off"
          /></a-form-item>
          <template v-if="proxy.mode === 'profile'"
            ><a-form-item label="供应商配置"
              ><a-select
                v-model:value="proxy.provider_profile_id"
                :options="
                  profiles.map((item) => ({ value: item.id, label: item.name }))
                " /></a-form-item
            ><a-form-item label="省份"
              ><a-select
                v-model:value="proxy.province"
                :options="
                  provinces.map((item) => ({
                    value: item.code,
                    label: item.name,
                  }))
                "
                @change="
                  (value: string | number) =>
                    loadCities(String(value)).catch(
                      (cause) => (proxyMessage = errorMessage(cause)),
                    )
                " /></a-form-item
            ><a-form-item label="城市"
              ><a-select
                v-model:value="proxy.city"
                :options="[
                  { value: '', label: '全省' },
                  ...cities.map((item) => ({
                    value: item.code,
                    label: item.name,
                  })),
                ]" /></a-form-item
          ></template>
        </div>
        <div class="actions">
          <a-button :loading="proxyBusy" @click="recommend"
            >推荐代理地区</a-button
          ><a-button :loading="proxyBusy" @click="testProxy">测试代理</a-button
          ><a-button
            type="primary"
            :disabled="proxyBusy"
            @click="mode === 'qr' ? newQR() : detectQuick()"
            >应用到授权</a-button
          >
        </div>
        <div v-if="location" class="actions" style="margin-top: 12px">
          <span>推荐 {{ location.province }} · {{ location.city }}</span
          ><a-button @click="useLocation">采用地区</a-button
          ><a-button @click="location = null">忽略</a-button>
        </div>
        <a-alert
          v-if="proxyMessage"
          style="margin-top: 12px"
          type="info"
          show-icon
          :message="proxyMessage"
        />
      </details>
      <a-segmented
        v-if="session.pcLoginEnabled"
        :value="mode"
        style="margin: 18px 0"
        :options="[
          { value: 'quick', label: '本机微信' },
          { value: 'qr', label: '手机扫码' },
        ]"
        @change="
          (value: string | number) => switchMode(value as 'quick' | 'qr')
        "
      />
      <template v-if="mode === 'qr'"
        ><div class="scan-qr">
          <a-skeleton v-if="loading" active /><Check
            v-else-if="account"
            :size="80"
            color="#218350"
          /><img
            v-else-if="qr && !imageFailed"
            :src="qr.image_base64 || qr.image_url"
            alt="微信登录二维码"
            @error="
              imageFailed = true;
              setStatus('二维码图片加载失败，请检查 /qr/* 路径', 'error');
            "
          /><span v-else>二维码未生成</span>
        </div>
        <a-alert v-if="status" :type="statusType" show-icon :message="status" />
        <div class="actions" style="margin-top: 12px">
          <span v-if="remaining > 0" class="muted"
            >{{ remaining }} 秒后刷新</span
          ><a-button :loading="loading" @click="newQR"
            ><RefreshCw :size="16" />重新生成</a-button
          >
        </div>
      </template>
      <template v-else
        ><a-alert
          type="info"
          show-icon
          :message="quickState || '正在检测本机微信'"
        />
        <div class="actions" style="margin-top: 16px">
          <a-button
            type="primary"
            :disabled="!quickConfig"
            :loading="quickBusy"
            @click="authorizeQuick"
            >使用本机微信授权</a-button
          ><a-button :disabled="quickBusy" @click="detectQuick"
            >重新检测</a-button
          >
        </div></template
      >
    </section>
    <section class="section">
      <h2>账号信息</h2>
      <a-empty v-if="!account" description="授权成功后显示账号信息" /><template
        v-else
      >
        <p>
          <strong>{{ accountName(account) }}</strong> · ID {{ account.id }} ·
          {{ account.status }}
        </p>
        <p>
          <code>{{ account.openid }}</code>
        </p>
        <a-form layout="vertical" @submit="saveRemark"
          ><a-form-item label="账号备注"
            ><a-input
              v-model:value="remark"
              :maxlength="80"
              placeholder="例如：Boom、微信账号 2"
          /></a-form-item>
          <div class="actions">
            <a-button html-type="submit" :loading="actionBusy"
              >保存备注</a-button
            ><a-button type="primary" :loading="actionBusy" @click="sync"
              >添加到面板</a-button
            ><a-button @click="copy"><Copy :size="15" />复制 OpenID</a-button>
          </div></a-form
        >
        <a-alert
          v-if="actionNote"
          style="margin-top: 14px"
          type="info"
          :message="actionNote"
          show-icon
        />
      </template>
    </section>
  </main>
</template>
