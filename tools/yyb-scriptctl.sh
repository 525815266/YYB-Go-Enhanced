#!/usr/bin/env bash
set -Eeuo pipefail

PROGRAM_NAME="yyb-scriptctl"
REPO_SLUG="${YYB_REPO_SLUG:-525815266/YYB-Go-Enhanced}"
REPO_BRANCH="${YYB_REPO_BRANCH:-main}"
REPO_URL="${YYB_REPO_URL:-https://github.com/${REPO_SLUG}.git}"
RAW_BASE="${YYB_RAW_BASE:-https://raw.githubusercontent.com/${REPO_SLUG}/${REPO_BRANCH}/scripts}"
SCRIPT_DIR="${YYB_SCRIPT_DIR:-/ql/data/scripts/525815266_YYB-Go-Enhanced_main/scripts}"
TASK_PREFIX="${YYB_TASK_PREFIX:-525815266_YYB-Go-Enhanced_main/scripts}"

log() {
  printf '[%s] %s\n' "$PROGRAM_NAME" "$*"
}

die() {
  printf '[%s] 错误：%s\n' "$PROGRAM_NAME" "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法：
  yyb-scriptctl.sh sync [--dest 目录]
  yyb-scriptctl.sh pull <脚本名> [--dest 目录]
  yyb-scriptctl.sh install <脚本名> [--cron "表达式"] [--name "任务名"]
                                    [--dest 目录] [--task-prefix 路径]

动作：
  sync     同步仓库 scripts/ 目录，不创建青龙任务
  pull     只下载一个脚本，不创建青龙任务
  install  下载一个脚本，并按完整命令创建或更新青龙任务

环境变量：
  YYB_REPO_SLUG、YYB_REPO_BRANCH、YYB_REPO_URL、YYB_RAW_BASE
  YYB_SCRIPT_DIR、YYB_TASK_PREFIX、QL_DIR

说明：
  install 只读取脚本前 40 行内锚定的 "# name:" / "# cron:" 元数据。
  脚本没有 cron 元数据时必须显式传 --cron，工具不会套用随机时间。
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

validate_filename() {
  local filename="$1"
  [[ -n "$filename" ]] || die "脚本名不能为空"
  [[ "$filename" != *'/'* && "$filename" != *'\'* && "$filename" != *'..'* ]] ||
    die "只允许传入 scripts/ 下的文件名，不能包含路径"
  case "$filename" in
    *.py|*.js|*.sh) ;;
    *) die "仅支持 .py、.js、.sh 脚本" ;;
  esac
}

encode_uri_component() {
  local python_bin
  python_bin="$(find_python)"
  "$python_bin" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import json, urllib.parse' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  die "缺少可用的 Python 3"
}

download_one() {
  local filename="$1"
  local destination_dir="$2"
  local encoded_filename url tmp_file

  validate_filename "$filename"
  require_command curl
  encoded_filename="$(encode_uri_component "$filename")"
  url="${RAW_BASE%/}/${encoded_filename}"
  mkdir -p "$destination_dir"
  tmp_file="$(mktemp "${destination_dir%/}/.${filename}.download.XXXXXX")"

  if ! curl -fL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 120 \
    --silent --show-error "$url" -o "$tmp_file"; then
    rm -f "$tmp_file"
    die "下载失败，原文件未被覆盖：$url"
  fi
  if [[ ! -s "$tmp_file" ]]; then
    rm -f "$tmp_file"
    die "下载结果为空，原文件未被覆盖：$url"
  fi

  chmod 0644 "$tmp_file"
  mv -f "$tmp_file" "${destination_dir%/}/${filename}"
  log "已拉取：${destination_dir%/}/${filename}"
}

sync_repo_scripts() (
  local destination_dir="$1"
  local temp_root temp_repo

  require_command git
  temp_root="$(mktemp -d)"
  temp_repo="$temp_root/repo"
  trap 'rm -rf "$temp_root"' EXIT

  log "正在同步 ${REPO_SLUG}@${REPO_BRANCH} 的 scripts/ 目录"
  git clone --quiet --depth=1 --filter=blob:none --sparse \
    --branch "$REPO_BRANCH" "$REPO_URL" "$temp_repo" || die "仓库拉取失败"
  git -C "$temp_repo" sparse-checkout set scripts >/dev/null
  [[ -d "$temp_repo/scripts" ]] || die "仓库中不存在 scripts/ 目录"

  mkdir -p "$destination_dir"
  cp -a "$temp_repo/scripts/." "$destination_dir/"
  log "同步完成：$destination_dir（未创建或修改青龙任务）"
)

read_metadata() {
  local key="$1"
  local file="$2"
  sed -n '1,40p' "$file" |
    sed -nE "s/^[[:space:]]*(#|\/\/)[[:space:]]*${key}[[:space:]]*:[[:space:]]*(.*[^[:space:]])[[:space:]]*$/\2/p" |
    head -n 1
}

load_qinglong_api() {
  local ql_root="${QL_DIR:-/ql}"
  [[ -r "$ql_root/shell/share.sh" && -r "$ql_root/shell/api.sh" ]] ||
    die "未找到青龙 API 脚本，请在青龙容器内执行 install，或设置 QL_DIR"
  # shellcheck source=/dev/null
  . "$ql_root/shell/share.sh"
  # shellcheck source=/dev/null
  . "$ql_root/shell/api.sh"
  load_ql_envs
  get_token
  [[ -n "${__ql_token__:-}" ]] || die "无法取得青龙 API Token"
}

qinglong_upsert_task() {
  local task_name="$1"
  local command="$2"
  local schedule="$3"
  local response existing_id payload result result_fields code message method python_bin

  require_command curl
  python_bin="$(find_python)"
  load_qinglong_api

  response="$(curl -fsS --noproxy '*' \
    "http://localhost:${ql_port}/open/crons?t=$(date +%s)" \
    -H "Authorization: Bearer ${__ql_token__}")" || die "读取青龙任务列表失败"
  existing_id="$(printf '%s' "$response" | "$python_bin" -c '
import json, sys
command = sys.argv[1]
data = json.load(sys.stdin)
for _ in range(3):
    if isinstance(data, list):
        break
    if not isinstance(data, dict):
        data = []
        break
    if isinstance(data.get("data"), (list, dict)):
        data = data["data"]
    elif isinstance(data.get("list"), list):
        data = data["list"]
    else:
        data = []
        break
for item in data if isinstance(data, list) else []:
    if item.get("command") == command:
        print(item.get("id") or item.get("_id") or "")
        break
' "$command")" || die "无法解析青龙任务列表"

  if [[ -n "$existing_id" ]]; then
    method='PUT'
    payload="$("$python_bin" -c \
      'import json,sys; print(json.dumps({"id":sys.argv[1],"name":sys.argv[2],"command":sys.argv[3],"schedule":sys.argv[4]}))' \
      "$existing_id" "$task_name" "$command" "$schedule")"
  else
    method='POST'
    payload="$("$python_bin" -c \
      'import json,sys; print(json.dumps({"name":sys.argv[1],"command":sys.argv[2],"schedule":sys.argv[3],"sub_id":None}))' \
      "$task_name" "$command" "$schedule")"
  fi

  result="$(curl -sS --noproxy '*' -X "$method" \
    "http://localhost:${ql_port}/open/crons?t=$(date +%s)" \
    -H "Authorization: Bearer ${__ql_token__}" \
    -H 'Content-Type: application/json;charset=UTF-8' \
    --data-raw "$payload")" || die "写入青龙任务失败"
  result_fields="$(printf '%s' "$result" | "$python_bin" -c '
