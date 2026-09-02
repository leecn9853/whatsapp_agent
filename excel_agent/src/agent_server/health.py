"""GET /health：检查 Postgres 与 Docker sandbox 是否就绪。"""

from __future__ import annotations

import asyncio

import docker
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.agent.backends.docker_sandbox import DEFAULT_CONTAINER_NAME
from src.agent_server.shared import runtime as _runtime

_HEALTH_CHECK_TIMEOUT_SECONDS = 3.0


async def health(_request: Request) -> JSONResponse:
    checks: dict[str, bool] = {}

    try:
        async with asyncio.timeout(_HEALTH_CHECK_TIMEOUT_SECONDS):
            async with _runtime.pool.connection() as conn:
                await conn.execute("SELECT 1")
        checks["postgres"] = True
    except Exception:
        checks["postgres"] = False

    try:
        container = docker.from_env().containers.get(DEFAULT_CONTAINER_NAME)
        checks["sandbox"] = container.status == "running"
    except Exception:
        checks["sandbox"] = False

    ok = all(checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "checks": checks},
        status_code=200 if ok else 503,
    )


routes = [Route("/health", health, methods=["GET"])]
