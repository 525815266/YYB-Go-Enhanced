<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, errorMessage } from "../api";
import { useSession } from "../session";
const route = useRoute(),
  router = useRouter(),
  session = useSession();
const register = computed(() => route.path === "/register");
const username = ref(""),
  password = ref(""),
  displayName = ref(""),
  confirmPassword = ref("");
const busy = ref(false),
  error = ref("");
async function submit() {
  if (busy.value) return;
  error.value = "";
  if (register.value && password.value !== confirmPassword.value) {
    error.value = "两次输入的密码不一致";
    return;
  }
  busy.value = true;
  try {
    await api(register.value ? "/register" : "/login", "POST", {
      username: username.value,
      password: password.value,
      displayName: displayName.value,
    });
    await session.load();
    const next =
      typeof route.query.next === "string" ? route.query.next : "/accounts";
    await router.replace(
      next.startsWith("/") &&
        !next.startsWith("//") &&
        !/^\/(login|register)(\?|$)/.test(next)
        ? next
        : "/accounts",
    );
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <main class="auth">
    <div class="auth-inner">
      <div class="brand">
        <span class="brand-mark">Y</span><strong>YYB Go 控制台</strong>
      </div>
      <h1>{{ register ? "创建账号" : "登录" }}</h1>
      <a-alert
        v-if="error"
        :message="error"
        type="error"
        show-icon
        role="alert"
      />
      <a-form layout="vertical" @submit="submit">
        <a-form-item label="用户名" html-for="username"
          ><a-input
            v-model:value="username"
            id="username"
            autocomplete="username"
            required
            :maxlength="64"
        /></a-form-item>
        <a-form-item
          v-if="register"
          label="显示名称"
          html-for="auth-display-name"
          ><a-input
            v-model:value="displayName"
            id="auth-display-name"
            autocomplete="nickname"
            :maxlength="80"
        /></a-form-item>
        <a-form-item label="密码" html-for="password"
          ><a-input-password
            v-model:value="password"
            id="password"
            :autocomplete="register ? 'new-password' : 'current-password'"
            required
            :minlength="register ? 8 : undefined"
        /></a-form-item>
        <a-form-item
          v-if="register"
          label="确认密码"
          html-for="auth-confirm-password"
          ><a-input-password
            v-model:value="confirmPassword"
            id="auth-confirm-password"
            required
            autocomplete="new-password"
        /></a-form-item>
        <a-button type="primary" html-type="submit" block :loading="busy">{{
          register ? "注册" : "登录"
        }}</a-button>
      </a-form>
      <p style="margin-top: 20px; text-align: center">
        <router-link :to="register ? '/login' : '/register'">{{
          register ? "返回登录" : "注册账号"
        }}</router-link>
      </p>
    </div>
  </main>
</template>
