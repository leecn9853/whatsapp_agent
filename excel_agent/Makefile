.PHONY: dev test test-perf

dev:
	uv run uvicorn src.agent_server:app --host 0.0.0.0 --port 8200 --reload

test:
	uv run pytest

test-perf:
	uv run pytest tests/performance -m performance
