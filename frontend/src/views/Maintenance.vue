<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { api, errorMessage } from "../api";
interface State {
  version: string;
  latest_version?: string;
  has_update?: boolean;
  check_error?: string;
  managed_update?: boolean;
  message?: string;
  runtime?: {
    label?: string;
    instructions?: string;
    download_url?: string;
    download_available?: boolean;
  };
  agent?: {
    job?: { running?: boolean; message?: string; finished_at?: number };
  };
}
const state = ref<State | null>(null),
  loading = ref(true),
  checking = ref(false);
const error = ref(""),
  note = ref(""),
  pending = ref<"update" | "restart" | "">("");
let timer: number | undefined,
  stopped = false,
  deadline = 0;
async function load(check = false) {
  const result = await api<State>(`/api/maintenance${check ? "?check=1" : ""}`);
  state.value = result;
  if (result.check_error) note.value = result.check_error;
  else if (check && !result.has_update) note.value = "当前版本无需更新";
  else if (result.agent?.job?.message) note.value = result.agent.job.message;
  if (result.agent?.job?.running && !timer) {
    deadline = Date.now() + 12 * 60 * 1000;
    timer = window.setInterval(poll, 3000);
  }
  return Boolean(result.agent?.job?.running);
}
async function poll() {
  if (stopped || Date.now() > deadline) {
    if (timer) clearInterval(timer);
    timer = undefined;
    if (!stopped)
      note.value = "等待超过 12 分钟，请刷新核对执行结果，不要重复提交";
    return;
  }
  try {
    if (!(await load())) {
      clearInterval(timer);
      timer = undefined;
    }
  } catch {
    note.value = "服务正在切换，等待重新连接";
  }
}
async function check() {
  checking.value = true;
  error.value = "";
  try {
    await load(true);
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    checking.value = false;
  }
}
async function submit() {
  if (!pending.value || checking.value) return;
  const action = pending.value;
  pending.value = "";
  checking.value = true;
  error.value = "";
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const requestID = [...bytes]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  try {
    const response = await fetch("/api/maintenance", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-YYB-Maintenance": "1" },
      body: JSON.stringify({ action, confirm: true, request_id: requestID }),
    });
    const payload = await response.json();
    if (!response.ok || payload.code !== 0)
      throw new Error(payload.msg || `HTTP ${response.status}`);
    note.value = "操作已受理，正在等待执行结果";
    deadline = Date.now() + 12 * 60 * 1000;
    timer = window.setInterval(poll, 3000);
  } catch (cause) {
    error.value = `${errorMessage(cause)}。请刷新核对执行状态后再试`;
  } finally {
    checking.value = false;
  }
}
onMounted(async () => {
  try {
    await load();
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
});
onBeforeUnmount(() => {
  stopped = true;
  if (timer) clearInterval(timer);
});
</script>
<template>
  <main class="page">
    <div class="page-head">
      <h1>系统维护</h1>
      <a-button :loading="checking" @click="check">检查更新</a-button>
    </div>
    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      class="toolbar"
    />
    <a-skeleton v-if="loading" active />
    <template v-else-if="state">
      <section class="section">
        <div class="toolbar">
          <strong>当前版本 v{{ state.version }}</strong
          ><span class="muted"
            >主分支版本
            {{
              state.latest_version ? `v${state.latest_version}` : "尚未检查"
            }}</span
          >
        </div>
        <p>
          {{ state.managed_update ? "Docker 维护执行器已连接" : state.message }}
        </p>
        <p class="muted">{{ state.runtime?.instructions }}</p>
        <div class="actions">
          <a-button
            type="primary"
            :disabled="
              !state.managed_update ||
              !state.has_update ||
              checking ||
              state.agent?.job?.running
            "
            @click="pending = 'update'"
            >更新服务</a-button
          >
          <a-button
            :disabled="
              !state.managed_update || checking || state.agent?.job?.running
            "
            @click="pending = 'restart'"
            >重启服务</a-button
          >
          <a-button
            v-if="
              state.runtime?.download_available &&
              state.runtime.download_url &&
              state.has_update
            "
            :href="state.runtime.download_url"
            target="_blank"
            rel="noopener"
            >下载 {{ state.runtime.label || "当前平台版本" }}</a-button
          >
          <a-button
            href="https://github.com/525815266/YYB-Go-Enhanced/blob/main/docs/maintenance.md"
            target="_blank"
            rel="noopener"
            >配置说明</a-button
          >
        </div>
      </section>
      <a-alert v-if="note" type="info" show-icon :message="note" />
      <section
        v-if="pending"
        class="section"
        style="margin-top: 20px"
        aria-label="确认维护操作"
      >
        <h2>确认{{ pending === "update" ? "更新服务" : "重启服务" }}</h2>
        <p>保留现有配置与数据卷，不操作其他容器。是否继续？</p>
        <div class="actions">
          <a-button danger :loading="checking" @click="submit"
            >确认执行</a-button
          ><a-button @click="pending = ''">取消</a-button>
        </div>
      </section>
    </template>
  </main>
</template>
