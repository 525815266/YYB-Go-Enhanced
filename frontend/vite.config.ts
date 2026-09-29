import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwind from "@tailwindcss/vite";
import { vbenAliases } from "./vben-aliases";

export default defineConfig({
  base: "/static/console/",
  plugins: [vue(), tailwind()],
  resolve: { alias: vbenAliases },
  build: { outDir: "../resource/static/console", emptyOutDir: true },
  server: {
    port: 18117,
    strictPort: true,
    proxy: Object.fromEntries(
      ["/api", "/accounts", "/login", "/register", "/logout", "/wx", "/qr"].map(
        (path) => [path, "http://127.0.0.1:18016"],
      ),
    ),
  },
});
