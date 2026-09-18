#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VERSION=${VERSION:-dev}
VERSION_CODE=${VERSION_CODE:-1}
COMMIT=${COMMIT:-unknown}
BUILD_DATE=${BUILD_DATE:-unknown}
ARCH=${1:-arm64}
OUT_DIR=${OUT_DIR:-"$ROOT/dist"}

if [[ "$ARCH" != "arm64" ]]; then
  echo "unsupported architecture: $ARCH (only arm64 is currently packaged)" >&2
  exit 2
fi

for command_name in go zip unzip; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "missing build dependency: $command_name" >&2
    exit 1
  }
done

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

cp -R "$ROOT/packaging/magisk/." "$STAGE/"
mkdir -p \
  "$STAGE/bin" \
  "$STAGE/resource/avatars" \
  "$STAGE/resource/db" \
  "$STAGE/resource/qr" \
  "$STAGE/resource/static" \
  "$STAGE/resource/templates" \
  "$OUT_DIR"

# Runtime resource directories may contain account databases and QR images.
# Package only the tracked application assets and leave runtime data empty.
cp -R "$ROOT/resource/static/." "$STAGE/resource/static/"
cp -R "$ROOT/resource/templates/." "$STAGE/resource/templates/"

runtime_text_entries=(
  module.prop
  META-INF/com/google/android/update-binary
  META-INF/com/google/android/updater-script
  customize.sh
  skip_mount
  action.sh
  service.sh
  stop.sh
  uninstall.sh
  config.conf.example
)
for runtime_text_entry in "${runtime_text_entries[@]}"; do
  sed -i 's/\r$//' "$STAGE/$runtime_text_entry"
done

CGO_ENABLED=0 GOOS=android GOARCH=arm64 \
  go build -trimpath -ldflags="-s -w -X yyb_go/internal/version.Version=$VERSION -X yyb_go/internal/version.Commit=$COMMIT -X yyb_go/internal/version.BuildDate=$BUILD_DATE" -o "$STAGE/bin/yyb-go" "$ROOT/cmd/yyb-go"

sed -i "s/^version=.*/version=$VERSION/" "$STAGE/module.prop"
sed -i "s/^versionCode=.*/versionCode=$VERSION_CODE/" "$STAGE/module.prop"
chmod 0755 "$STAGE"/*.sh "$STAGE/bin/yyb-go"
chmod 0755 "$STAGE/META-INF/com/google/android/update-binary"

OUTPUT="$OUT_DIR/yyb-go-magisk-arm64-$VERSION.zip"
rm -f "$OUTPUT"
(cd "$STAGE" && zip -q -X -9 "$OUTPUT" \
  module.prop \
  META-INF/com/google/android/update-binary \
  META-INF/com/google/android/updater-script \
  customize.sh skip_mount action.sh service.sh stop.sh uninstall.sh \
  config.conf.example)
(cd "$STAGE" && zip -q -X -9 -r "$OUTPUT" bin resource)

required_entries=(
  module.prop
  META-INF/com/google/android/update-binary
  META-INF/com/google/android/updater-script
  customize.sh
  skip_mount
  service.sh
  bin/yyb-go
)
archive_entries=$(unzip -Z1 "$OUTPUT")
for required_entry in "${required_entries[@]}"; do
  grep -Fxq "$required_entry" <<< "$archive_entries" || {
    echo "invalid Magisk package: missing $required_entry" >&2
    exit 1
  }
done

for runtime_text_entry in "${runtime_text_entries[@]}"; do
  if unzip -p "$OUTPUT" "$runtime_text_entry" | grep -q $'\r'; then
    echo "invalid Magisk package: $runtime_text_entry must use LF line endings" >&2
    exit 1
  fi
done

if grep -Eq '^resource/(db|qr|avatars)/.+' <<< "$archive_entries"; then
  echo "invalid Magisk package: runtime account data must not be packaged" >&2
  exit 1
fi

unzip -tq "$OUTPUT" >/dev/null
echo "$OUTPUT"
