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

# simulator：HTTP 可达不够，发消息要求 WhatsApp 为 READY
sim_json=$(curl -sf http://127.0.0.1:3000/status 2>/dev/null || true)
sim_state=$(printf '%s' "$sim_json" | sed -n 's/.*"state":"\([^"]*\)".*/\1/p')
if [[ -z "$sim_json" ]]; then
  echo "  ✗ simulator     http://127.0.0.1:3000/status（不可达）"
  failed=1
elif [[ "$sim_state" == "READY" ]]; then
  echo "  ✓ simulator     state=READY"
else
  echo "  ✗ simulator     state=${sim_state:-unknown}（发消息会 503；扫码: http://localhost:3000/login）"
  failed=1
fi

exit "$failed"
