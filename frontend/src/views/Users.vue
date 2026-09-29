<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { message } from "ant-design-vue";
import {
  api,
  accountName,
  errorMessage,
  type User,
  type Account,
} from "../api";
import { useSession } from "../session";
const session = useSession();
const users = ref<User[]>([]),
  search = ref(""),
  registration = ref(false);
const loading = ref(true),
  busy = ref(false),
  error = ref(""),
  creating = ref(false);
const form = reactive({
  username: "",
  display_name: "",
  password: "",
  role: "user",
});
const resetUser = ref<User | null>(null),
  resetPassword = ref(""),
  confirmPassword = ref("");
const inspected = ref<User | null>(null),
  inspectedAccounts = ref<Account[]>([]),
  inspecting = ref(false);
const filtered = computed(() =>
  users.value.filter((user) =>
    `${user.username} ${user.display_name}`
      .toLowerCase()
      .includes(search.value.toLowerCase()),
  ),
);
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const [list, setting] = await Promise.all([
      api<User[]>("/api/auth/users"),
      api<{ enabled: boolean }>("/api/auth/registration"),
    ]);
    users.value = list;
    registration.value = setting.enabled;
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
}
async function mutate(
  path: string,
  method: string,
  body?: unknown,
  success = "已保存",
) {
  if (busy.value) return false;
  busy.value = true;
  error.value = "";
  try {
    await api(path, method, body);
    message.success(success);
    await load();
    return true;
  } catch (cause) {
    error.value = errorMessage(cause);
    return false;
  } finally {
    busy.value = false;
  }
}
async function create() {
  if (await mutate("/api/auth/users", "POST", form, "用户已创建")) {
    creating.value = false;
    Object.assign(form, {
      username: "",
      display_name: "",
      password: "",
      role: "user",
    });
  }
}
async function reset() {
  if (!resetUser.value) return;
  if (resetPassword.value !== confirmPassword.value) {
    error.value = "两次输入的密码不一致";
    return;
  }
  if (
    await mutate(
      `/api/auth/users/${resetUser.value.id}/password`,
      "PUT",
      { password: resetPassword.value },
      "密码已重置，用户会话已注销",
    )
  ) {
    resetUser.value = null;
    resetPassword.value = "";
    confirmPassword.value = "";
  }
}
async function inspect(user: User) {
  inspected.value = user;
  inspectedAccounts.value = [];
  inspecting.value = true;
  try {
    const result = await api<{ accounts: Account[] }>(
      `/api/auth/users/${user.id}/accounts`,
    );
    if (inspected.value?.id === user.id)
      inspectedAccounts.value = result.accounts;
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    if (inspected.value?.id === user.id) inspecting.value = false;
  }
}
async function toggleRegistration(value: boolean | string | number) {
  const enabled = Boolean(value);
  await mutate(
    "/api/auth/registration",
    "PUT",
    { enabled },
    enabled ? "已开放用户注册" : "已关闭用户注册",
  );
}
onMounted(load);
</script>
<template>
  <main class="page">
    <div class="page-head">
      <h1>用户管理</h1>
      <a-button type="primary" @click="creating = !creating">{{
        creating ? "收起表单" : "新增用户"
      }}</a-button>
    </div>
    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      class="toolbar"
      closable
      @close="error = ''"
    />
    <section v-if="creating" class="section" aria-label="新增用户">
      <h2>新增用户</h2>
      <a-form layout="vertical" @submit="create" class="field-grid">
        <a-form-item label="用户名" html-for="user-create-name"
          ><a-input
            v-model:value="form.username"
            id="user-create-name"
            required
            autocomplete="off"
            :maxlength="64"
        /></a-form-item>
        <a-form-item label="显示名称" html-for="user-create-display"
          ><a-input
            id="user-create-display"
            v-model:value="form.display_name"
            :maxlength="80"
        /></a-form-item>
        <a-form-item label="初始密码" html-for="user-create-password"
          ><a-input-password
            v-model:value="form.password"
            id="user-create-password"
            required
            :minlength="8"
            autocomplete="new-password"
        /></a-form-item>
        <a-form-item label="角色" html-for="user-create-role"
          ><a-select
            v-model:value="form.role"
            id="user-create-role"
            :options="[
              { label: '普通用户', value: 'user' },
              { label: '管理员', value: 'admin' },
            ]"
        /></a-form-item>
        <div class="actions wide">
          <a-button type="primary" html-type="submit" :loading="busy"
            >创建用户</a-button
          ><a-button
            @click="
              creating = false;
              form.password = '';
            "
            >取消</a-button
          >
        </div>
      </a-form>
    </section>
    <div class="toolbar">
      <a-input-search
        v-model:value="search"
        placeholder="搜索用户名或名称"
        aria-label="搜索用户"
      /><a-switch
        :checked="registration"
        :loading="busy"
        aria-label="开放用户注册"
        @change="toggleRegistration"
      /><span>开放注册</span
      ><a-button :loading="loading" @click="load">刷新</a-button>
    </div>
    <a-skeleton v-if="loading" active />
    <div v-else-if="filtered.length" class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>用户</th>
            <th>角色</th>
            <th>账号数</th>
            <th>最近登录</th>
            <th>启用</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in filtered" :key="user.id">
            <td>
              <strong>{{ user.display_name || user.username }}</strong>
              <div class="muted">{{ user.username }} · ID {{ user.id }}</div>
            </td>
            <td>
              <a-select
                :value="user.role"
                :disabled="busy || user.id === session.user?.id"
                :options="[
                  { label: '普通用户', value: 'user' },
                  { label: '管理员', value: 'admin' },
                ]"
                style="width: 110px"
                @change="
                  (role: string) =>
                    mutate(`/api/auth/users/${user.id}/state`, 'PUT', {
                      role,
                      enabled: user.enabled,
                    })
                "
              />
            </td>
            <td>{{ user.account_count || 0 }}</td>
            <td>
              {{
                user.last_login_at
                  ? new Date(user.last_login_at).toLocaleString("zh-CN")
                  : "从未登录"
              }}
            </td>
            <td>
              <a-switch
                :checked="user.enabled"
                :disabled="busy || user.id === session.user?.id"
                :aria-label="`启用${user.username}`"
                @change="
                  (enabled: boolean) =>
                    mutate(`/api/auth/users/${user.id}/state`, 'PUT', {
                      role: user.role,
                      enabled,
                    })
                "
              />
            </td>
            <td>
              <div class="actions">
                <a-button size="small" @click="inspect(user)">查看账号</a-button
                ><a-button
                  size="small"
                  @click="
                    resetUser = user;
                    resetPassword = '';
                    confirmPassword = '';
                  "
                  >重置密码</a-button
                >
                <a-popconfirm
                  title="删除用户后无法恢复，确认删除？"
                  @confirm="
                    mutate(
                      `/api/auth/users/${user.id}/delete`,
                      'DELETE',
                      undefined,
                      '用户已删除',
                    )
                  "
                  ><a-button
                    size="small"
                    danger
                    :disabled="busy || user.id === session.user?.id"
                    >删除</a-button
                  ></a-popconfirm
                >
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <a-empty v-else description="没有符合条件的用户" />
    <section v-if="resetUser" class="section" style="margin-top: 24px">
      <h2>重置 {{ resetUser.display_name || resetUser.username }} 的密码</h2>
      <a-form layout="vertical" @submit="reset" class="field-grid"
        ><a-form-item label="新密码" html-for="user-reset-password"
          ><a-input-password
            v-model:value="resetPassword"
            id="user-reset-password"
            required
            :minlength="8"
            autocomplete="new-password" /></a-form-item
        ><a-form-item label="确认密码" html-for="user-reset-confirm"
          ><a-input-password
            v-model:value="confirmPassword"
            id="user-reset-confirm"
            required
            autocomplete="new-password"
        /></a-form-item>
        <div class="actions wide">
          <a-button html-type="submit" :loading="busy">保存新密码</a-button
          ><a-button
            @click="
              resetUser = null;
              resetPassword = '';
              confirmPassword = '';
            "
            >取消</a-button
          >
        </div></a-form
      >
    </section>
    <section v-if="inspected" class="section" style="margin-top: 24px">
      <div class="page-head">
        <h2>{{ inspected.display_name || inspected.username }} 的账号</h2>
        <a-button @click="inspected = null">关闭</a-button>
      </div>
      <a-skeleton v-if="inspecting" active />
      <ul v-else>
        <li v-for="account in inspectedAccounts" :key="account.id">
          ID {{ account.id }} · {{ accountName(account) }} ·
          {{ account.status }}
        </li>
      </ul>
      <a-empty
        v-if="!inspecting && !inspectedAccounts.length"
        description="尚未绑定微信账号"
      />
    </section>
  </main>
</template>
