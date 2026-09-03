#!/usr/bin/env bash
# 本地开发：并行拉起 third_app、excel_agent、whatsapp_simulator。
# 需先在本机各子目录配置好 .env；Docker 需已安装。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "==> 启动 Postgres + sandbox"
(cd "$ROOT/excel_agent" && docker compose up -d postgres sandbox)

echo "==> 启动 third_app (:8800)"
(cd "$ROOT/third_app" && uv run python main.py) &
PIDS+=($!)

echo "==> 启动 excel_agent (:8200)"
(cd "$ROOT/excel_agent" && make dev) &
PIDS+=($!)

echo "==> 启动 whatsapp_simulator (:3000)"
# 避免上次异常退出后旧 Node 占着端口，导致新代码不生效（/login 404 等）
if command -v lsof >/dev/null 2>&1; then
  stale=$(lsof -tiTCP:3000 -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "${stale}" ]]; then
    echo "    检测到 3000 被占用 (PID ${stale})，先结束旧进程"
    kill ${stale} 2>/dev/null || true
    sleep 1
  fi
fi
(cd "$ROOT/whatsapp_simulator" && npm start) &
PIDS+=($!)

echo "等待服务就绪..."
sleep 8

echo ""
echo "==> 健康检查"
"$ROOT/scripts/health-check.sh" || true

echo ""
echo "各服务已在后台运行，Ctrl+C 停止本脚本（会一并结束子进程）。"
wait
