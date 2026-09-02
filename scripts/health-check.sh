#!/usr/bin/env bash
# 检查各服务是否可达。单独运行：./scripts/health-check.sh
set -uo pipefail

check() {
  local name="$1"
  local cmd="$2"
  if eval "$cmd" > /dev/null 2>&1; then
    echo "  ✓ $name"
    return 0
  else
    echo "  ✗ $name"
    return 1
  fi
}

failed=0
check "third_app     http://127.0.0.1:8800/docs" "curl -sf http://127.0.0.1:8800/docs" || failed=1
check "excel_agent   http://127.0.0.1:8200/health" "curl -sf http://127.0.0.1:8200/health" || failed=1
check "simulator     http://127.0.0.1:3000/status" "curl -sf http://127.0.0.1:3000/status" || failed=1

exit "$failed"
