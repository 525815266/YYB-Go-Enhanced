<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { message } from "ant-design-vue";
import { api, accountName, errorMessage, type Account } from "../api";
import { useSession } from "../session";
const session = useSession();
const features = [
  { value: "code", label: "获取小程序 code", appid: true },
  { value: "getuserinfo", label: "用户信息", appid: false },
  { value: "encryptkey", label: "加密能力转发", appid: true, payload: true },
  {
    value: "getlatestuserkey",
    label: "最新加密密钥",
    appid: true,
    payload: true,
  },
  { value: "getphonenumber", label: "手机号授权", appid: true },
  { value: "cloud", label: "云函数", appid: true, payload: true },
  { value: "qrcodeauth", label: "二维码授权", appid: false },
  { value: "mpgeta8key", label: "文章会话", appid: true, payload: true },
  { value: "appmsgext", label: "文章阅读数", appid: true, payload: true },
  { value: "appmsglike", label: "文章点赞", appid: true, payload: true },
  { value: "oauth", label: "公众号授权", appid: true },
];
const accounts = ref<Account[]>([]),
  selected = ref(""),
  capability = ref("code"),
  appid = ref("");
const redirect = ref(""),
  scope = ref("snsapi_base"),
  state = ref("");
const payload = ref('{"api_name":"getUserInfo","data":{},"env":1}'),
  result = ref("");
const feature = computed(
  () => features.find((item) => item.value === capability.value)!,
);
const account = computed(() =>
  accounts.value.find((item) => String(item.id) === selected.value),
);
const error = ref(""),
  busy = ref(false),
  loading = ref(true);
const panel = reactive({
  type: "qinglong",
  url: "",
  client_id: "",
  client_secret: "",
});
const panelConfigured = ref(false),
  saving = ref(false);
