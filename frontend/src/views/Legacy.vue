<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
const route = useRoute(),
  router = useRouter(),
  frame = ref<HTMLIFrameElement>();
const loading = ref(true),
  failed = ref(false);
const paths: Record<string, string> = {
  "/scan": "/scan",
  "/proxies": "/proxies",
  "/account-links": "/account-links",
  "/maintenance": "/maintenance",
};
const source = computed(() => {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(route.query))
    if (typeof value === "string") params.set(key, value);
  return `${paths[route.path] || "/"}?${params}`;
});
watch(source, () => {
  loading.value = true;
  failed.value = false;
});
function loaded() {
  loading.value = false;
  const document = frame.value?.contentDocument;
  if (!document) {
    failed.value = true;
    return;
  }
  if (document.location.pathname === "/login") {
    router.replace("/login");
    return;
  }
  const style = document.createElement("style");
  style.textContent = `.platform-sidebar,.platform-topbar,.platform-tabs,.platform-overlay{display:none!important}.platform-shell{display:block!important}.platform-stage{margin:0!important}.platform-main>main{padding:20px!important}body{min-height:0!important}`;
  document.head.append(style);
  // Intercept legacy navigation once per frame load, keeping query/account context.
  document.addEventListener("click", (event) => {
    const link = (event.target as HTMLElement).closest("a");
    if (!link || link.target === "_blank" || event.ctrlKey || event.metaKey)
      return;
    const url = new URL(link.href, location.origin);
    if (url.origin !== location.origin) return;
    let target = "";
    if (url.pathname.startsWith("/console/"))
      target = url.pathname.slice("/console".length);
    else if (url.pathname === "/")
      target =
        url.searchParams.get("focus") === "test" ? "/calls" : "/accounts";
    else if (
      Object.values(paths).includes(url.pathname) ||
      ["/runs", "/settings", "/users"].includes(url.pathname)
    )
      target = url.pathname;
    if (target) {
      event.preventDefault();
      router.push(target + url.search + url.hash);
    }
  });
}
</script>
<template>
  <a-skeleton v-if="loading" active style="padding: 24px" />
  <a-result v-if="failed" status="error" title="页面加载失败"
    ><template #extra
      ><a-button @click="frame?.contentWindow?.location.reload()"
        >重新加载</a-button
      ></template
    ></a-result
  >
  <iframe
    ref="frame"
    class="legacy-frame"
    :src="source"
    :title="String(route.meta.title)"
    @load="loaded"
    @error="
      failed = true;
      loading = false;
    "
  />
</template>
