#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${S203_PYTHON:-python3}"
exec "$PYTHON" "$HERE/s203_isolated_env.py" "$@"
