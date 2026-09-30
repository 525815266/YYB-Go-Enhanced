<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { message } from "ant-design-vue";
import { Copy, ExternalLink, RefreshCw, Trash2 } from "@lucide/vue";
import { api, accountName, errorMessage, type Account } from "../api";
import { copyText } from "../clipboard";

interface LinkRecord {
  id: number;
  kind: "add" | "update";
  account_id: number;
  account?: Account;
  url?: string;
  status: "active" | "used" | "expired" | "revoked";
  created_at: number;
  expires_at: number;
  used_at?: number;
}
const links = ref<LinkRecord[]>([]);
const counts = ref<Record<string, number>>({});
const status = ref("all"),
  kind = ref("all");
const loading = ref(true),
  busy = ref(false),
  error = ref("");
const filtered = computed(() =>
  links.value.filter(
    (item) =>
      (status.value === "all" || item.status === status.value) &&
      (kind.value === "all" || item.kind === kind.value),
  ),
);
const label = (status: string) =>
  ({ active: "使用中", used: "已消费", expired: "已过期", revoked: "已作废" })[
    status as "active"
  ] || status;
const date = (value?: number) =>
  value
    ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
    : "-";
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const result = await api<{
      links: LinkRecord[];
      counts: Record<string, number>;
    }>("/api/account-links");
    links.value = result.links || [];
    counts.value = result.counts || {};
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
}
async function copy(item: LinkRecord) {
  if (!item.url) return;
  const name = item.account
    ? accountName(item.account)
    : `账号 ${item.account_id}`;
  const text =
    item.kind === "update"
      ? `更新：${name}：${item.url}`
      : `新增账号：${item.url}`;
  try {
    await copyText(text);
    message.success("链接已复制");
  } catch {
    error.value = "复制失败，请使用打开链接按钮后从地址栏复制";
  }
}
async function change(item: LinkRecord, action: "revoke" | "delete") {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await api(
      `/api/account-links/${item.id}${action === "revoke" ? "/revoke" : ""}`,
      action === "revoke" ? "POST" : "DELETE",
    );
    message.success(action === "revoke" ? "链接已作废" : "链接已删除");
    await load();
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}
onMounted(load);
</script>
<template>
  <main class="page">
    <div class="page-head">
      <h1>授权链接</h1>
      <a-button :loading="loading" @click="load"
        ><RefreshCw :size="16" />刷新列表</a-button
      >
    </div>
    <a-alert
      v-if="error"
      class="toolbar"
      type="error"
      show-icon
      :message="error"
    />
    <div class="toolbar">
      <a-select
        v-model:value="status"
        aria-label="状态筛选"
        style="width: 140px"
        :options="[
          { value: 'all', label: '全部状态' },
          { value: 'active', label: '使用中' },
          { value: 'used', label: '已消费' },
          { value: 'expired', label: '已过期' },
          { value: 'revoked', label: '已作废' },
        ]"
      />
      <a-select
        v-model:value="kind"
        aria-label="类型筛选"
        style="width: 140px"
        :options="[
          { value: 'all', label: '全部类型' },
          { value: 'update', label: '更新链接' },
          { value: 'add', label: '新增链接' },
        ]"
      />
      <span class="muted"
        >全部 {{ links.length }} · 使用中 {{ counts.active || 0 }} · 已消费
        {{ counts.used || 0 }}</span
      >
    </div>
    <a-skeleton v-if="loading" active />
    <div v-else-if="filtered.length" class="account-list">
      <article v-for="item in filtered" :key="item.id" class="account-row">
        <div class="account-name">
          <strong
            >{{ item.kind === "update" ? "更新链接" : "新增链接" }} ·
            {{
              item.account
                ? accountName(item.account)
                : `账号 ${item.account_id}`
            }}</strong
          >
          <code>{{ item.url || "历史链接未保存可恢复地址" }}</code>
          <div class="muted">
            创建 {{ date(item.created_at) }} · 失效 {{ date(item.expires_at)
            }}<span v-if="item.used_at"> · 消费 {{ date(item.used_at) }}</span>
          </div>
        </div>
        <a-tag :color="item.status === 'active' ? 'green' : 'default'">{{
          label(item.status)
        }}</a-tag>
        <div class="actions">
          <a-button
            v-if="item.url"
            size="small"
            title="复制链接"
            @click="copy(item)"
            ><Copy :size="15"
          /></a-button>
          <a-button
            v-if="item.url"
            size="small"
            :href="item.url"
            target="_blank"
            rel="noopener noreferrer"
            title="打开链接"
            ><ExternalLink :size="15"
          /></a-button>
          <a-popconfirm
            v-if="item.status === 'active'"
            title="作废后链接将立即失效，保留历史记录。"
            @confirm="change(item, 'revoke')"
          >
            <a-button size="small" :disabled="busy">作废</a-button>
          </a-popconfirm>
          <a-popconfirm
            title="删除后不再保留这条历史记录，确定继续？"
            @confirm="change(item, 'delete')"
          >
            <a-button size="small" danger :disabled="busy" title="删除链接"
              ><Trash2 :size="15"
            /></a-button>
          </a-popconfirm>
        </div>
      </article>
    </div>
    <a-empty v-else description="暂无符合条件的授权链接" />
  </main>
</template>
