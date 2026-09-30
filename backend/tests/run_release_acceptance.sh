#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
PYTHON_EXE="$BACKEND_DIR/.venv/bin/python"
BOUNDARY_GUARDS="$BACKEND_DIR/tests/run_unified_boundary_guards.py"
MIGRATION_ROLLBACK_SMOKE="$BACKEND_DIR/tests/run_migration_rollback_smoke.py"
SMOKE_SUITE="$BACKEND_DIR/tests/run_backend_smoke_suite.py"
PERF_PROBE="$BACKEND_DIR/tests/run_perf_probe.py"
PREFLIGHT="$BACKEND_DIR/tests/check_e2e_preflight.py"
FRONTEND_E2E="$FRONTEND_DIR/tests/run_admin_e2e.sh"

BASE_URL="${ADMIN_BASE_URL:-${GO_BASE_URL:-http://127.0.0.1:8081}}"
ADMIN_EMAIL="${ADMIN_EMAIL:-yh@qs.al}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
ADMIN_TOKEN="${ADMIN_TOKEN:-}"
E2E_SPEC="${E2E_SPEC:-business-docqa-flow.spec.ts}"
PERF_PRESET="${PERF_PROBE_PRESET:-release}"
PERF_REQUESTS="${PERF_PROBE_REQUESTS:-20}"
PERF_CONCURRENCY="${PERF_PROBE_CONCURRENCY:-4}"
MAX_ERROR_RATE="${PERF_PROBE_MAX_ERROR_RATE:-0.0}"
MAX_P95_MS="${PERF_PROBE_MAX_P95_MS:-0.0}"
ARTIFACTS_DIR="${RELEASE_ACCEPTANCE_ARTIFACTS_DIR:-$ROOT_DIR/artifacts/release-acceptance}"
BACKEND_INCLUDES=()
SKIP_BOUNDARY_GUARDS=0
SKIP_MIGRATION_ROLLBACK_SMOKE=0
SKIP_BACKEND_SMOKE=0
SKIP_FRONTEND_E2E=0
SKIP_PERF_PROBE=0
FAIL_FAST=0

usage() {
  cat <<'EOF'
Usage: backend/tests/run_release_acceptance.sh [options]

Options:
  --base-url URL              Go gateway base URL. Default: ADMIN_BASE_URL, GO_BASE_URL, or http://127.0.0.1:8081
  --admin-email EMAIL         Admin email. Default: ADMIN_EMAIL or yh@qs.al
  --admin-password PASSWORD   Admin password. Prefer env ADMIN_PASSWORD for local use.
  --admin-token TOKEN         Admin token. Prefer env ADMIN_TOKEN for local use.
  --e2e-spec SPEC             Playwright spec. Default: business-docqa-flow.spec.ts
  --include CASE              Backend smoke case to include. Repeatable; comma-separated values are accepted.
  --perf-preset PRESET        Perf probe preset: readonly or release. Default: release
  --perf-requests N           Request count per perf case. Default: 20
  --perf-concurrency N        Perf probe concurrency. Default: 4
  --max-error-rate RATE       Perf probe max error rate. Default: 0.0
  --max-p95-ms MS             Perf probe max p95 latency. 0 disables. Default: 0.0
  --artifacts-dir DIR         Release artifacts directory.
  --skip-boundary-guards      Skip Python/Go boundary guard suite.
  --skip-migration-rollback-smoke
                              Skip database migration rollback smoke suite.
  --skip-backend-smoke        Skip backend smoke suite.
  --skip-frontend-e2e         Skip frontend business E2E.
  --skip-perf-probe           Skip performance probe.
  --fail-fast                 Stop after first failed step.
  -h, --help                  Show this help.
EOF
}

split_includes() {
  local value="$1"
  local item
  IFS=',' read -ra parts <<< "$value"
  for item in "${parts[@]}"; do
    item="$(echo "$item" | xargs)"
    if [[ -n "$item" ]]; then
      BACKEND_INCLUDES+=("$item")
    fi
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="$2"
      shift 2
      ;;
    --admin-email)
      ADMIN_EMAIL="$2"
      shift 2
      ;;
    --admin-password)
      ADMIN_PASSWORD="$2"
      shift 2
      ;;
    --admin-token)
      ADMIN_TOKEN="$2"
      shift 2
      ;;
    --e2e-spec)
      E2E_SPEC="$2"
      shift 2
      ;;
    --include)
      split_includes "$2"
      shift 2
      ;;
    --perf-preset)
      PERF_PRESET="$2"
      shift 2
      ;;
    --perf-requests)
      PERF_REQUESTS="$2"
      shift 2
      ;;
    --perf-concurrency)
      PERF_CONCURRENCY="$2"
      shift 2
      ;;
    --max-error-rate)
      MAX_ERROR_RATE="$2"
      shift 2
      ;;
    --max-p95-ms)
      MAX_P95_MS="$2"
      shift 2
      ;;
    --artifacts-dir)
      ARTIFACTS_DIR="$2"
      shift 2
      ;;
    --skip-boundary-guards)
      SKIP_BOUNDARY_GUARDS=1
      shift
      ;;
    --skip-migration-rollback-smoke)
      SKIP_MIGRATION_ROLLBACK_SMOKE=1
      shift
      ;;
    --skip-backend-smoke)
      SKIP_BACKEND_SMOKE=1
      shift
      ;;
    --skip-frontend-e2e)
      SKIP_FRONTEND_E2E=1
      shift
      ;;
    --skip-perf-probe)
      SKIP_PERF_PROBE=1
      shift
      ;;
    --fail-fast)
      FAIL_FAST=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "$PYTHON_EXE" ]]; then
  echo "PYTHON_EXE_NOT_FOUND path=$PYTHON_EXE"
  echo "Create the backend virtual environment first: python -m venv backend/.venv && backend/.venv/bin/python -m pip install -r backend/requirements.txt"
  exit 1
