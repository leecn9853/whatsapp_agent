.PHONY: help infra third-app agent simulator health dev

help:
	@echo "常用命令："
	@echo "  make infra      启动 Postgres + Docker sandbox"
	@echo "  make third-app  启动 third_app (:8800)"
	@echo "  make agent      启动 excel_agent (:8200)"
	@echo "  make simulator  启动 whatsapp_simulator (:3000)"
	@echo "  make health     检查各服务是否可达"
	@echo "  make dev        一键并行启动（需各子目录 .env 已配置）"

infra:
	cd excel_agent && docker compose up -d postgres sandbox

third-app:
	cd third_app && uv run python main.py

agent:
	cd excel_agent && make dev

simulator:
	cd whatsapp_simulator && npm start

health:
	@./scripts/health-check.sh

dev:
	@./scripts/dev.sh
