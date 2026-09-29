<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useMediaQuery, useStorage } from "@vueuse/core";
import { VbenAdminLayout } from "@vben-core/layout-ui";
import { Menu as VbenMenu } from "@vben-core/menu-ui";
import {
  Users,
  QrCode,
  Play,
  Network,
  PanelLeft,
  UserCog,
  Settings,
  Link,
  Wrench,
  SquareTerminal,
  LogOut,
  X,
} from "@lucide/vue";
import zhCN from "ant-design-vue/es/locale/zh_CN";
import { api, errorMessage } from "./api";
import { useSession } from "./session";
import { router } from "./router";

const session = useSession();
const route = useRoute();
const mobile = useMediaQuery("(max-width: 767px)");
const collapsed = useStorage("yyb-vben-collapsed", false);
const shell = ref<InstanceType<typeof VbenAdminLayout>>();
const fatal = ref("");
const loggingOut = ref(false);
let navigationObserver: MutationObserver | undefined;
let navigationOpen = false;
let previousFocus: HTMLElement | null = null;
function syncNavigation() {
  const root = shell.value?.$el as HTMLElement | undefined;
  if (!root) return;
  const open = mobile.value && root.dataset.sidebarCollapsed === "false";
  const main = root.querySelector<HTMLElement>('[data-layout-region="main"]');
  if (main) main.inert = open;
  if (open && !navigationOpen) {
    previousFocus = document.activeElement as HTMLElement;
    nextTick(() =>
      root.querySelector<HTMLElement>("aside a, aside button")?.focus(),
    );
  } else if (!open && navigationOpen) {
    previousFocus?.focus();
  }
  navigationOpen = open;
}
watch([shell, mobile], async () => {
  await nextTick();
  navigationObserver?.disconnect();
  const root = shell.value?.$el as HTMLElement | undefined;
  if (root) {
    navigationObserver = new MutationObserver(syncNavigation);
    navigationObserver.observe(root, {
      attributes: true,
      attributeFilter: ["data-sidebar-collapsed", "data-mobile"],
    });
    syncNavigation();
  }
  shell.value?.$el
    .querySelector('[data-layout-action="toggle-sidebar"]')
    ?.setAttribute("aria-label", "打开导航");
});
const nav = computed(() => [
  { path: "/accounts", name: "微信账号", icon: Users },
  { path: "/scan", name: "扫码添加", icon: QrCode },
  { path: "/runs", name: "运行管理", icon: Play },
  { path: "/proxies", name: "代理设置", icon: Network },
  { path: "/calls", name: "调用配置", icon: SquareTerminal },
  { path: "/account-links", name: "授权链接", icon: Link },
  ...(session.admin
    ? [
        { path: "/users", name: "用户管理", icon: UserCog },
        { path: "/maintenance", name: "系统维护", icon: Wrench },
      ]
    : []),
  ...(session.authEnabled
    ? [{ path: "/settings", name: "个人设置", icon: Settings }]
    : []),
]);
const tabs = useStorage<{ path: string; title: string }[]>(
  "yyb-vben-tabs",
  [],
  sessionStorage,
);
watch(
  () => route.fullPath,
  () => {
    fatal.value = "";
    tabs.value = tabs.value.filter((tab) =>
      nav.value.some((menu) => menu.path === tab.path.split("?")[0]),
    );
    if (
      !route.meta.public &&
      route.meta.title &&
      !tabs.value.some((tab) => tab.path === route.fullPath)
    ) {
      tabs.value = [
        ...tabs.value,
        { path: route.fullPath, title: String(route.meta.title) },
      ].slice(-8);
    }
    closeMobile();
  },
);
function closeMobile() {
  if (mobile.value)
    shell.value?.$el
      .querySelector('[data-layout-region="sidebar-mask"]')
      ?.click();
}
function closeTab(path: string) {
  tabs.value = tabs.value.filter((tab) => tab.path !== path);
  if (route.fullPath === path)
    router.push(tabs.value.at(-1)?.path || "/accounts");
}
async function logout() {
  loggingOut.value = true;
  try {
    await api("/logout", "POST");
    session.clear();
    tabs.value = [];
    await router.replace("/login");
  } catch (error) {
    fatal.value = errorMessage(error);
  } finally {
    loggingOut.value = false;
  }
}
function expired() {
  session.clear();
  router.replace({ path: "/login", query: { next: route.fullPath } });
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Escape") closeMobile();
  if (event.key === "Tab" && navigationOpen) {
    const items = [
      ...(shell.value?.$el.querySelectorAll(
        "aside a[href],aside button:not(:disabled)",
      ) || []),
    ] as HTMLElement[];
    const first = items[0],
      last = items.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }
}
window.addEventListener("yyb-session-expired", expired);
window.addEventListener("keydown", keydown);
const removeRouterError = router.onError((error) => {
  fatal.value = errorMessage(error);
});
onBeforeUnmount(() => {
  navigationObserver?.disconnect();
  removeRouterError();
  window.removeEventListener("yyb-session-expired", expired);
  window.removeEventListener("keydown", keydown);
});
</script>

