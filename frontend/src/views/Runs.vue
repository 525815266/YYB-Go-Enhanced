<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";
import { message } from "ant-design-vue";
import { RefreshCw, Play } from "@lucide/vue";
import { api, accountName, errorMessage, type Account } from "../api";
interface Job {
  script_key: string;
  name: string;
  schedule: string;
  enabled: boolean;
  running: boolean;
  provisioned: boolean;
  global_task_active?: boolean;
}
interface Run {
  log_key: string;
  script_key: string;
  name: string;
  started_at: number;
  status: string;
  running: boolean;
  size: number;
}
interface Push {
  channel: string;
  token_configured?: boolean;
  topic_configured?: boolean;
}
const route = useRoute(),
  router = useRouter();
const accounts = ref<Account[]>([]),
  selected = ref(""),
  jobs = ref<Job[]>([]),
  runs = ref<Run[]>([]);
const mode = ref(route.query.view === "logs" ? "logs" : "tasks"),
  filter = ref("configured"),
  query = ref("");
const loading = ref(false),
  error = ref(""),
  actionBusy = ref(false),
  loadedRef = ref("");
const push = reactive({
    channel: "none",
    token: "",
    topic: "",
    token_configured: false,
  }),
  pushOpen = ref(route.query.view === "push");
const logKey = ref(""),
  logName = ref(""),
  logText = ref(""),
  logError = ref(""),
  logBusy = ref(false),
  logTime = ref("");
const logElement = ref<HTMLElement>(),
  logAccount = ref("");
const account = computed(() =>
  accounts.value.find((item) => String(item.id) === selected.value),
);
const filteredJobs = computed(() =>
  jobs.value.filter(
    (job) =>
      (filter.value === "all" ||
        (filter.value === "enabled" && job.enabled) ||
        (filter.value === "configured" && job.provisioned)) &&
      `${job.name} ${job.script_key}`
        .toLowerCase()
        .includes(query.value.toLowerCase()),
  ),
);
const duplicateCount = computed(
  () => jobs.value.filter((job) => job.global_task_active).length,
);
let disposed = false,
  refreshing = false,
  generation = 0,
  timer: number;
