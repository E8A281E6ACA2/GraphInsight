#!/usr/bin/env bash
# Release Gate：enforce + soft 双模式回滚验收矩阵执行器。
#
# 为什么需要它：只看 enforce 一腿会把可回滚下限判高。45db134 在 enforce 下 15/15 全过，
# 但在 soft（RBAC_AUTHZ_MODE=local_jwt_soft + RBAC_ENFORCE_BUSINESS_API=false）下，
# 伪造入站身份头的探针返回 200 —— 真实跨 KB 泄漏。因此一条版本必须两腿都过才算完成
# 回滚验收，而且两腿共用同一套断言：授权模式不是安全不变量的豁免理由。
#
# 网关"怎么起"由调用方给模板命令（本地隔离栈用一次性容器，CI 用宿主进程）；
# 执行器只负责编排、健康等待、身份链记录、断言汇总与腿级清理。
#
# 模板占位符：{version} {mode} {port} {authz_env} {base_url} {gw_log}
#   enforce -> RBAC_AUTHZ_MODE=go_db RBAC_ENFORCE_BUSINESS_API=true
#   soft    -> RBAC_AUTHZ_MODE=local_jwt_soft RBAC_ENFORCE_BUSINESS_API=false
#
# 夹具（kb-b + 仅 viewer@kb-b 的低权限用户）由 run_rollback_drill.py --phase setup 建立，
# 只能在发布版本 + enforce 腿上跑一次；本执行器不隐式跑 setup，避免把夹具写进旧版本。
#
# 退出码：0=全部腿通过；1=存在失败腿；2=前置缺失或参数非法。
set -uo pipefail

VERSIONS="HEAD"
MODES="enforce,soft"
STATE_FILE=""
START_CMD=""
STOP_CMD=""
IDENTITY_CMD=""
BASE_URL_TEMPLATE="http://127.0.0.1:{port}"
PORT_BASE=18091
READY_TIMEOUT=90
PROBE_PYTHON="python3"
ADMIN_EMAIL="${ADMIN_EMAIL:-e2e-admin@local.test}"
ADMIN_PASSWORD_FILE=""
LOG_DIR=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRILL="$SCRIPT_DIR/run_rollback_drill.py"

CURRENT_STOP=""

die() {
  echo "MATRIX_PREREQ_INVALID $*"
  exit 2
}

cleanup_current_leg() {
  if [[ -n "$CURRENT_STOP" ]]; then
    local stop="$CURRENT_STOP"
    CURRENT_STOP=""
    ( eval "$stop" ) >/dev/null 2>&1 || true
  fi
}
trap cleanup_current_leg EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --versions) VERSIONS="$2"; shift 2 ;;
    --modes) MODES="$2"; shift 2 ;;
    --state-file) STATE_FILE="$2"; shift 2 ;;
    --start-cmd) START_CMD="$2"; shift 2 ;;
    --stop-cmd) STOP_CMD="$2"; shift 2 ;;
    --identity-cmd) IDENTITY_CMD="$2"; shift 2 ;;
    --base-url) BASE_URL_TEMPLATE="$2"; shift 2 ;;
    --port-base) PORT_BASE="$2"; shift 2 ;;
    --ready-timeout) READY_TIMEOUT="$2"; shift 2 ;;
    --python) PROBE_PYTHON="$2"; shift 2 ;;
    --admin-email) ADMIN_EMAIL="$2"; shift 2 ;;
    --admin-password-file) ADMIN_PASSWORD_FILE="$2"; shift 2 ;;
    --log-dir) LOG_DIR="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -f "$DRILL" ]] || die "drill probe missing: $DRILL"
[[ -n "$STATE_FILE" && -n "$START_CMD" && -n "$STOP_CMD" ]] || die "--state-file/--start-cmd/--stop-cmd are required"
case "$MODES" in
  *enforce*soft*|*soft*enforce*) ;;
  *) die "both enforce and soft legs are required (modes=$MODES): 缺任一腿视为未演练" ;;
esac
if [[ -z "$ADMIN_PASSWORD_FILE" && -z "${ADMIN_PASSWORD:-}" ]]; then
  die "admin password required via ADMIN_PASSWORD env or --admin-password-file"
fi
[[ $READY_TIMEOUT -gt 0 ]] || die "--ready-timeout must be > 0"

