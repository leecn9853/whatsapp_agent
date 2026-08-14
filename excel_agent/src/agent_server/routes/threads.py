"""GET /v1/threads、GET /v1/threads/{thread_id}/state、DELETE /v1/threads/{thread_id}

取数逻辑：aget_state、checkpointer.adelete_thread、对 checkpoints 表的 COUNT 查询；
"列出所有 thread" 这条 SQL 走 _runtime.pool（Postgres 连接池）。
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.agent_server import _runtime
from src.agent_server.utils.security import local_only


@local_only
async def list_threads(_request: Request) -> JSONResponse:
    async with _runtime.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT thread_id, COUNT(*) AS n FROM checkpoints GROUP BY thread_id ORDER BY n DESC"
            )
            rows = await cur.fetchall()
    return JSONResponse([{"thread_id": thread_id, "checkpoint_count": n} for thread_id, n in rows])


@local_only
async def get_state(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    snapshot = await _runtime.agent.aget_state({"configurable": {"thread_id": thread_id}})
    messages = snapshot.values.get("messages", [])
    return JSONResponse(
        {
            "messages": [
                {"role": type(m).__name__, "content": str(getattr(m, "content", m))}
                for m in messages
            ]
        }
    )


@local_only
async def delete_thread(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    async with _runtime.lock_for(thread_id):
        await _runtime.agent.checkpointer.adelete_thread(thread_id)
    return JSONResponse({"ok": True})


routes = [
    Route("/v1/threads", list_threads),
    Route("/v1/threads/{thread_id}/state", get_state),
    Route("/v1/threads/{thread_id}", delete_thread, methods=["DELETE"]),
]