async function loadSelected() {
  const refID = selected.value,
    request = ++generation;
  jobs.value = [];
  runs.value = [];
  logKey.value = "";
  loadedRef.value = "";
  Object.assign(push, {
    channel: "none",
    token: "",
    topic: "",
    token_configured: false,
  });
  error.value = "";
  loading.value = true;
  if (!refID) {
    loading.value = false;
    return;
  }
  try {
    const [jobList, history, notification] = await Promise.all([
      api<{ jobs: Job[] }>(
        `/api/qinglong/jobs?ref=${encodeURIComponent(refID)}`,
      ),
      api<{ runs: Run[] }>(
        `/api/qinglong/runs?ref=${encodeURIComponent(refID)}`,
      ),
      api<Push>(`/api/qinglong/push?ref=${encodeURIComponent(refID)}`),
    ]);
    if (disposed || request !== generation) return;
    jobs.value = jobList.jobs;
    runs.value = history.runs;
    Object.assign(push, notification);
    loadedRef.value = refID;
  } catch (cause) {
    if (request === generation) error.value = errorMessage(cause);
  } finally {
    if (request === generation) loading.value = false;
  }
}
async function mutate(path: string, body: unknown) {
  if (actionBusy.value || loadedRef.value !== selected.value) return;
  const refID = selected.value;
  actionBusy.value = true;
  error.value = "";
  try {
    const result = await api<Push>(
      `/api/qinglong/${path}`,
      path === "jobs/run" ? "POST" : "PUT",
      { ref: refID, ...(body as object) },
    );
    if (selected.value !== refID || disposed) return;
    if (path === "push") {
      Object.assign(push, result, { token: "", topic: "" });
      message.success("推送设置已保存");
    } else {
      message.success(
        path === "jobs/run"
          ? `已提交账号 ${refID}，等待面板生成日志`
          : "定时任务已更新",
      );
      if (path === "jobs/run") mode.value = "logs";
      await refresh();
    }
  } catch (cause) {
    if (selected.value === refID) error.value = errorMessage(cause);
  } finally {
    actionBusy.value = false;
  }
}
async function readLog() {
  const key = logKey.value,
    refID = selected.value;
  if (!key || logBusy.value || disposed) return;
  logBusy.value = true;
  const element = logElement.value,
    top = element?.scrollTop || 0;
  const bottom = element
    ? element.scrollHeight - element.clientHeight - top < 24
    : true;
  try {
    const result = await api<{ log: string }>(
      "/api/qinglong/runs/log",
      "POST",
      { ref: refID, log_key: key },
    );
    if (disposed || selected.value !== refID || logKey.value !== key) return;
    logText.value = result.log || "日志为空";
    logError.value = "";
    logTime.value = new Date().toLocaleTimeString("zh-CN");
    await nextTick();
    if (logElement.value)
      logElement.value.scrollTop = bottom ? logElement.value.scrollHeight : top;
  } catch (cause) {
    if (selected.value === refID && logKey.value === key)
      logError.value = errorMessage(cause);
  } finally {
    logBusy.value = false;
  }
}
async function openLog(run: Run) {
  logKey.value = run.log_key;
  logName.value = run.name;
  logAccount.value = account.value
    ? accountName(account.value)
    : selected.value;
  logText.value = "";
  logError.value = "";
  await nextTick();
  await readLog();
}
async function refresh() {
  if (
    refreshing ||
    !selected.value ||
    loadedRef.value !== selected.value ||
    disposed
  )
    return;
  const refID = selected.value,
    request = generation;
  refreshing = true;
  try {
    const [jobList, history] = await Promise.all([
      api<{ jobs: Job[] }>(`/api/qinglong/jobs?ref=${refID}`),
      api<{ runs: Run[] }>(`/api/qinglong/runs?ref=${refID}`),
    ]);
    if (disposed || request !== generation) return;
    jobs.value = jobList.jobs;
    runs.value = history.runs;
    error.value = "";
    await readLog();
  } catch (cause) {
    if (request === generation) error.value = errorMessage(cause);
  } finally {
    refreshing = false;
  }
}
watch(selected, () => {
  router.replace({ query: { ...route.query, ref: selected.value } });
  loadSelected();
});
watch(
  () => route.query.ref,
  (value) => {
    if (accounts.value.some((item) => String(item.id) === String(value)))
      selected.value = String(value);
  },
);
function visibility() {
  if (!document.hidden) refresh();
}
onMounted(async () => {
  loading.value = true;
  try {
    accounts.value = await api<Account[]>("/accounts");
    if (disposed) return;
    selected.value = accounts.value.some(
      (item) => String(item.id) === String(route.query.ref),
    )
      ? String(route.query.ref)
      : String(accounts.value[0]?.id || "");
    if (!selected.value) loading.value = false;
  } catch (cause) {
    error.value = errorMessage(cause);
    loading.value = false;
  }
  if (disposed) return;
  timer = window.setInterval(() => {
    if (!document.hidden && !loading.value && !actionBusy.value) refresh();
  }, 3000);
  document.addEventListener("visibilitychange", visibility);
});
onBeforeUnmount(() => {
  disposed = true;
  ++generation;
  clearInterval(timer);
  document.removeEventListener("visibilitychange", visibility);
});
</script>
<template>
  <main class="page">
    <div class="page-head">
      <h1>运行管理</h1>
      <a-button @click="pushOpen = !pushOpen">{{
        pushOpen ? "收起推送" : "配置推送"
      }}</a-button>
    </div>
    <div class="toolbar">
      <a-select
        v-model:value="selected"
        aria-label="选择运行账号"
        :disabled="actionBusy"
        style="width: 280px; max-width: 100%"
        :options="
          accounts.map((item) => ({
            value: String(item.id),
            label: `ID ${item.id} · ${accountName(item)}`,
          }))
        "
      /><a-button
        aria-label="刷新运行管理"
        title="刷新"
        :loading="loading"
        @click="loadedRef === selected ? refresh() : loadSelected()"
        ><RefreshCw :size="16"
      /></a-button>
    </div>
    <a-alert
      v-if="error"
      show-icon
      type="error"
      :message="error"
      class="toolbar"
    />
    <a-alert
      v-if="duplicateCount"
      type="warning"
      :message="`有 ${duplicateCount} 个旧全局任务仍启用，可能重复运行`"
      class="toolbar"
    />
    <section class="section" v-if="pushOpen">
      <h2>{{ account ? accountName(account) : "未选择账号" }} 的推送设置</h2>
      <a-form
        layout="vertical"
        class="field-grid"
        @submit="
          mutate('push', {
            channel: push.channel,
            token: push.token,
            topic: push.channel === 'pushplus' ? push.topic : null,
          })
        "
      >
        <a-form-item label="推送方式" html-for="push-channel"
          ><a-select
            v-model:value="push.channel"
            id="push-channel"
            :options="[
              { value: 'none', label: '关闭推送' },
              { value: 'serverchan', label: 'Server酱' },
              { value: 'pushplus', label: 'PushPlus' },
              { value: 'qywx', label: '企业微信机器人' },
            ]"
        /></a-form-item>
        <a-form-item
          v-if="push.channel !== 'none'"
          html-for="push-token"
          :label="push.token_configured ? 'Token（留空保留）' : 'Token'"
          ><a-input-password
            id="push-token"
            v-model:value="push.token"
            autocomplete="off"
        /></a-form-item>
        <a-form-item
          v-if="push.channel === 'pushplus'"
          label="群组 Topic"
          html-for="push-topic"
          ><a-input id="push-topic" v-model:value="push.topic"
        /></a-form-item>
        <div class="wide">
          <a-button
            html-type="submit"
            type="primary"
            :loading="actionBusy"
            :disabled="!selected || loadedRef !== selected"
            >保存推送设置</a-button
          >
        </div>
      </a-form>
    </section>
    <a-skeleton v-if="loading" active /><a-empty
      v-else-if="!accounts.length"
      description="尚未添加微信账号"
    />
    <template v-else
      ><a-tabs v-model:active-key="mode"
        ><a-tab-pane key="tasks" tab="账号任务" /><a-tab-pane
          key="logs"
          :tab="`运行日志 (${runs.length})`"
      /></a-tabs>
      <template v-if="mode === 'tasks'"
        ><div class="toolbar">
          <a-segmented
            v-model:value="filter"
            :options="[
              { value: 'configured', label: '已配置' },
              { value: 'enabled', label: '已启用' },
              { value: 'all', label: '全部脚本' },
            ]"
          /><a-input-search
            v-model:value="query"
            placeholder="搜索脚本"
            aria-label="搜索脚本"
          />
        </div>
        <div class="table-scroll" v-if="filteredJobs.length">
          <table class="data-table">
            <thead>
              <tr>
                <th>脚本</th>
                <th>Cron</th>
                <th>状态</th>
                <th>定时开关</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="job in filteredJobs" :key="job.script_key">
                <td>
                  <strong>{{ job.name }}</strong>
                  <div class="muted">{{ job.script_key }}</div>
                </td>
                <td>
                  <code>{{ job.schedule }}</code>
                </td>
                <td>
                  <a-tag
                    :color="
                      job.running ? 'blue' : job.enabled ? 'green' : 'default'
                    "
                    >{{
                      job.running
                        ? "运行中"
                        : job.enabled
                          ? "定时已启用"
                          : job.provisioned
                            ? "定时已停用"
                            : "尚未添加"
                    }}</a-tag
                  >
                </td>
                <td>
                  <a-switch
                    :checked="job.enabled"
                    :disabled="actionBusy || loadedRef !== selected"
                    :aria-label="`启用${job.name}`"
                    @change="
                      (enabled: boolean) =>
                        mutate('jobs/enable', {
                          script_key: job.script_key,
                          enabled,
                        })
                    "
                  />
                </td>
                <td>
                  <a-button
                    :disabled="actionBusy || loadedRef !== selected"
                    @click="mutate('jobs/run', { script_key: job.script_key })"
                    ><Play :size="14" />运行账号 {{ selected }}</a-button
                  >
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <a-empty v-else description="没有符合条件的账号任务" />
      </template>
      <template v-else
        ><div class="table-scroll" v-if="runs.length">
          <table class="data-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>脚本</th>
                <th>状态</th>
                <th>日志</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="run in runs" :key="run.log_key">
                <td>
                  {{ new Date(run.started_at * 1000).toLocaleString("zh-CN") }}
                </td>
                <td>
                  {{ run.name }}
                  <div class="muted">{{ run.script_key }}</div>
                </td>
                <td>{{ run.status }}</td>
                <td>
                  <a-button size="small" @click="openLog(run)"
                    >查看日志</a-button
                  >
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <a-empty v-else description="暂无运行日志"
      /></template>
    </template>
    <a-drawer
      :open="Boolean(logKey)"
      :title="`${logAccount} · ${logName}`"
      placement="right"
      :width="720"
      :root-class-name="'log-drawer'"
      @close="logKey = ''"
    >
      <div class="actions">
        <a-button :loading="logBusy" @click="readLog">刷新日志</a-button
        ><span class="muted">{{
          logTime ? `更新于 ${logTime}` : "正在读取"
        }}</span>
      </div>
      <a-alert
        v-if="logError"
        :message="`日志读取失败：${logError}`"
        type="error"
        show-icon
        style="margin-top: 16px"
      />
      <pre class="log-text" ref="logElement">{{ logText }}</pre>
    </a-drawer>
  </main>
</template>
