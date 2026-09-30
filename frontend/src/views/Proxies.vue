<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { message } from "ant-design-vue";
import { RefreshCw, ArrowLeft, Trash2 } from "@lucide/vue";
import { api, accountName, errorMessage, type Account } from "../api";
import { useSession } from "../session";

interface Profile {
  id: number;
  name: string;
  provider: string;
  proxy_type: string;
  api_url: string;
}
interface Area {
  code: string;
  name: string;
}
interface Config {
  ref?: string;
  mode: string;
  proxy_type: string;
  static_proxy: string;
  api_url: string;
  provider_profile_id: number | null;
  region_code: string;
  region_province: string;
  region_city: string;
  refresh_ahead_minutes: number;
  configured?: boolean;
  token_ttl_minutes?: number;
  proxy_warning?: string;
}
const route = useRoute(),
  router = useRouter(),
  session = useSession();
const accounts = ref<Account[]>([]),
  profiles = ref<Profile[]>([]),
  provinces = ref<Area[]>([]),
  cities = ref<Area[]>([]);
const selected = ref(""),
  search = ref(""),
  profileID = ref("new");
const loading = ref(true),
  editorLoading = ref(false),
  busy = ref(false),
  profileBusy = ref(false);
const error = ref(""),
  feedback = ref(""),
  profileError = ref("");
const config = reactive<Config>({
  mode: "direct",
  proxy_type: "http",
  static_proxy: "",
  api_url: "",
  provider_profile_id: null,
  region_code: "",
  region_province: "",
  region_city: "",
  refresh_ahead_minutes: 5,
});
const mode = ref("direct"),
  province = ref("all"),
  city = ref("");
const profile = reactive({
  name: "",
  provider: "ipzan",
  proxy_type: "http",
  authorization_mode: "auth",
  api_url: "",
  trade_no: "",
  api_key: "",
});
const current = computed(() =>
  accounts.value.find((item) => String(item.id) === selected.value),
);
const visible = computed(() =>
  accounts.value.filter((item) =>
    `${item.id} ${accountName(item)} ${item.openid}`
      .toLowerCase()
      .includes(search.value.toLowerCase()),
  ),
);
const chosenProfile = computed(() =>
  profiles.value.find((item) => String(item.id) === profileID.value),
);
const back = computed(() => {
  const value = route.query.return;
  if (
    typeof value === "string" &&
    value.startsWith("/") &&
    !value.startsWith("//") &&
    !value.includes("\\")
  )
    return value.startsWith("/console/")
      ? value.slice("/console".length)
      : value;
  return `/accounts?ref=${selected.value}`;
});
const profileOptions = computed(() =>
  profiles.value.map((item) => ({ value: item.id, label: item.name })),
);
const areaOptions = (items: Area[]) =>
  items.map((item) => ({ value: item.code, label: item.name }));
let generation = 0;

