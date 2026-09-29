<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { message } from "ant-design-vue";
import { RefreshCw, Network, Play, Pencil, Trash2 } from "@lucide/vue";
import { api, accountName, errorMessage, type Account } from "../api";
import { useSession } from "../session";
const session = useSession(),
  route = useRoute(),
  router = useRouter();
const accounts = ref<Account[]>([]),
  query = ref(""),
  status = ref("");
const loading = ref(true),
  busy = ref(false),
  error = ref(""),
  editing = ref<Account | null>(null),
  remark = ref("");
const list = computed(() =>
  accounts.value.filter(
    (account) =>
      (!status.value || account.status === status.value) &&
      `${account.id} ${accountName(account)} ${account.openid}`
        .toLowerCase()
        .includes(query.value.toLowerCase()),
  ),
);
const statusText = (value: string) =>
  value === "alive" ? "可用" : value === "expired" ? "需重扫" : "待确认";
async function load() {
  loading.value = true;
  error.value = "";
  try {
    accounts.value = await api<Account[]>("/accounts");
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
}
async function action(
  path: string,
  method = "POST",
  body?: unknown,
  label = "操作完成",
) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await api(path, method, body);
    message.success(label);
    await load();
    editing.value = null;
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}
function proxy(account: Account) {
  router.push({
    path: "/proxies",
    query: {
      ref: String(account.id),
      return: `/console/accounts?ref=${account.id}`,
    },
  });
}
function scrollToAccount() {
  const id = String(route.query.ref || "");
  if (/^\d+$/.test(id))
    requestAnimationFrame(() =>
      document
        .getElementById(`account-${id}`)
        ?.scrollIntoView({ block: "center" }),
    );
}
watch(loading, (value) => {
  if (!value) scrollToAccount();
});
onMounted(load);
</script>
<template>
  <main class="page">
    <div class="page-head">
      <h1>
        微信账号 <span class="muted">{{ accounts.length }} 个</span>
      </h1>
      <div class="actions">
        <a-button type="primary" @click="router.push('/scan')"
          >扫码添加</a-button
        ><a-button @click="router.push('/account-links')">授权链接</a-button>
      </div>
    </div>
    <a-alert
      v-if="error"
      show-icon
      type="error"
      :message="error"
      class="toolbar"
    />
    <div class="toolbar">
      <a-input-search
        v-model:value="query"
        placeholder="搜索名称、备注或账号 ID"
        aria-label="搜索账号"
        allow-clear
      /><a-select
        v-model:value="status"
        aria-label="筛选账号状态"
        style="width: 130px"
        :options="[
          { label: '全部状态', value: '' },
          { label: '可用', value: 'alive' },
          { label: '需重扫', value: 'expired' },
          { label: '待确认', value: 'unknown' },
        ]"
      />
      <a-button
        :loading="loading"
        title="刷新列表"
        aria-label="刷新列表"
        @click="load"
        ><RefreshCw :size="16" /></a-button
      ><a-button :loading="busy" @click="action('/accounts/refresh')"
        >刷新存活</a-button
      ><a-button :disabled="busy" @click="action('/accounts/resync')"
        >同步资料</a-button
      >
      <a-button
        v-if="session.admin"
        :disabled="busy"
        @click="action('/api/qinglong/sync-all')"
        >同步全部到面板</a-button
      >
    </div>
    <section class="section" v-if="editing">
      <h2>账号 {{ editing.id }} · {{ accountName(editing) }}</h2>
      <a-form
        layout="vertical"
        @submit="
          action(
            '/accounts/remark',
            'PUT',
            { ref: String(editing!.id), remark },
            '备注已保存',
          )
        "
        ><a-form-item label="备注" html-for="account-remark"
          ><a-input id="account-remark" v-model:value="remark" :maxlength="200"
        /></a-form-item>
        <div class="actions">
          <a-button type="primary" html-type="submit" :loading="busy"
            >保存备注</a-button
          ><a-button @click="editing = null">取消</a-button>
        </div></a-form
      >
    </section>
    <a-skeleton v-if="loading" active />
    <div v-else-if="list.length" class="account-list">
      <article
        v-for="account in list"
        :key="account.id"
        class="account-row"
        :id="`account-${account.id}`"
      >
        <span class="account-number">#{{ account.id }}</span>
        <div class="account-name">
          <strong>{{ accountName(account) }}</strong
          ><code>{{ account.openid }}</code>
        </div>
        <a-tag
          :color="
            account.status === 'alive'
              ? 'green'
              : account.status === 'expired'
                ? 'red'
                : 'default'
          "
          >{{ statusText(account.status) }}</a-tag
        >
        <div class="actions">
          <a-button size="small" @click="proxy(account)" title="代理设置"
            ><Network :size="15" /></a-button
          ><a-button
            size="small"
            @click="
              router.push({ path: '/runs', query: { ref: String(account.id) } })
            "
            title="运行管理"
            ><Play :size="15" /></a-button
          ><a-button
            size="small"
            @click="
              editing = account;
              remark = account.remark || '';
            "
            title="修改备注"
            :disabled="busy"
            ><Pencil :size="15"
          /></a-button>
          <a-button
            size="small"
            :disabled="busy"
            @click="
              action(
                '/api/qinglong/sync',
                'POST',
                { ref: String(account.id) },
                '已同步到面板',
              )
            "
            >同步</a-button
          >
          <a-popconfirm
            title="删除账号及关联面板条目和专属任务？此操作不可撤销。"
            @confirm="
              action(
                `/accounts?ref=${account.id}`,
                'DELETE',
                undefined,
                '账号已删除',
              )
            "
            ><a-button
              size="small"
              danger
              :disabled="busy"
              :title="`删除账号 ${account.id}`"
              ><Trash2 :size="15" /></a-button
          ></a-popconfirm>
        </div>
      </article>
    </div>
    <a-empty
      v-else
      :description="accounts.length ? '没有符合条件的账号' : '尚未添加微信账号'"
    />
  </main>
</template>