import json, sys
data = json.load(sys.stdin)
print(data.get("code", ""))
print(data.get("message") or data.get("msg") or "")
')" || die "无法解析青龙任务写入结果"
  code="${result_fields%%$'\n'*}"
  message="${result_fields#*$'\n'}"
  [[ "$code" == '200' || "$code" == '0' ]] || die "青龙任务接口拒绝操作：${message:-未知错误}"

  if [[ -n "$existing_id" ]]; then
    log "已更新任务：$task_name（$command）"
  else
    log "已创建任务：$task_name（$command）"
  fi
}

action="${1:-}"
[[ -n "$action" ]] || {
  usage
  exit 1
}
shift

case "$action" in
  sync|repo)
    destination="$SCRIPT_DIR"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --dest) [[ $# -ge 2 ]] || die "--dest 缺少参数"; destination="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1" ;;
      esac
    done
    sync_repo_scripts "$destination"
    ;;
  pull)
    [[ $# -ge 1 ]] || die "pull 需要脚本名"
    filename="$1"
    shift
    destination="$SCRIPT_DIR"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --dest) [[ $# -ge 2 ]] || die "--dest 缺少参数"; destination="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1" ;;
      esac
    done
    download_one "$filename" "$destination"
    ;;
  install)
    [[ $# -ge 1 ]] || die "install 需要脚本名"
    filename="$1"
    shift
    destination="$SCRIPT_DIR"
    task_prefix="$TASK_PREFIX"
    schedule=''
    task_name=''
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --cron) [[ $# -ge 2 ]] || die "--cron 缺少参数"; schedule="$2"; shift 2 ;;
        --name) [[ $# -ge 2 ]] || die "--name 缺少参数"; task_name="$2"; shift 2 ;;
        --dest) [[ $# -ge 2 ]] || die "--dest 缺少参数"; destination="$2"; shift 2 ;;
        --task-prefix) [[ $# -ge 2 ]] || die "--task-prefix 缺少参数"; task_prefix="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1" ;;
      esac
    done

    download_one "$filename" "$destination"
    script_path="${destination%/}/${filename}"
    [[ -n "$task_name" ]] || task_name="$(read_metadata name "$script_path")"
    [[ -n "$task_name" ]] || task_name="${filename%.*}"
    [[ -n "$schedule" ]] || schedule="$(read_metadata cron "$script_path")"
    [[ -n "$schedule" ]] ||
      die "脚本没有 cron 元数据，请使用 --cron 明确指定执行时间；脚本已下载但未创建任务"
    command="task ${task_prefix%/}/${filename}"
    qinglong_upsert_task "$task_name" "$command" "$schedule"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    die "未知动作：$action（支持 sync、pull、install）"
    ;;
esac
