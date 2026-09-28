#!/usr/bin/env bash
# ==============================================================================
# YYB-Go-Enhanced 多架构 Release 自动化交叉编译与打包脚本
# 支持目标: Linux (amd64, arm64, armv7), Windows (amd64, arm64), macOS (amd64, arm64)
# 产物类型: 独立单二进制文件 + 完整依赖资源归档包 (.tar.gz / .zip) + checksums.txt
# ==============================================================================
set -euo pipefail

# 确定项目根目录与构建基础参数
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VERSION=${VERSION:-$(tr -d '[:space:]' < "$ROOT/VERSION")}
COMMIT=${COMMIT:-$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo "unknown")}
BUILD_DATE=${BUILD_DATE:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}
OUT_DIR=${OUT_DIR:-"$ROOT/dist"}
TARGET_FILTER=${1:-""}

# 兼容探测 Go 编译器命令（兼容 Linux/macOS 的 go 与 Windows Git Bash 的 go.exe）
GO_CMD="go"
if ! command -v "$GO_CMD" >/dev/null 2>&1; then
  if command -v "go.exe" >/dev/null 2>&1; then
    GO_CMD="go.exe"
  else
    echo ">> [错误] 未检测到 Go 编译环境，请先安装 Go 语言工具链" >&2
    exit 1
  fi
fi

# 确保产物输出目录存在
mkdir -p "$OUT_DIR"
OUT_DIR=$(cd "$OUT_DIR" && pwd -P)
rm -f "$OUT_DIR"/yyb-go-* "$OUT_DIR/checksums.txt"

# 跨平台构建目标矩阵清单 (操作系统 架构 标签标识 额外参数/后缀)
targets=(
  "linux amd64 linux-amd64"
  "linux arm64 linux-arm64"
  "linux arm linux-armv7 7"
  "windows amd64 windows-amd64 .exe"
  "windows arm64 windows-arm64 .exe"
  "darwin amd64 darwin-amd64"
  "darwin arm64 darwin-arm64"
)

echo "======================================================================"
echo ">> 开始 YYB-Go Enhanced 多架构 Release 产物构建"
echo ">> 构建版本:   $VERSION"
echo ">> 提交哈希:   $COMMIT"
echo ">> 构建时间:   $BUILD_DATE"
echo ">> 输出目录:   $OUT_DIR"
echo "======================================================================"
echo

for target in "${targets[@]}"; do
  read -r os arch tag extra1 extra2 <<< "$target"
  ext=""
  goarm=""

  # Windows 平台附加可执行后缀
  if [[ "$os" == "windows" ]]; then
    ext=".exe"
  elif [[ "$arch" == "arm" ]]; then
    goarm="$extra1"
  fi

  # 若指定了单个架构过滤，则跳过不匹配的目标
  if [[ -n "$TARGET_FILTER" && "$tag" != "$TARGET_FILTER" ]]; then
    continue
  fi

  echo "==> 正在编译平台架构: $os/$arch ($tag)..."

  # 创建临时工作区用于组装安装归档包
  STAGE=$(mktemp -d)
  bin_name="yyb-go$ext"
  standalone_name="yyb-go-$tag$ext"
  standalone_path="$OUT_DIR/$standalone_name"

  # 设置无 CGO 静态交叉编译环境变量
  env_args=(CGO_ENABLED=0 GOOS="$os" GOARCH="$arch")
  if [[ -n "$goarm" ]]; then
    env_args+=(GOARM="$goarm")
  fi

  # 切换至项目根目录，使用相对路径调用 go build（消除不同操作系统下路径绝对解析差异）
  (
    cd "$ROOT"
    env "${env_args[@]}" "$GO_CMD" build -trimpath \
      -ldflags="-s -w -X yyb_go/internal/version.Version=$VERSION -X yyb_go/internal/version.Commit=$COMMIT -X yyb_go/internal/version.BuildDate=$BUILD_DATE" \
      -o "$standalone_path" \
      ./cmd/yyb-go
  )

  chmod 0755 "$standalone_path"
  cp "$standalone_path" "$STAGE/$bin_name"

  # 组装完整安装包依赖：拷贝 Web 控制台静态资源与 HTML 模板（排除数据库与临时数据）
  mkdir -p "$STAGE/resource/static" "$STAGE/resource/templates"
  if [[ -d "$ROOT/resource/static" ]]; then
    cp -R "$ROOT/resource/static/." "$STAGE/resource/static/"
  fi
  if [[ -d "$ROOT/resource/templates" ]]; then
    cp -R "$ROOT/resource/templates/." "$STAGE/resource/templates/"
  fi
  if [[ -f "$ROOT/README.md" ]]; then
    cp "$ROOT/README.md" "$STAGE/"
  fi
  if [[ -f "$ROOT/.env.example" ]]; then
    cp "$ROOT/.env.example" "$STAGE/"
  fi

  # 打包压缩归档文件（Windows 生成 .zip，Linux/macOS 生成 .tar.gz）
  if [[ "$os" == "windows" ]]; then
    archive_name="yyb-go-v${VERSION}-${tag}.zip"
    archive_path="$OUT_DIR/$archive_name"
    rm -f "$archive_path"
    if command -v zip >/dev/null 2>&1; then
      (cd "$STAGE" && zip -q -X -9 -r "$archive_path" .)
    else
      # 缺失 zip 命令时安全回退为 tar.gz
      tar -czf "$OUT_DIR/yyb-go-v${VERSION}-${tag}.tar.gz" -C "$STAGE" .
      archive_name="yyb-go-v${VERSION}-${tag}.tar.gz"
    fi
  else
    archive_name="yyb-go-v${VERSION}-${tag}.tar.gz"
    archive_path="$OUT_DIR/$archive_name"
    rm -f "$archive_path"
    tar -czf "$archive_path" -C "$STAGE" .
  fi

  # 清理当前目标的临时工作区
  rm -rf "$STAGE"
  echo "    [独立执行程序] $standalone_name"
  echo "    [完整资源归档] $archive_name"
done

echo
echo "==> 正在计算并生成 SHA256 校验和文件 (checksums.txt)..."
(
  cd "$OUT_DIR"
  rm -f checksums.txt
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- * > checksums.txt
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -- * > checksums.txt
  else
    echo ">> [错误] 未检测到 SHA256 校验工具" >&2
    exit 1
  fi
)

echo ">> 构建完成！所有产物已输出至: $OUT_DIR"