<template>
  <a-config-provider
    :locale="zhCN"
    :theme="{
      token: {
        colorPrimary: '#b52d48',
        colorTextPlaceholder: '#667085',
        colorTextSecondary: '#576170',
        borderRadius: 6,
        fontFamily: 'system-ui, sans-serif',
      },
    }"
  >
    <template v-if="route.meta.public"><router-view /></template>
    <VbenAdminLayout
      v-else-if="session.loaded"
      ref="shell"
      v-model:sidebar-collapse="collapsed"
      :is-mobile="mobile"
      :sidebar-logo-visible="true"
      :sidebar-width="224"
      :sidebar-draggable="false"
      :sidebar-collapsed-button="false"
      :sidebar-fixed-button="false"
      :header-height="56"
      :tabbar-height="40"
      sidebar-theme="light"
      header-theme="light"
      :header-toggle-sidebar-button="false"
      @toggle-sidebar="collapsed = !collapsed"
    >
      <template #logo
        ><router-link class="brand" to="/accounts"
          ><span class="brand-mark">Y</span
          ><strong v-if="!collapsed || mobile">YYB Go</strong></router-link
        ></template
      >
      <template #menu>
        <nav aria-label="平台功能">
          <button
            v-if="mobile"
            class="icon-button"
            aria-label="关闭导航"
            @click="closeMobile"
          >
            <X :size="18" />
          </button>
          <VbenMenu
            :menus="nav"
            :collapse="collapsed && !mobile"
            :default-active="route.path"
            theme="light"
            @select="(path: string) => router.push(path)"
          />
        </nav>
      </template>
      <template #header>
        <button
          v-if="!mobile"
          class="icon-button"
          aria-label="切换导航"
          title="折叠导航"
          @click="collapsed = !collapsed"
        >
          <PanelLeft :size="18" />
        </button>
        <span class="header-title">{{ route.meta.title }}</span>
        <router-link
          to="/settings"
          class="user-name"
          v-if="session.authEnabled"
          >{{
            session.user?.display_name || session.user?.username
          }}</router-link
        >
        <a-button
          v-if="session.authEnabled"
          type="text"
          aria-label="退出登录"
          title="退出登录"
          :loading="loggingOut"
          @click="logout"
          ><LogOut :size="18"
        /></a-button>
      </template>
      <template #tabbar>
        <div class="page-tabs" aria-label="已打开页面">
          <div
            v-for="tab in tabs"
            :key="tab.path"
            class="page-tab"
            :class="{ active: tab.path === route.fullPath }"
          >
            <router-link :to="tab.path">{{ tab.title }}</router-link
            ><button
              :aria-label="`关闭${tab.title}页签`"
              @click="closeTab(tab.path)"
            >
              <X :size="14" />
            </button>
          </div>
        </div>
      </template>
      <template #content>
        <a-alert
          v-if="fatal"
          type="error"
          show-icon
          :message="fatal"
          class="page-error"
        />
        <router-view />
      </template>
    </VbenAdminLayout>
    <main v-else class="startup">
      <a-alert
        v-if="fatal"
        type="error"
        :message="fatal"
        show-icon
      /><a-skeleton v-else active /><a-button v-if="fatal" @click="router.go(0)"
        >重新连接</a-button
      >
    </main>
  </a-config-provider>
</template>
