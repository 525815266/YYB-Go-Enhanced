#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ "${YYB_FRONTEND_PREBUILT:-0}" == "1" ]]; then
  test -s "$ROOT/resource/static/console/index.html" || { echo 'console assets missing' >&2; exit 1; }
  exit 0
fi
command -v npm >/dev/null || { echo 'Node.js >= 22.18 and npm are required to build the console' >&2; exit 1; }
cd "$ROOT/frontend"
npm ci --no-fund --no-audit
npm run build
