import { cpSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

// Deterministic vendor import: no install hooks or upstream app/demo code.
const revision = "50f4ede309d4450c7dd417399cb8d5c02346d2d2";
const source = resolve(process.argv[2] || "../.codex-tmp/vben-upstream");
if (
  execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: source,
    encoding: "utf8",
  }).trim() !== revision
) {
  throw new Error(`Expected Vben revision ${revision}`);
}
const packages = {
  layout: "packages/@core/ui-kit/layout-ui",
  menu: "packages/@core/ui-kit/menu-ui",
  shadcn: "packages/@core/ui-kit/shadcn-ui",
  composables: "packages/@core/composables",
  shared: "packages/@core/base/shared",
  icons: "packages/@core/base/icons",
  typings: "packages/@core/base/typings",
  design: "packages/@core/base/design",
};
mkdirSync("vendor", { recursive: true });
for (const [name, path] of Object.entries(packages)) {
  cpSync(resolve(source, path, "src"), `vendor/${name}/src`, {
    recursive: true,
    filter: (path) =>
      !path.includes("__tests__") && !/\.(test|spec)\./.test(path),
  });
}
cpSync(
  resolve(source, "internal/tailwind-config/src/theme.css"),
  "vendor/theme.css",
);
cpSync(resolve(source, "LICENSE"), "vendor/LICENSE");
writeFileSync(
  "vendor/UPSTREAM.json",
  JSON.stringify(
    {
      repository: "https://github.com/vbenjs/vue-vben-admin",
      revision,
      packages,
      license: "MIT",
      sourceVersion: JSON.parse(readFileSync(resolve(source, "package.json")))
        .version,
    },
    null,
    2,
  ) + "\n",
);
