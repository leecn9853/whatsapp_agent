"""GET /v1/memories、GET /v1/memories/{namespace}

走 BaseStore 的公开方法（alist_namespaces/aget）而不是直连底层表名/字段，这样换
存储实现（当前是 Postgres）不需要跟着改这里。
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.agent_server import _runtime
from src.agent_server.utils.security import local_only

MEMORY_KEY = "/AGENTS.md"  # CompositeBackend 把 "/memories/" 前缀路由后剥掉了


@local_only
async def list_memories(_request: Request) -> JSONResponse:
    namespaces = await _runtime.store.alist_namespaces()
    return JSONResponse([".".join(ns) for ns in namespaces])


@local_only
async def get_memory(request: Request) -> JSONResponse:
    namespace = request.path_params["namespace"]
    item = await _runtime.store.aget((namespace.replace(".", "_"),), MEMORY_KEY)
    if item is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"content": item.value["content"]})


routes = [
    Route("/v1/memories", list_memories),
    Route("/v1/memories/{namespace}", get_memory),
]
