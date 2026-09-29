import { fileURLToPath } from "node:url";

const src = (path: string) =>
  fileURLToPath(new URL(`./vendor/${path}`, import.meta.url));
export const vbenAliases = {
  "@vben-core/layout-ui": src("layout/src/index.ts"),
  "@vben-core/menu-ui": src("menu/src/index.ts"),
  "@vben-core/shadcn-ui": src("shadcn/src/index.ts"),
  "@vben-core/composables": src("composables/src/index.ts"),
  "@vben-core/typings": src("typings/src/index.ts"),
  "@vben-core/icons": src("icons/src/index.ts"),
  "@vben-core/shared/constants": src("shared/src/constants/index.ts"),
  "@vben-core/shared/utils": src("shared/src/utils/index.ts"),
  "@vben-core/shared/color": src("shared/src/color/index.ts"),
  "@vben-core/shared/cache": src("shared/src/cache/index.ts"),
  "@vben-core/shared/store": src("shared/src/store.ts"),
  "@vben-core/shared/global-state": src("shared/src/global-state.ts"),
  "@vben-core/design/bem": src("design/src/scss-bem/bem.scss"),
  "@vben-core/design": src("design/src/index.ts"),
  "@vben/tailwind-config/theme": src("theme.css"),
};