fi

health_check() {
  local response
  response="$(curl -fsS --max-time 10 "${BASE_URL%/}/health")" || return 1
  "$PYTHON_EXE" - <<'PY' "$response"
import json
import sys

payload = json.loads(sys.argv[1])
if payload.get("code") != 200:
    raise SystemExit(1)
data = payload.get("data") or {}
neo4j_ok = bool((data.get("neo4j") or {}).get("connected"))
python_ok = bool((data.get("python_backend") or {}).get("connected"))
orchestrator_ok = bool((data.get("orchestrator") or {}).get("connected"))
raise SystemExit(0 if neo4j_ok and python_ok and orchestrator_ok else 1)
PY
}

if ! health_check; then
  echo "GATEWAY_HEALTH_FAIL url=$BASE_URL"
  curl -fsS --max-time 10 "${BASE_URL%/}/health" || true
  echo
  echo "Start Python capability backend and Go gateway first, then rerun release acceptance."
  exit 1
fi

mkdir -p "$ARTIFACTS_DIR"
export ADMIN_BASE_URL="$BASE_URL"
export GO_BASE_URL="$BASE_URL"
export E2E_API_BASE_URL="${E2E_API_BASE_URL:-$BASE_URL}"
export VITE_API_BASE_URL="${VITE_API_BASE_URL:-same-origin}"
export ADMIN_EMAIL
export E2E_SPEC
export PERF_PROBE_PRESET="$PERF_PRESET"
if [[ -n "$ADMIN_PASSWORD" ]]; then export ADMIN_PASSWORD; fi
if [[ -n "$ADMIN_TOKEN" ]]; then export ADMIN_TOKEN; fi

# Preflight gate: the acceptance chain is meaningless unless the test account can
# really authenticate and really sees an active KB (no manual SQL allowed).
if ! "$PYTHON_EXE" "$PREFLIGHT" --base-url "$BASE_URL" --email "$ADMIN_EMAIL"; then
  echo "E2E_PREFLIGHT_BLOCKED base_url=$BASE_URL email=$ADMIN_EMAIL"
  echo "Seed the stack with backend/scripts/seed_e2e_local_stack.py (it self-verifies login), then rerun."
  exit 1
fi

failures=0

run_step() {
  local name="$1"
  shift
  local started
  local exit_code
  started="$(date +%s)"
  echo "ACCEPTANCE_STEP_BEGIN name=$name"
  set +e
  "$@"
  exit_code=$?
  set -e
  local duration=$(( $(date +%s) - started ))
  if [[ "$exit_code" -eq 0 ]]; then
    echo "ACCEPTANCE_STEP_OK name=$name duration_seconds=$duration"
  else
    echo "ACCEPTANCE_STEP_FAIL name=$name exit_code=$exit_code duration_seconds=$duration"
    failures=$((failures + 1))
    if [[ "$FAIL_FAST" -eq 1 ]]; then
      exit "$exit_code"
    fi
  fi
}

if [[ "$SKIP_BOUNDARY_GUARDS" -eq 0 ]]; then
  run_step "unified-boundary-guards" "$PYTHON_EXE" "$BOUNDARY_GUARDS"
fi

if [[ "$SKIP_MIGRATION_ROLLBACK_SMOKE" -eq 0 ]]; then
  run_step "migration-rollback-smoke" "$PYTHON_EXE" "$MIGRATION_ROLLBACK_SMOKE"
fi

if [[ "$SKIP_BACKEND_SMOKE" -eq 0 ]]; then
  smoke_args=("$SMOKE_SUITE" "--base-url" "$BASE_URL")
  for include_name in "${BACKEND_INCLUDES[@]}"; do
    smoke_args+=("--include" "$include_name")
  done
  if [[ "$FAIL_FAST" -eq 1 ]]; then
    smoke_args+=("--fail-fast")
  fi
  run_step "backend-smoke" "$PYTHON_EXE" "${smoke_args[@]}"
fi

if [[ "$SKIP_FRONTEND_E2E" -eq 0 ]]; then
  run_step "frontend-e2e" bash "$FRONTEND_E2E"
fi

if [[ "$SKIP_PERF_PROBE" -eq 0 ]]; then
  run_step "perf-probe" "$PYTHON_EXE" "$PERF_PROBE" \
    --base-url "$BASE_URL" \
    --preset "$PERF_PRESET" \
    --requests "$PERF_REQUESTS" \
    --concurrency "$PERF_CONCURRENCY" \
    --max-error-rate "$MAX_ERROR_RATE" \
    --max-p95-ms "$MAX_P95_MS" \
    --output-json "$ARTIFACTS_DIR/perf-probe.json" \
    --output-markdown "$ARTIFACTS_DIR/perf-probe.md"
fi

echo "ACCEPTANCE_SUMMARY failures=$failures artifacts_dir=$ARTIFACTS_DIR"
if [[ "$failures" -eq 0 ]]; then
  exit 0
fi
exit 1