if [[ -z "$LOG_DIR" ]]; then
  LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gi-rollback-matrix.XXXXXX")"
fi
mkdir -p "$LOG_DIR" || die "cannot create log dir: $LOG_DIR"

render() {
  local template="$1" version="$2" mode="$3" port="$4" authz_env="$5" gw_log="$6"
  # 同一条 local 语句里右侧引用左侧变量在 bash 5.3 会在赋值前展开，set -u 下直接报 unbound。
  local out="$template"
  out="${out//\{version\}/$version}"
  out="${out//\{mode\}/$mode}"
  out="${out//\{port\}/$port}"
  out="${out//\{authz_env\}/$authz_env}"
  out="${out//\{gw_log\}/$gw_log}"
  out="${out//\{base_url\}/http://127.0.0.1:$port}"
  printf '%s' "$out"
}

authz_env_for() {
  case "$1" in
    enforce) printf 'RBAC_AUTHZ_MODE=go_db RBAC_ENFORCE_BUSINESS_API=true' ;;
    soft) printf 'RBAC_AUTHZ_MODE=local_jwt_soft RBAC_ENFORCE_BUSINESS_API=false' ;;
    *) die "unsupported mode: $1" ;;
  esac
}

# 模板必须先自检：占位符写错或 render 退化成空串时，宁可立刻失败，也不能让 eval 空命令
# 返回 0 再把失败伪装成 not_ready（这类静默走偏会让整条矩阵看起来"跑过"）。
SUPPORTED_PLACEHOLDERS='{version} {mode} {port} {authz_env} {base_url} {gw_log}'

