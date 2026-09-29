import { createApp } from "vue";
import { createPinia } from "pinia";
import {
  Alert,
  Button,
  ConfigProvider,
  Drawer,
  Empty,
  Form,
  Input,
  Popconfirm,
  Result,
  Segmented,
  Select,
  Skeleton,
  Switch,
  Tabs,
  Tag,
} from "ant-design-vue";
import { addIcon } from "@iconify/vue";
import icons from "../vendor/layout-icons.json";
import App from "./App.vue";
import { router } from "./router";
import "./style.css";

// Bundle layout icons locally: console operation must not depend on Iconify CDN.
addIcon("ep:fold", { ...icons.fold, width: icons.width, height: icons.height });
addIcon("ep:expand", {
  ...icons.expand,
  width: icons.width,
  height: icons.height,
});
const app = createApp(App).use(createPinia());
for (const component of [
  Alert,
  Button,
  ConfigProvider,
  Drawer,
  Empty,
  Form,
  Input,
  Popconfirm,
  Result,
  Segmented,
  Select,
  Skeleton,
  Switch,
  Tabs,
  Tag,
])
  app.use(component);
app.use(router).mount("#app");
