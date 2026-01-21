#!/usr/bin/env bash
# chmod +x ~/Desktop/benlab.sh
# mv ~/Desktop/benlab.sh ~/.local/bin/benlab
# chmod +x ~/.local/bin/benlab

set -e

# ===== 输出颜色 =====
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() { echo -e "${GREEN}✅ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
error() { echo -e "${RED}❌ $1${NC}"; }

# ===== 项目路径 =====
resolve_project_path() {
  local candidate=""
  local script_dir=""

  if [ -n "${PROJECT_PATH:-}" ]; then
    candidate="$PROJECT_PATH"
  elif [ -n "${BENLAB_HOME:-}" ]; then
    candidate="$BENLAB_HOME"
  fi

  if [ -n "$candidate" ]; then
    if [ ! -d "$candidate" ]; then
      error "PROJECT_PATH 不存在: $candidate"
      exit 1
    fi
    PROJECT_PATH="$(cd -- "$candidate" && pwd -P)"
    return
  fi

  if [ -n "${BASH_SOURCE[0]:-}" ]; then
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
  fi

  if [ -n "$script_dir" ] && {
    [ -f "$script_dir/app.py" ] || [ -f "$script_dir/requirements.txt" ] || [ -d "$script_dir/templates" ];
  }; then
    PROJECT_PATH="$script_dir"
    return
  fi

  if [ -f "$PWD/app.py" ] || [ -f "$PWD/requirements.txt" ] || [ -d "$PWD/templates" ]; then
    PROJECT_PATH="$PWD"
    return
  fi

  error "无法定位项目目录，请设置 PROJECT_PATH 环境变量"
  exit 1
}

resolve_project_path

# ===== 项目配置 =====
PID_FILE="${PID_FILE:-$PROJECT_PATH/flask.pid}"
LOG_FILE="${LOG_FILE:-$PROJECT_PATH/flask.log}"
ACCESS_LOG_FILE="${ACCESS_LOG_FILE:-$PROJECT_PATH/flask-access.log}"
ENV_FILE="${ENV_FILE:-$PROJECT_PATH/.env}"
PORT="${PORT:-5001}"
REQ_FILE="${REQ_FILE:-$PROJECT_PATH/requirements.txt}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
GUNICORN_APP="${GUNICORN_APP:-app:app}"
GUNICORN_BIN="${GUNICORN_BIN:-gunicorn}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
GUNICORN_WORKER_CLASS="${GUNICORN_WORKER_CLASS:-gevent}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_PATH/.benlab_backup}"
BACKUP_ITEMS="${BACKUP_ITEMS:-attachments static .env instance}"
UPDATE_RESTART="${UPDATE_RESTART:-auto}"
GIT_PULL_ARGS="${GIT_PULL_ARGS:---ff-only}"
UPDATE_REPO_URL="${UPDATE_REPO_URL:-https://github.com/Benjaminisgood/Benlab.git}"
UPDATE_BRANCH="${UPDATE_BRANCH:-}"
FORCE_UPDATE="${FORCE_UPDATE:-0}"

if ! cd "$PROJECT_PATH"; then
  error "无法进入项目目录: $PROJECT_PATH"
  exit 1
fi

load_env_file() {
  if [ -z "${ENV_FILE:-}" ]; then
    return
  fi
  if [ -f "$ENV_FILE" ]; then
    info "加载环境变量文件 $ENV_FILE"
    # shellcheck disable=SC1090
    set -a && source "$ENV_FILE" && set +a
  else
    warn "未找到 $ENV_FILE，继续使用当前 shell 环境变量"
  fi
}

load_env_file

# ===== Python 版本检测 =====
if ! command -v python3 &>/dev/null; then
  error "未检测到 Python3，请先安装。"
  exit 1
fi

# ===== 虚拟环境检测 =====
ensure_venv() {
  if [ -d "venv" ]; then
    source venv/bin/activate
  else
    warn "未找到虚拟环境，是否创建? [y/N]"
    read -r ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      python3 -m venv venv
      source venv/bin/activate
      info "已创建并激活 venv"
    else
      warn "未创建虚拟环境，退出"
      exit 1
    fi
  fi
}

# ===== 包检测函数 =====
check_and_install() {
  local package=$1
  if ! python -c "import $package" &>/dev/null; then
    error "缺少依赖: $package"
    echo "是否安装 $package ? [y/N]"
    read -r ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      python -m pip install "$package"
      info "已安装 $package"
    else
      warn "依赖 $package 未安装，无法继续"
      exit 1
    fi
  fi
}

# ===== 依赖检测（只在 start 时调用） =====
ensure_requirements() {
  if [ -f "$REQ_FILE" ]; then
    local check_exit=0
    local check_output=""
    if check_output=$(
      REQ_PATH="$REQ_FILE" python 2>&1 <<'PY'
import sys
from pathlib import Path
import os

try:
    from pkg_resources import (
        DistributionNotFound,
        RequirementParseError,
        VersionConflict,
        require,
    )
except Exception as exc:  # pragma: no cover - defensive guard
    print(f"SETUPTOOLS_ERROR: {exc}")
    sys.exit(2)

req_path = Path(os.environ.get("REQ_PATH", "")).expanduser()
if not req_path.exists():
    print(f"未找到 requirements.txt: {req_path}")
    sys.exit(2)

requirements = []
for raw in req_path.read_text().splitlines():
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        continue
    requirements.append(raw)

if not requirements:
    sys.exit(0)

try:
    require(requirements)
except (DistributionNotFound, VersionConflict) as exc:
    print(exc)
    sys.exit(1)
except RequirementParseError as exc:
    print(f"无法解析 requirements: {exc}")
    sys.exit(2)
except Exception as exc:  # pragma: no cover - generic guard
    print(exc)
    sys.exit(2)
PY
    ); then
      info "requirements.txt 依赖已满足"
      return
    else
      check_exit=$?
      if [ -n "$check_output" ]; then
        warn "$check_output"
      fi
      if [ "$check_exit" -ne 1 ]; then
        warn "无法完整验证 requirements.txt，将尝试重新安装依赖"
      else
        warn "检测到依赖缺失或版本冲突，正在安装 requirements.txt"
      fi
      python -m pip install --upgrade pip
      python -m pip install -r "$REQ_FILE"
      info "requirements.txt 安装完成"
      return
    fi
  else
    warn "未找到 requirements.txt，将逐个检测依赖"
    check_and_install flask
    check_and_install flask_sqlalchemy
    check_and_install flask_login
    check_and_install flask_migrate
    check_and_install pandas
  fi
}

# ===== 计算 Gunicorn workers =====
determine_workers() {
  if [ -n "$GUNICORN_WORKERS" ]; then
    echo "$GUNICORN_WORKERS"
    return
  fi
  python - <<'PY'
import multiprocessing
import platform
import subprocess

def sysctl_int(name: str):
    try:
        out = subprocess.check_output(["sysctl", "-n", name], text=True)
        out = out.strip()
        return int(out) if out else None
    except Exception:
        return None

def linux_mem_total():
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    return int(parts[1]) * 1024  # value is reported in kB
    except Exception:
        return None

cores = multiprocessing.cpu_count()
system = platform.system()
machine = platform.machine()
is_apple_silicon = system == "Darwin" and machine.startswith(("arm", "aarch64"))

# Keep at least one fast core free for macOS on entry-level Apple Silicon boxes.
if is_apple_silicon:
    perf_cores = sysctl_int("hw.perflevel0.physicalcpu")
    usable_cores = perf_cores - 1 if perf_cores and perf_cores > 1 else cores // 2 or 1
else:
    usable_cores = cores - 1 if cores > 2 else cores

usable_cores = max(1, usable_cores)

mem_bytes = None
if system == "Darwin":
    mem_bytes = sysctl_int("hw.memsize")
elif system == "Linux":
    mem_bytes = linux_mem_total()

workers_by_mem = None
if mem_bytes:
    mem_gb = mem_bytes / (1024 ** 3)
    # Roughly budget 1.5 GB per worker to stay safe on 8 GB variants.
    workers_by_mem = max(1, int(mem_gb // 1.5))

workers = usable_cores
if workers_by_mem:
    workers = min(workers, workers_by_mem)

print(max(2, workers))
PY
}

# ===== 端口检测函数 =====
check_port() {
  if lsof -i :$PORT &>/dev/null; then
    error "端口 $PORT 已被占用，请先释放。"
    lsof -i :$PORT
    exit 1
  fi
}

ensure_gunicorn() {
  if ! command -v "$GUNICORN_BIN" &>/dev/null; then
    error "未找到 $GUNICORN_BIN，请确认 gunicorn 已安装 (pip install gunicorn)"
    exit 1
  fi
}

ensure_runtime_dirs() {
  mkdir -p "$PROJECT_PATH/attachments" "$PROJECT_PATH/instance"
}

wait_for_pid_file() {
  local retries=${1:-30}
  local delay=${2:-0.5}

  while [ "$retries" -gt 0 ]; do
    if [ -f "$PID_FILE" ]; then
      local pid
      pid=$(cat "$PID_FILE" 2>/dev/null || true)
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "$pid"
        return 0
      fi
    fi
    sleep "$delay"
    retries=$((retries - 1))
  done

  return 1
}

wait_for_process_exit() {
  local pid=$1
  local retries=${2:-30}
  local delay=${3:-0.5}

  if [ -z "$pid" ]; then
    return 0
  fi

  while [ "$retries" -gt 0 ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep "$delay"
    retries=$((retries - 1))
  done

  return 1
}

wait_for_port_release() {
  local retries=${1:-40}
  local delay=${2:-0.5}

  while [ "$retries" -gt 0 ]; do
    if ! lsof -i :"$PORT" &>/dev/null; then
      return 0
    fi
    sleep "$delay"
    retries=$((retries - 1))
  done

  return 1
}

is_running() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

resolve_backup_dir() {
  if [[ "$BACKUP_DIR" = /* ]]; then
    echo "$BACKUP_DIR"
  else
    echo "$PROJECT_PATH/$BACKUP_DIR"
  fi
}

create_backup() {
  BACKUP_FILE=""
  local -a items=()
  local item

  for item in $BACKUP_ITEMS; do
    if [ -e "$PROJECT_PATH/$item" ]; then
      items+=("$item")
    else
      warn "备份项不存在，跳过: $item"
    fi
  done

  if [ "${#items[@]}" -eq 0 ]; then
    error "没有可备份的数据"
    return 1
  fi

  local backup_dir
  backup_dir=$(resolve_backup_dir)
  mkdir -p "$backup_dir"

  local timestamp
  timestamp=$(date +"%Y%m%d-%H%M%S")
  BACKUP_FILE="$backup_dir/backup-$timestamp.tar.gz"

  if ! tar -czf "$BACKUP_FILE" -C "$PROJECT_PATH" "${items[@]}"; then
    error "备份失败"
    BACKUP_FILE=""
    return 1
  fi

  info "备份完成: $BACKUP_FILE"
  return 0
}

restore_backup() {
  local backup_file="${1:-$BACKUP_FILE}"

  if [ -z "$backup_file" ]; then
    error "未指定备份文件"
    return 1
  fi

  if [ ! -f "$backup_file" ]; then
    error "备份文件不存在: $backup_file"
    return 1
  fi

  if ! tar -xzf "$backup_file" -C "$PROJECT_PATH"; then
    error "还原备份失败"
    return 1
  fi

  info "备份已还原"
}

ensure_git_ready() {
  if ! command -v git &>/dev/null; then
    error "未检测到 git，请先安装"
    return 1
  fi

  if [ -z "${UPDATE_REPO_URL:-}" ]; then
    error "UPDATE_REPO_URL 不能为空"
    return 1
  fi

  if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    error "当前目录不是 git 仓库，无法更新"
    return 1
  fi

  if [ -n "$(git status --porcelain)" ]; then
    if [ "${ALLOW_DIRTY_UPDATE:-}" = "1" ]; then
      warn "检测到未提交变更，但 ALLOW_DIRTY_UPDATE=1，继续更新"
    else
      warn "检测到未提交变更"
      if confirm_force_update; then
        return 0
      fi
      error "检测到未提交变更，请先提交/清理或设置 ALLOW_DIRTY_UPDATE=1"
      return 1
    fi
  fi
}

confirm_force_update() {
  local ans
  echo "是否强制更新并丢弃本地已跟踪修改？[y/N]"
  read -r ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    FORCE_UPDATE=1
    warn "已选择强制更新，将丢弃本地已跟踪修改"
    return 0
  fi
  return 1
}

resolve_update_branch() {
  if [ -n "${UPDATE_BRANCH:-}" ]; then
    echo "$UPDATE_BRANCH"
    return
  fi

  local current_branch=""
  current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  if [ -n "$current_branch" ] && [ "$current_branch" != "HEAD" ]; then
    echo "$current_branch"
    return
  fi

  local origin_head=""
  origin_head=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
  if [ -n "$origin_head" ]; then
    echo "${origin_head#origin/}"
    return
  fi

  local remote_head=""
  if [ -n "${UPDATE_REPO_URL:-}" ]; then
    remote_head=$(git ls-remote --symref "$UPDATE_REPO_URL" HEAD 2>/dev/null | awk '/^ref:/ {print $2}' | sed 's#refs/heads/##')
  fi
  if [ -n "$remote_head" ]; then
    echo "$remote_head"
    return
  fi

  echo "main"
}

git_pull_latest() {
  local branch
  branch=$(resolve_update_branch)
  if [ "${FORCE_UPDATE:-0}" = "1" ]; then
    info "强制更新：从 $UPDATE_REPO_URL 拉取最新代码 (branch=$branch)..."
    if ! git fetch "$UPDATE_REPO_URL" "$branch"; then
      error "git 拉取失败"
      return 1
    fi
    if ! git reset --hard FETCH_HEAD; then
      error "强制更新失败（reset --hard）"
      return 1
    fi
    info "强制更新完成"
  else
    info "从 $UPDATE_REPO_URL 拉取最新代码 (branch=$branch)..."
    if ! git pull $GIT_PULL_ARGS "$UPDATE_REPO_URL" "$branch"; then
      error "git 拉取失败"
      return 1
    fi
    info "代码更新完成"
  fi
}

should_restart_after_update() {
  local was_running=$1
  local mode="${UPDATE_RESTART:-auto}"

  case "$mode" in
    auto|"")
      [ "$was_running" -eq 1 ]
      return
      ;;
    always|yes|true|1)
      return 0
      ;;
    never|no|false|0)
      return 1
      ;;
    *)
      warn "未知 UPDATE_RESTART=$mode，使用 auto"
      [ "$was_running" -eq 1 ]
      return
      ;;
  esac
}

# ===== 功能函数 =====
start() {
  ensure_venv
  ensure_requirements
  ensure_gunicorn
  ensure_runtime_dirs

  if [ -f "$PID_FILE" ]; then
    local existing_pid
    existing_pid=$(cat "$PID_FILE")
    if kill -0 "$existing_pid" 2>/dev/null; then
      warn "Gunicorn 已在运行 (PID=$existing_pid)"
      return
    else
      warn "发现残留 PID 文件，清理中..."
      rm -f "$PID_FILE"
    fi
  fi

  check_port

  local workers
  workers=$(determine_workers)

  info "🚀 使用 Gunicorn 启动 (bind=$BIND_HOST:$PORT, workers=$workers, class=$GUNICORN_WORKER_CLASS)"
  : > "$LOG_FILE"
  : > "$ACCESS_LOG_FILE"

  "$GUNICORN_BIN" \
    "$GUNICORN_APP" \
    --bind "$BIND_HOST:$PORT" \
    --pid "$PID_FILE" \
    --workers "$workers" \
    --worker-class "$GUNICORN_WORKER_CLASS" \
    --timeout "$GUNICORN_TIMEOUT" \
    --daemon \
    --log-file "$LOG_FILE" \
    --access-logfile "$ACCESS_LOG_FILE" \
    --capture-output

  local pid
  if pid=$(wait_for_pid_file 40 0.5); then
    info "Gunicorn 已启动 (PID=$pid)"
    echo "📄 错误日志: $LOG_FILE"
    echo "📄 访问日志: $ACCESS_LOG_FILE"
    if command -v open &>/dev/null; then
      open "http://localhost:$PORT" >/dev/null 2>&1 || true
    fi
  else
    error "未能在限定时间内检测到 PID 文件，请检查日志 $LOG_FILE"
    return 1
  fi
}

stop() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "🛑 停止 Gunicorn (PID=$pid)..."
      kill "$pid" || true
      if wait_for_process_exit "$pid" 60 0.5; then
        if ! wait_for_port_release 60 0.5; then
          warn "Gunicorn 已退出，但端口 $PORT 仍被占用，请手动确认残留进程。"
        fi
        info "已停止"
      else
        warn "Gunicorn (PID=$pid) 未在预期时间退出，可手动检查。"
      fi
    else
      warn "PID 文件存在但 Gunicorn 未在运行"
    fi
    rm -f "$PID_FILE"
  else
    warn "Gunicorn 未运行"
  fi
}

status() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
      info "Gunicorn 正在运行 (PID=$pid)"
      return
    fi
  fi
  warn "Gunicorn 未运行"
}

logs() {
  local files=()
  if [ -f "$LOG_FILE" ]; then
    files+=("$LOG_FILE")
  else
    warn "未找到错误日志文件 $LOG_FILE"
  fi

  if [ -f "$ACCESS_LOG_FILE" ]; then
    files+=("$ACCESS_LOG_FILE")
  else
    warn "未找到访问日志文件 $ACCESS_LOG_FILE"
  fi

  if [ "${#files[@]}" -eq 0 ]; then
    error "没有可供 tail 的日志文件"
    return
  fi

  tail -f "${files[@]}"
}

restart() {
  stop
  start
}

backup() {
  if create_backup; then
    info "备份文件: $BACKUP_FILE"
  else
    return 1
  fi
}

update() {
  if ! ensure_git_ready; then
    return 1
  fi

  local was_running=0
  if is_running; then
    was_running=1
    info "检测到服务正在运行，准备停止..."
    stop
  fi

  if ! create_backup; then
    error "备份失败，已取消更新"
    if [ "$was_running" -eq 1 ]; then
      warn "尝试恢复服务..."
      start
    fi
    return 1
  fi

  if ! git_pull_latest; then
    error "更新失败，已保留当前版本"
    if [ "$was_running" -eq 1 ]; then
      warn "尝试恢复服务..."
      start
    fi
    return 1
  fi

  if ! restore_backup "$BACKUP_FILE"; then
    error "备份还原失败，请检查 $BACKUP_FILE"
    if [ "$was_running" -eq 1 ]; then
      warn "尝试恢复服务..."
      start
    fi
    return 1
  fi

  if should_restart_after_update "$was_running"; then
    start
  else
    info "更新完成"
  fi
}

ip() {
  echo "🌍 当前运行端口: $PORT"
  echo "—— 本地访问: http://localhost:$PORT"
  local lan_ip
  lan_ip=$(ifconfig | grep "inet " | grep -v 127.0.0.1 | awk '{print $2}' | head -n 1)
  if [ -n "$lan_ip" ]; then
    echo "—— 局域网访问: http://$lan_ip:$PORT"
  else
    warn "无法获取局域网IP"
  fi
  echo "—— 公网访问（如配置了frp/nginx反向代理的话）：http://<你的公网IP或域名>:$PORT"
}

# ===== 主入口 =====
case "$1" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  restart) restart ;;
  logs) logs ;;
  backup) backup ;;
  update) update ;;
  ip) ip ;;
  *)
    echo "用法: $0 {start|stop|status|restart|logs|backup|update|ip}"
    exit 1
    ;;
esac