function editProfile(id: string) {
  profileID.value = id;
  const item = profiles.value.find((value) => String(value.id) === id);
  profile.name = item?.name || "";
  profile.provider = item?.provider || "ipzan";
  profile.proxy_type = item?.proxy_type || "http";
  let url: URL | null = null;
  try {
    url = item ? new URL(item.api_url) : null;
  } catch {
    /* static proxy address */
  }
  profile.authorization_mode =
    url?.searchParams.get("mode") === "whitelist" ? "whitelist" : "auth";
  profile.api_url = item?.provider === "juliang" ? "" : item?.api_url || "";
  profile.trade_no = url?.searchParams.get("trade_no") || "";
  profile.api_key = url?.searchParams.get("key") || "";
}
async function loadCities(code: string, requested = "") {
  cities.value =
    code === "all"
      ? []
      : await api<Area[]>(
          `/api/proxy-profiles/areas/cities?province=${encodeURIComponent(code)}`,
        );
  city.value = cities.value.some((item) => item.code === requested)
    ? requested
    : "";
}
async function selectAccount(id: string) {
  if (!accounts.value.some((item) => String(item.id) === id)) return;
  const request = ++generation;
  selected.value = id;
  editorLoading.value = true;
  error.value = "";
  feedback.value = "";
  router.replace({ query: { ...route.query, ref: id } });
  try {
    const result = await api<Config>(
      `/accounts/proxy?ref=${encodeURIComponent(id)}`,
    );
    if (request !== generation) return;
    Object.assign(config, result);
    mode.value = result.provider_profile_id ? "profile" : result.mode;
    province.value =
      result.region_code && result.region_code !== "all"
        ? `${result.region_code.slice(0, 2)}0000`
        : "all";
    await loadCities(province.value, result.region_code);
    if (request !== generation) return;
    feedback.value = result.configured
      ? `当前代理已配置${result.proxy_warning ? `；${result.proxy_warning}` : ""}`
      : "当前账号使用直连";
  } catch (cause) {
    if (request === generation) error.value = errorMessage(cause);
  } finally {
    if (request === generation) editorLoading.value = false;
  }
}
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const [accountList, profileList, provinceList] = await Promise.all([
      api<Account[]>("/accounts"),
      api<Profile[]>("/api/proxy-profiles"),
      api<Area[]>("/api/proxy-profiles/areas/provinces"),
    ]);
    accounts.value = accountList;
    profiles.value = profileList;
    provinces.value = provinceList;
    editProfile(profileID.value);
    const requested = String(route.query.ref || selected.value || "");
    await selectAccount(
      accounts.value.some((item) => String(item.id) === requested)
        ? requested
        : String(accounts.value[0]?.id || ""),
    );
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
}
function payload(): Config {
  const configuredProfile = mode.value === "profile";
  const area = cities.value.find((item) => item.code === city.value);
  return {
    ref: selected.value,
    mode: configuredProfile ? "api" : mode.value,
    proxy_type: config.proxy_type,
    static_proxy: config.static_proxy.trim(),
    api_url: config.api_url.trim(),
    provider_profile_id: configuredProfile ? config.provider_profile_id : null,
    region_code: configuredProfile ? city.value || province.value : "",
    region_province: configuredProfile
      ? provinces.value.find((item) => item.code === province.value)?.name || ""
      : "",
    region_city: configuredProfile ? area?.name || "" : "",
    refresh_ahead_minutes: config.refresh_ahead_minutes,
  };
}
async function test() {
  if (busy.value || editorLoading.value) return;
  busy.value = true;
  error.value = "";
  try {
    const result = await api<{
      resolved: boolean;
      proxy?: string;
      exit_region?: string;
      exit_city?: string;
      exit_ip?: string;
      probe_error?: string;
    }>("/accounts/proxy/test", "POST", payload());
    feedback.value = result.resolved
      ? `代理 ${result.proxy}；出口 ${[result.exit_region, result.exit_city, result.exit_ip].filter(Boolean).join(" · ") || result.probe_error || "未能检测"}`
      : "直连模式无需代理";
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}
async function save() {
  if (busy.value || editorLoading.value || !selected.value) return;
  if (
    config.configured &&
    mode.value === "direct" &&
    !window.confirm(`账号 ${selected.value} 当前使用代理，确定切换为直连吗？`)
  )
    return;
  const id = selected.value;
  busy.value = true;
  error.value = "";
  try {
    await api("/accounts/proxy", "PUT", payload());
    if (selected.value === id) {
      await selectAccount(id);
      message.success("账号代理已保存");
    }
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}
async function saveProfile() {
  if (!session.admin || profileBusy.value) return;
  profileBusy.value = true;
  profileError.value = "";
  const existing = chosenProfile.value;
  try {
    const saved = await api<Profile>(
      existing ? `/api/proxy-profiles/${existing.id}` : "/api/proxy-profiles",
      existing ? "PUT" : "POST",
      {
        ...profile,
        api_url: profile.provider === "juliang" ? "" : profile.api_url.trim(),
        trade_no: profile.provider === "juliang" ? profile.trade_no.trim() : "",
        api_key: profile.provider === "juliang" ? profile.api_key.trim() : "",
      },
    );
    profiles.value = await api<Profile[]>("/api/proxy-profiles");
    editProfile(String(saved.id));
    message.success("代理配置已保存");
  } catch (cause) {
    profileError.value = errorMessage(cause);
  } finally {
    profileBusy.value = false;
  }
}
async function deleteProfile() {
  if (!chosenProfile.value || profileBusy.value) return;
  profileBusy.value = true;
  profileError.value = "";
  try {
    await api(`/api/proxy-profiles/${chosenProfile.value.id}`, "DELETE");
    profiles.value = await api<Profile[]>("/api/proxy-profiles");
    editProfile("new");
    message.success("配置已删除");
  } catch (cause) {
    profileError.value = errorMessage(cause);
  } finally {
    profileBusy.value = false;
  }
}
function importIPZan() {
  editProfile("new");
  profile.name = `${current.value ? accountName(current.value) : "账号"}的品赞代理`;
  profile.provider = "ipzan";
  profile.proxy_type = config.proxy_type;
  profile.api_url = config.api_url;
  document.getElementById("profile-editor")?.scrollIntoView({ block: "start" });
}
watch(
  () => route.query.ref,
  (value) => {
    if (
      value &&
      String(value) !== selected.value &&
      accounts.value.some((item) => String(item.id) === String(value))
    )
      selectAccount(String(value));
  },
);
onMounted(load);
</script>
<template>
  <main class="page">
    <div class="page-head">
      <h1>代理设置</h1>
      <div class="actions">
        <a-button @click="router.push(back)"
          ><ArrowLeft :size="16" />返回账号</a-button
        ><a-button :loading="loading" @click="load"
          ><RefreshCw :size="16" />重新加载</a-button
        >
      </div>
    </div>
    <a-alert
      v-if="error"
      class="toolbar"
      type="error"
      show-icon
      :message="error"
    />
    <section id="profile-editor" class="section">
      <div class="page-head">
        <h2>代理配置库</h2>
        <a-select
          :value="profileID"
          aria-label="选择代理配置"
          style="width: 220px; max-width: 100%"
          :options="[
            { value: 'new', label: '新增配置' },
            ...profileOptions.map((item) => ({
              ...item,
              value: String(item.value),
            })),
          ]"
          @change="(value: string | number) => editProfile(String(value))"
        />
      </div>
      <a-alert
        v-if="profileError"
        type="error"
        show-icon
        :message="profileError"
        class="toolbar"
      />
      <a-form layout="vertical" class="field-grid" @submit="saveProfile">
        <a-form-item label="配置名称"
          ><a-input
            v-model:value="profile.name"
            :maxlength="50"
            required
            :disabled="!session.admin"
        /></a-form-item>
        <a-form-item label="配置类型"
          ><a-select
            v-model:value="profile.provider"
            :disabled="!session.admin"
            :options="[
              { value: 'ipzan', label: '品赞' },
              { value: 'juliang', label: '巨量' },
              { value: 'static', label: '静态代理预设' },
            ]"
        /></a-form-item>
        <a-form-item label="代理协议"
          ><a-select
            v-model:value="profile.proxy_type"
            :disabled="!session.admin"
            :options="[
              { value: 'http', label: 'HTTP CONNECT' },
              { value: 'socks5', label: 'SOCKS5' },
            ]"
        /></a-form-item>
        <a-form-item v-if="profile.provider !== 'static'" label="授权方式"
          ><a-select
            v-model:value="profile.authorization_mode"
            :disabled="!session.admin"
            :options="[
              { value: 'auth', label: '账号密码' },
              { value: 'whitelist', label: '出口 IP 白名单' },
            ]"
        /></a-form-item>
        <a-form-item
          v-if="profile.provider !== 'juliang'"
          class="wide"
          :label="profile.provider === 'static' ? '代理地址' : '品赞提取链接'"
          ><a-input
            v-model:value="profile.api_url"
            required
            autocomplete="off"
            :disabled="!session.admin"
        /></a-form-item>
        <template v-else
          ><a-form-item label="巨量业务编号"
            ><a-input
              v-model:value="profile.trade_no"
              required
              :disabled="!session.admin" /></a-form-item
          ><a-form-item label="巨量 API Key"
            ><a-input-password
              v-model:value="profile.api_key"
              required
              :disabled="!session.admin" /></a-form-item
        ></template>
        <div v-if="session.admin" class="actions wide">
          <a-button type="primary" html-type="submit" :loading="profileBusy">{{
            chosenProfile ? "保存配置" : "添加配置"
          }}</a-button
          ><a-popconfirm
            v-if="chosenProfile"
            title="确定删除此代理配置？使用中的配置不能删除。"
            @confirm="deleteProfile"
            ><a-button danger :disabled="profileBusy"
              ><Trash2 :size="15" />删除配置</a-button
            ></a-popconfirm
          >
        </div>
      </a-form>
    </section>
    <a-skeleton v-if="loading" active />
    <div v-else class="proxy-workspace-native">
      <section class="proxy-account-panel" aria-label="微信账号列表">
        <h2>微信账号 · {{ accounts.length }}</h2>
        <a-input-search
          v-model:value="search"
          placeholder="搜索备注、昵称或 ID"
          aria-label="搜索微信账号"
        />
        <div class="proxy-account-scroll">
          <button
            v-for="account in visible"
            :key="account.id"
            class="proxy-account-choice"
            :class="{ selected: selected === String(account.id) }"
            :aria-pressed="selected === String(account.id)"
            @click="selectAccount(String(account.id))"
          >
            <strong>{{ accountName(account) }}</strong
            ><span
              >ID {{ account.id }} ·
              {{ account.status === "alive" ? "可用" : "需确认" }}</span
            ></button
          ><a-empty v-if="!visible.length" description="没有符合条件的账号" />
        </div>
      </section>
      <section class="proxy-config-panel" aria-label="账号代理编辑器">
        <a-empty v-if="!current" description="请先添加微信账号" />
        <template v-else
          ><div class="page-head">
            <h2>{{ accountName(current) }} · ID {{ current.id }}</h2>
          </div>
          <a-skeleton v-if="editorLoading" active />
          <a-form v-else layout="vertical" class="field-grid" @submit="save">
            <div class="wide">
              <label>连接模式</label
              ><a-segmented
                v-model:value="mode"
                :options="[
                  { value: 'direct', label: '直连' },
                  { value: 'static', label: '静态代理' },
                  { value: 'api', label: '自定义 API' },
                  { value: 'profile', label: '配置库' },
                ]"
              />
            </div>
            <a-form-item
              v-if="mode === 'static' || mode === 'api'"
              label="代理协议"
              ><a-select
                v-model:value="config.proxy_type"
                :options="[
                  { value: 'http', label: 'HTTP CONNECT' },
                  { value: 'socks5', label: 'SOCKS5' },
                ]"
            /></a-form-item>
            <a-form-item v-if="mode === 'static'" class="wide" label="代理地址"
              ><a-input
                v-model:value="config.static_proxy"
                required
                autocomplete="off"
                placeholder="user:pass@127.0.0.1:8080"
            /></a-form-item>
            <a-form-item v-if="mode === 'api'" class="wide" label="代理提取 API"
              ><a-input
                v-model:value="config.api_url"
                required
                autocomplete="off"
              /><a-button
                v-if="
                  config.api_url.includes('service.ipzan.com') && session.admin
                "
                style="margin-top: 8px"
                @click="importIPZan"
                >导入为品赞配置</a-button
              ></a-form-item
            >
            <template v-if="mode === 'profile'"
              ><a-form-item label="供应商配置"
                ><a-select
                  v-model:value="config.provider_profile_id"
                  :options="profileOptions"
                  placeholder="选择配置" /></a-form-item
              ><a-form-item label="省份"
                ><a-select
                  v-model:value="province"
                  :options="areaOptions(provinces)"
                  @change="
                    (value: string | number) =>
                      loadCities(String(value)).catch(
                        (cause) => (error = errorMessage(cause)),
                      )
                  " /></a-form-item
              ><a-form-item label="城市"
                ><a-select
                  v-model:value="city"
                  :options="[
                    { value: '', label: '全省' },
                    ...areaOptions(cities),
                  ]" /></a-form-item
            ></template>
            <a-form-item
              v-if="mode === 'api' || mode === 'profile'"
              class="wide"
              label="提前刷新"
              ><a-slider
                v-model:value="config.refresh_ahead_minutes"
                :min="5"
                :max="90"
                :step="5"
              /><span class="muted"
                >提前 {{ config.refresh_ahead_minutes }} 分钟 · 预计约
                {{
                  Math.max(
                    1,
                    (config.token_ttl_minutes || 120) -
                      config.refresh_ahead_minutes,
                  )
                }}
                分钟刷新一次</span
              ></a-form-item
            >
            <a-alert
              v-if="feedback"
              class="wide"
              type="info"
              show-icon
              :message="feedback"
            />
            <div class="actions wide">
              <a-button :loading="busy" @click="test">测试代理</a-button
              ><a-button type="primary" html-type="submit" :loading="busy"
                >保存代理</a-button
              >
            </div>
          </a-form>
        </template>
      </section>
    </div>
  </main>
</template>