async function load() {
  loading.value = true;
  error.value = "";
  try {
    accounts.value = await api<Account[]>("/accounts");
    selected.value = String(accounts.value[0]?.id || "");
    if (session.admin) {
      const config = await api<typeof panel & { configured: boolean }>(
        "/api/qinglong/config",
      );
      Object.assign(panel, {
        type: config.type || "qinglong",
        url: config.url || "",
        client_id: config.client_id || "",
        client_secret: "",
      });
      panelConfigured.value = config.configured;
    }
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
}
async function call() {
  if (busy.value) return;
  error.value = "";
  if (capability.value !== "qrcodeauth" && !account.value) {
    error.value = "请选择账号";
    return;
  }
  if (feature.value.appid && !appid.value.trim()) {
    error.value = "请输入 AppID";
    return;
  }
  let body: Record<string, unknown> | undefined =
    capability.value === "qrcodeauth"
      ? undefined
      : { ref: account.value!.openid };
  if (body && feature.value.appid) body.app_id = appid.value.trim();
  if (capability.value === "oauth") {
    if (!redirect.value.trim()) {
      error.value = "请输入公众号回调地址";
      return;
    }
    body = {
      ref: String(account.value!.id),
      appid: appid.value.trim(),
      redirect_uri: redirect.value.trim(),
      scope: scope.value,
      state: state.value,
    };
  }
  if (feature.value.payload) {
    try {
      body!.payload = JSON.parse(payload.value);
    } catch {
      error.value = "请求 JSON 格式不正确";
      return;
    }
  }
  busy.value = true;
  try {
    result.value = JSON.stringify(
      await api(`/wx/${capability.value}`, "POST", body),
      null,
      2,
    );
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}
async function savePanel(clear = false) {
  if (saving.value) return;
  saving.value = true;
  error.value = "";
  try {
    await api(
      "/api/qinglong/config",
      "PUT",
      clear ? { type: panel.type, clear: true } : panel,
    );
    panel.client_secret = "";
    message.success(clear ? "连接配置已清除" : "连接测试成功，配置已保存");
    await load();
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    saving.value = false;
  }
}
onMounted(load);
</script>
<template>
  <main class="page">
    <div class="page-head"><h1>调用配置</h1></div>
    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      class="toolbar"
    />
    <a-skeleton v-if="loading" active /><template v-else>
      <section class="section">
        <a-form layout="vertical" @submit="call" class="field-grid">
          <a-form-item label="微信账号" html-for="call-account"
            ><a-select
              id="call-account"
              v-model:value="selected"
              :disabled="busy"
              :options="
                accounts.map((item) => ({
                  value: String(item.id),
                  label: `ID ${item.id} · ${accountName(item)}`,
                }))
              "
          /></a-form-item>
          <a-form-item label="能力" html-for="call-feature"
            ><a-select
              id="call-feature"
              v-model:value="capability"
              :disabled="busy"
              :options="features"
          /></a-form-item>
          <a-form-item v-if="feature.appid" label="AppID" html-for="call-appid"
            ><a-input
              id="call-appid"
              v-model:value="appid"
              required
              autocomplete="off"
              :disabled="busy"
          /></a-form-item>
          <template v-if="capability === 'oauth'">
            <a-form-item label="公众号回调地址" html-for="call-redirect"
              ><a-input
                id="call-redirect"
                v-model:value="redirect"
                type="url"
                required
                :disabled="busy"
            /></a-form-item>
            <a-form-item label="授权范围" html-for="call-scope"
              ><a-select
                id="call-scope"
                v-model:value="scope"
                :options="[
                  { value: 'snsapi_base', label: 'snsapi_base' },
                  { value: 'snsapi_userinfo', label: 'snsapi_userinfo' },
                ]"
                :disabled="busy"
            /></a-form-item>
            <a-form-item label="State" html-for="call-state"
              ><a-input
                id="call-state"
                v-model:value="state"
                :maxlength="128"
                :disabled="busy"
            /></a-form-item>
          </template>
          <a-form-item
            v-if="feature.payload"
            label="请求 JSON"
            html-for="call-payload"
            class="wide"
            ><a-textarea
              id="call-payload"
              v-model:value="payload"
              :rows="5"
              required
              :disabled="busy"
          /></a-form-item>
          <div class="actions wide">
            <a-button type="primary" html-type="submit" :loading="busy"
              >执行调用</a-button
            ><a-button @click="result = ''">清空结果</a-button>
          </div>
        </a-form>
      </section>
      <section class="section">
        <h2>返回结果</h2>
        <pre class="result-text">{{ result || "暂无结果" }}</pre>
      </section>
      <section v-if="session.admin" class="section">
        <h2>
          面板连接
          <span class="muted">{{ panelConfigured ? "已配置" : "未配置" }}</span>
        </h2>
        <a-form layout="vertical" @submit="savePanel()" class="field-grid">
          <a-form-item label="面板类型" html-for="panel-type"
            ><a-select
              id="panel-type"
              v-model:value="panel.type"
              :disabled="saving"
              :options="[
                { value: 'qinglong', label: '青龙' },
                { value: 'daidai', label: '呆呆' },
                { value: 'arcadia', label: 'Arcadia' },
              ]"
          /></a-form-item>
          <a-form-item label="面板地址" html-for="panel-url"
            ><a-input
              id="panel-url"
              v-model:value="panel.url"
              required
              type="url"
              :disabled="saving"
          /></a-form-item>
          <a-form-item
            v-if="panel.type !== 'arcadia'"
            label="Client ID / App Key"
            html-for="panel-client"
            ><a-input
              id="panel-client"
              v-model:value="panel.client_id"
              :disabled="saving"
          /></a-form-item>
          <a-form-item label="Secret / Token" html-for="panel-secret"
            ><a-input-password
              id="panel-secret"
              v-model:value="panel.client_secret"
              autocomplete="off"
              :disabled="saving"
          /></a-form-item>
          <div class="actions wide">
            <a-button type="primary" html-type="submit" :loading="saving"
              >测试并保存</a-button
            ><a-popconfirm
              title="确认清除面板连接配置？"
              @confirm="savePanel(true)"
              ><a-button :disabled="saving">清除配置</a-button></a-popconfirm
            >
          </div>
        </a-form>
      </section>
    </template>
  </main>
</template>
