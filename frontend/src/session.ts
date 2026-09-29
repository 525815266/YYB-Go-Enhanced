import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { api, type User } from "./api";

export const useSession = defineStore("session", () => {
  const user = ref<User | null>(null);
  const authEnabled = ref(true);
  const loaded = ref(false);
  const admin = computed(
    () => !authEnabled.value || user.value?.role === "admin",
  );
  async function load() {
    const result = await api<{ user: User; auth_enabled: boolean }>(
      "/api/auth/me",
    );
    user.value = result.user;
    authEnabled.value = result.auth_enabled;
    loaded.value = true;
  }
  function clear() {
    user.value = null;
    loaded.value = false;
  }
  return { user, loaded, admin, authEnabled, load, clear };
});