verify_render_contract() {
  local name="$1" template="$2" rendered="$3" token unknown=()
  [[ -n "$rendered" ]] || die "$name renders empty (template=$template)"
  # `${VAR}` 是调用方自己的 shell 展开（命令最终走 eval），不算执行器占位符；
  # 只有裸 {token} 才必须是下面支持的集合，否则就是拼错、会静默留到运行期。
  for token in $(printf '%s\n' "$template" \
    | sed -E 's/\$\{[A-Za-z_][A-Za-z0-9_]*\}//g' \
    | grep -oE '\{[A-Za-z_][A-Za-z0-9_]*\}' | sort -u); do
    case " $SUPPORTED_PLACEHOLDERS " in
      *" $token "*) ;;
      *) unknown+=("$token") ;;
    esac
  done
  if [[ ${#unknown[@]} -gt 0 ]]; then
    die "$name uses unknown placeholders: ${unknown[*]} (supported: $SUPPORTED_PLACEHOLDERS)"
  fi
  printf 'RENDER_CONTRACT_OK name=%s\n' "$name"
}

wait_ready() {
  local base_url="$1" attempt
  for attempt in $(seq 1 "$READY_TIMEOUT"); do
    if curl -fsS --max-time 3 "$base_url/health" >/dev/null 2>&1; then
      printf 'READY attempt=%s\n' "$attempt"
      return 0
    fi
    sleep 1
  done
  return 1
}

IFS=',' read -r -a version_list <<< "$VERSIONS"
IFS=',' read -r -a mode_list <<< "$MODES"

# 版本令牌要和构建步骤用同一套去空格规则，否则 "HEAD, 78b1f28" 会让 leg 找不到 bin/api。
normalized=()
for token in "${version_list[@]}"; do
  trimmed="$(printf '%s' "$token" | tr -d '[:space:]')"
  [[ -n "$trimmed" ]] || die "empty version token in --versions=$VERSIONS"
  normalized+=("$trimmed")
done
version_list=("${normalized[@]}")

password_args=()
[[ -z "$ADMIN_PASSWORD_FILE" ]] || password_args=(--admin-password-file "$ADMIN_PASSWORD_FILE")

legs_total=0
legs_passed=0
legs_failed=0
leg_index=0
leg_lines=()

echo "MATRIX_BEGIN versions=${version_list[*]} modes=${mode_list[*]} log_dir=$LOG_DIR"

verify_render_contract base_url "$BASE_URL_TEMPLATE" \
  "$(render "$BASE_URL_TEMPLATE" "${version_list[0]}" "${mode_list[0]}" "$PORT_BASE" "" "")"
verify_render_contract start_cmd "$START_CMD" \
  "$(render "$START_CMD" "${version_list[0]}" "${mode_list[0]}" "$PORT_BASE" "$(authz_env_for "${mode_list[0]}")" "$LOG_DIR/probe.gateway.log")"
verify_render_contract stop_cmd "$STOP_CMD" \
  "$(render "$STOP_CMD" "${version_list[0]}" "${mode_list[0]}" "$PORT_BASE" "" "")"
if [[ -n "$IDENTITY_CMD" ]]; then
  verify_render_contract identity_cmd "$IDENTITY_CMD" \
    "$(render "$IDENTITY_CMD" "${version_list[0]}" "${mode_list[0]}" "$PORT_BASE" "" "")"
fi

for version in "${version_list[@]}"; do
  for mode in "${mode_list[@]}"; do
    port=$((PORT_BASE + leg_index))
    leg_index=$((leg_index + 1))
    legs_total=$((legs_total + 1))
    base_url="$(render "$BASE_URL_TEMPLATE" "$version" "$mode" "$port" "" "")"
    authz_env="$(authz_env_for "$mode")"
    probe_log="$LOG_DIR/matrix_${version}_${mode}_${port}.probe.log"
    gw_log="$LOG_DIR/matrix_${version}_${mode}_${port}.gateway.log"

    start_cmd="$(render "$START_CMD" "$version" "$mode" "$port" "$authz_env" "$gw_log")"
    CURRENT_STOP="$(render "$STOP_CMD" "$version" "$mode" "$port" "$authz_env" "$gw_log")"

    echo "LEG_BEGIN version=$version mode=$mode port=$port base_url=$base_url authz_env=[$authz_env]"
    if [[ -n "$IDENTITY_CMD" ]]; then
      # 身份链必须是实跑输出（提交号 + 二进制 sha256），不是回显命令模板。
      identity_out="$(eval "$(render "$IDENTITY_CMD" "$version" "$mode" "$port" "$authz_env" "$gw_log")" 2>&1 | tr '\n' ' ')"
      echo "LEG_IDENTITY version=$version mode=$mode ${identity_out:-identity_command_empty}"
    fi
    # 子 shell 隔离：start-cmd 引用到未导出变量时，set -u 只终止子 shell，本执行器
    # 仍能给出 start_failed 的腿级结论，而不是整个矩阵无摘要中断。
    if ! ( eval "$start_cmd" ); then
      echo "LEG_RESULT version=$version mode=$mode result=start_failed gw_log=$gw_log"
      legs_failed=$((legs_failed + 1))
      leg_lines+=("$version/$mode=start_failed")
      cleanup_current_leg
      continue
    fi

    if ! wait_ready "$base_url"; then
      echo "LEG_RESULT version=$version mode=$mode result=not_ready gw_log=$gw_log"
      legs_failed=$((legs_failed + 1))
      leg_lines+=("$version/$mode=not_ready")
      cleanup_current_leg
      continue
    fi

    probe_args=(--phase probe --version-label "$version" --authz-mode "$mode"
                --base-url "$base_url" --state-file "$STATE_FILE" --admin-email "$ADMIN_EMAIL")
    if [[ ${#password_args[@]} -gt 0 ]]; then
      probe_args+=("${password_args[@]}")
    fi
    # ADMIN_PASSWORD 只经环境变量传给探针，不出现在命令行里。
    $PROBE_PYTHON "$DRILL" "${probe_args[@]}" >"$probe_log" 2>&1
    probe_exit=$?
    summary="$(grep 'ROLLBACK_DRILL_SUMMARY' "$probe_log" | tail -n 1)"
    echo "LEG_RESULT version=$version mode=$mode exit=$probe_exit ${summary:-summary_missing} probe_log=$probe_log gw_log=$gw_log"
    if [[ $probe_exit -eq 0 ]]; then
      legs_passed=$((legs_passed + 1))
      leg_lines+=("$version/$mode=pass")
    else
      legs_failed=$((legs_failed + 1))
      leg_lines+=("$version/$mode=fail")
    fi
    cleanup_current_leg
  done
done

echo "MATRIX_ARTIFACT_LOGS dir=$LOG_DIR"
echo "ROLLBACK_MATRIX_SUMMARY versions=${#version_list[@]} modes=${#mode_list[@]} \
legs=$legs_total legs_passed=$legs_passed legs_failed=$legs_failed detail=${leg_lines[*]}"
[[ $legs_failed -eq 0 ]] || exit 1
exit 0
