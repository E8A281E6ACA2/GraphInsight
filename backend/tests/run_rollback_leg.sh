#!/usr/bin/env bash
# 回滚矩阵的单条腿执行器：把一个指定版本的 Go 网关二进制拉到独立端口上起停，
# 并只改掉这一腿的授权模式环境变量。run_rollback_matrix.sh 通过 --start-cmd /
# --stop-cmd 模板调用它，因此本脚本不做编排、不做断言，只负责"起一条腿、停一条腿"。
#
# 必填环境变量：
#   DRILL_BUILD_ROOT  版本二进制根目录，腿二进制路径为 $DRILL_BUILD_ROOT/<version>/go-backend/bin/$DRILL_BIN_NAME
#   DRILL_PID_DIR     腿 pid 文件目录（必须仓库外，随 runner temp / 临时目录销毁）
# 可选环境变量（缺省沿用统一开发栈口径）：
#   DRILL_BIN_NAME, PYTHON_BACKEND_BASE_URL, ADMIN_DATABASE_URL,
#   NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, NEO4J_DATABASE
set -uo pipefail

usage() {
  echo "Usage: $0 start <version> <mode> <port> <gw_log> [KEY=VAL ...] | stop <version> <mode>" >&2
  exit 2
}

fail_prereq() {
  echo "LEG_PREREQ_INVALID $*" >&2
  exit 2
}

command_name="${1:-}"
[[ -n "$command_name" ]] || usage
shift

BIN_NAME="${DRILL_BIN_NAME:-api}"

case "$command_name" in
  start)
    [[ $# -ge 4 ]] || usage
    version="$1"; mode="$2"; port="$3"; gw_log="$4"; shift 4
    authz_env="$*"

    [[ -n "${DRILL_BUILD_ROOT:-}" ]] || fail_prereq "DRILL_BUILD_ROOT is required"
    [[ -n "${DRILL_PID_DIR:-}" ]] || fail_prereq "DRILL_PID_DIR is required"
    [[ "$port" =~ ^[0-9]+$ ]] || fail_prereq "port must be numeric: $port"
    # 授权模式只允许执行器给出的两种取值，其它一律拒绝，避免起出一条"没有模式"的腿。
    case "$authz_env" in
      *"RBAC_AUTHZ_MODE=go_db RBAC_ENFORCE_BUSINESS_API=true"*) mode_check="enforce" ;;
      *"RBAC_AUTHZ_MODE=local_jwt_soft RBAC_ENFORCE_BUSINESS_API=false"*) mode_check="soft" ;;
      *) fail_prereq "unsupported authz env for mode=$mode: [$authz_env]" ;;
    esac
    [[ "$mode_check" == "$mode" ]] || fail_prereq "mode/authz env mismatch: declared=$mode actual=$mode_check"

    bin="$DRILL_BUILD_ROOT/$version/go-backend/bin/$BIN_NAME"
    [[ -f "$bin" ]] || { echo "START_FAIL missing_binary $bin"; exit 3; }
    mkdir -p "$DRILL_PID_DIR"
    pid_file="$DRILL_PID_DIR/leg-$version-$mode.pid"

    : > "$gw_log"
    env \
      API_HOST="${DRILL_GO_HOST:-127.0.0.1}" \
      API_PORT="$port" \
      PYTHON_BACKEND_BASE_URL="${PYTHON_BACKEND_BASE_URL:-http://127.0.0.1:8001}" \
      ADMIN_DATABASE_URL="${ADMIN_DATABASE_URL:-}" \
      NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}" \
      NEO4J_USER="${NEO4J_USER:-neo4j}" \
      NEO4J_PASSWORD="${NEO4J_PASSWORD:-change-this-password}" \
      NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}" \
      NEO4J_CONFIG_SOURCE=auto \
      $authz_env \
      "$bin" >"$gw_log" 2>&1 < /dev/null &
    pid=$!
    echo "$pid" > "$pid_file"
    echo "START_OK version=$version mode=$mode port=$port pid=$pid overrides=[$authz_env]"
    ;;
  stop)
    [[ $# -ge 2 ]] || usage
    version="$1"; mode="$2"
    [[ -n "${DRILL_PID_DIR:-}" ]] || fail_prereq "DRILL_PID_DIR is required"
    pid_file="$DRILL_PID_DIR/leg-$version-$mode.pid"
    if [[ ! -f "$pid_file" ]]; then
      echo "STOP_SKIP version=$version mode=$mode pidfile_missing"
      exit 0
    fi
    pid="$(tr -d '[:space:]' < "$pid_file")"
    [[ "$pid" =~ ^[0-9]+$ ]] || fail_prereq "pid file is not numeric: $pid_file"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
      done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
    echo "STOP_OK version=$version mode=$mode pid=$pid"
    ;;
  *)
    usage
    ;;
esac
