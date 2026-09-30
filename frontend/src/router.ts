import { createRouter, createWebHistory } from "vue-router";
import { APIError } from "./api";
import { useSession } from "./session";

export const router = createRouter({
  history: createWebHistory("/console/"),
  routes: [
    { path: "/", redirect: "/accounts" },
    {
      path: "/login",
      component: () => import("./views/Auth.vue"),
      meta: { public: true, title: "登录" },
    },
    {
      path: "/register",
      component: () => import("./views/Auth.vue"),
      meta: { public: true, title: "注册" },
    },
    {
      path: "/accounts",
      component: () => import("./views/Accounts.vue"),
      meta: { title: "微信账号" },
    },
    {
      path: "/runs",
      component: () => import("./views/Runs.vue"),
      meta: { title: "运行管理" },
    },
    {
      path: "/users",
      component: () => import("./views/Users.vue"),
      meta: { title: "用户管理", admin: true },
    },
    {
      path: "/settings",
      component: () => import("./views/Settings.vue"),
      meta: { title: "个人设置", auth: true },
    },
    {
      path: "/calls",
      component: () => import("./views/Calls.vue"),
      meta: { title: "调用配置" },
    },
    {
      path: "/scan",
      component: () => import("./views/Scan.vue"),
      meta: { title: "扫码添加" },
    },
    {
      path: "/proxies",
      component: () => import("./views/Proxies.vue"),
      meta: { title: "代理设置" },
    },
    {
      path: "/account-links",
      component: () => import("./views/AccountLinks.vue"),
      meta: { title: "授权链接" },
    },
    {
      path: "/maintenance",
      component: () => import("./views/Maintenance.vue"),
      meta: { title: "系统维护", admin: true },
    },
    {
      path: "/:pathMatch(.*)*",
      component: () => import("./views/NotFound.vue"),
      meta: { title: "页面不存在" },
    },
  ],
});

router.beforeEach(async (to) => {
  if (to.meta.public) return;
  const session = useSession();
  if (!session.loaded) {
    try {
      await session.load();
    } catch (error) {
      // Network/500 errors stay visible instead of starting a login reload loop.
      if (error instanceof APIError && error.status === 401)
        return { path: "/login", query: { next: to.fullPath } };
      throw error;
    }
  }
  if (to.meta.admin && !session.admin) return "/accounts";
  if (to.meta.auth && !session.authEnabled) return "/accounts";
});
router.afterEach((to) => {
  document.title = `${to.meta.title || "控制台"} · YYB Go`;
});
