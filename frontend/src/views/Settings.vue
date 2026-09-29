<script setup lang="ts">
import { onMounted, ref } from "vue";
import { message } from "ant-design-vue";
import { api, errorMessage } from "../api";
import { useSession } from "../session";
const session = useSession();
const name = ref(session.user?.display_name || ""),
  current = ref(""),
  password = ref(""),
  confirm = ref("");
const error = ref(""),
  busy = ref(false),
  loading = ref(true);
interface Session {
  id: string;
  ip_address: string;
  user_agent: string;
  last_seen_at: string;
}
const sessions = ref<Session[]>([]),
  currentID = ref("");
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const result = await api<{
      sessions: Session[];
      current_session_id: string;
    }>("/api/auth/sessions");
    sessions.value = result.sessions;
    currentID.value = result.current_session_id;
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
}
async function save(kind: "profile" | "password" | "sessions") {
  if (busy.value) return;
  error.value = "";
  if (kind === "password" && password.value !== confirm.value) {
    error.value = "两次输入的密码不一致";
    return;
  }
  busy.value = true;
  try {
    if (kind === "profile") {
      await api("/api/auth/profile", "PUT", { display_name: name.value });
      await session.load();
      message.success("基本信息已保存");
    }
    if (kind === "password") {
      await api("/api/auth/password", "PUT", {
        current_password: current.value,
        new_password: password.value,
      });
      current.value = "";
      password.value = "";
      confirm.value = "";
      message.success("密码已更新，其他会话已注销");
    }
    if (kind === "sessions") {
      await api("/api/auth/sessions", "DELETE");
      message.success("其他会话已注销");
    }
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
    <div class="page-head"><h1>个人设置</h1></div>
    <a-alert
      v-if="error"
      type="error"
      :message="error"
      show-icon
      class="toolbar"
    />
    <section class="section">
      <h2>基本信息</h2>
      <a-form layout="vertical" @submit="save('profile')" class="field-grid">
        <a-form-item label="用户名" html-for="profile-username"
          ><a-input
            id="profile-username"
            :value="session.user?.username"
            disabled
        /></a-form-item>
        <a-form-item label="显示名称" html-for="profile-name"
          ><a-input
            id="profile-name"
            v-model:value="name"
            required
            :maxlength="80"
        /></a-form-item>
        <div class="wide">
          <a-button html-type="submit" type="primary" :loading="busy"
            >保存基本信息</a-button
          >
        </div>
      </a-form>
    </section>
    <section class="section">
      <h2>修改密码</h2>
      <a-form layout="vertical" @submit="save('password')" class="field-grid">
        <a-form-item label="当前密码" html-for="profile-current-password"
          ><a-input-password
            v-model:value="current"
            id="profile-current-password"
            required
            autocomplete="current-password"
        /></a-form-item>
        <a-form-item label="新密码" html-for="profile-new-password"
          ><a-input-password
            v-model:value="password"
            id="profile-new-password"
            required
            :minlength="8"
            autocomplete="new-password"
        /></a-form-item>
        <a-form-item label="确认新密码" html-for="profile-confirm-password"
          ><a-input-password
            v-model:value="confirm"
            id="profile-confirm-password"
            required
            :minlength="8"
            autocomplete="new-password"
        /></a-form-item>
        <div class="wide">
          <a-button html-type="submit" :loading="busy">更新密码</a-button>
        </div>
      </a-form>
    </section>
    <section class="section">
      <div class="page-head">
        <h2>登录会话</h2>
        <a-button :loading="busy" @click="save('sessions')"
          >注销其他会话</a-button
        >
      </div>
      <a-skeleton v-if="loading" active />
      <div class="table-scroll" v-else>
        <table class="data-table">
          <thead>
            <tr>
              <th>会话</th>
              <th>IP 地址</th>
              <th>设备</th>
              <th>最近活动</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in sessions" :key="item.id">
              <td>{{ item.id === currentID ? "当前会话" : "其他会话" }}</td>
              <td>{{ item.ip_address }}</td>
              <td style="white-space: normal; max-width: 500px">
                {{ item.user_agent }}
              </td>
              <td>{{ new Date(item.last_seen_at).toLocaleString("zh-CN") }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
</template>
