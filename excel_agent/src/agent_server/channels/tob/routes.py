"""toB 对外 API：POST /v1/tob/threads/{external_id}/runs（SSE）、GET .../state、
DELETE /v1/tob/threads/{external_id}、GET /v1/tob/threads。

调用方只知道自己起的 `external_id`，不需要知道内部的 `tob:` 前缀方案；这里统一转成
`thread_ids.tob_thread_id(external_id)` 再传给 engine/checkpointer。`GET /v1/tob/threads`
只列出 `tob:` 前缀的记录，不能让调用方看到 WhatsApp 那边的 thread。

目前 toB 没有真正的外部调用方（都是内部开发/测试在用），鉴权先靠 local_only 兜底，
以后如果要给真正的外部 toB 调用方开放，再在这基础上加真正的鉴权。

跑一次 agent 的重试/续跑逻辑在 shared/engine.py，这里只负责把工具调用事件转成 SSE
帧、把最终结果/异常转成 done/error 帧——和 channels/whatsapp/processor.py 共用同一个
run_agent_turn，呈现方式不同而已。
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from src.context import ContextSchema
from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.engine import RunFailed, RunResult, run_agent_turn
from src.agent_server.shared.thread_ids import tob_thread_id
from src.agent_server.shared.security import local_only


def _sse_frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _sse(thread_id: str, message: str):
    run_id = await _runtime.runs_store.acreate_run(thread_id)
    context = ContextSchema(caller="tob", user_id=thread_id, run_id=run_id)

    async with _runtime.lock_for(thread_id):
        result: RunResult | None = None
        try:
            async for event in run_agent_turn(thread_id, message, context, run_id=run_id):
                if isinstance(event, RunResult):
                    result = event
                else:
                    yield _sse_frame({"event": "tool_call", "name": event})
        except RunFailed as fail:
            files = [str(p.resolve()) for p in fail.files]
            yield _sse_frame(
                {"event": "error", "message": str(fail.cause) or type(fail.cause).__name__, "files": files}
            )
            return

        assert result is not None
        files = [str(p.resolve()) for p in result.files]
        yield _sse_frame({"event": "done", "reply": result.reply, "files": files})


@local_only
async def create_run(request: Request):
    external_id = request.path_params["external_id"]
    body = await request.json()
    message = body.get("message")
    if not message:
        return JSONResponse({"error": "message 不能为空"}, status_code=400)
    thread_id = tob_thread_id(external_id)
    return StreamingResponse(_sse(thread_id, message), media_type="text/event-stream")


@local_only
async def list_threads(_request: Request) -> JSONResponse:
    async with _runtime.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT thread_id, COUNT(*) AS n FROM checkpoints WHERE thread_id LIKE 'tob:%' "
                "GROUP BY thread_id ORDER BY n DESC"
            )
            rows = await cur.fetchall()
    return JSONResponse([{"thread_id": thread_id, "checkpoint_count": n} for thread_id, n in rows])


@local_only
async def get_state(request: Request) -> JSONResponse:
    external_id = request.path_params["external_id"]
    thread_id = tob_thread_id(external_id)
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
    external_id = request.path_params["external_id"]
    thread_id = tob_thread_id(external_id)
    async with _runtime.lock_for(thread_id):
        await _runtime.agent.checkpointer.adelete_thread(thread_id)
    return JSONResponse({"ok": True})


routes = [
    Route("/v1/tob/threads", list_threads),
    Route("/v1/tob/threads/{external_id}/runs", create_run, methods=["POST"]),
    Route("/v1/tob/threads/{external_id}/state", get_state),
    Route("/v1/tob/threads/{external_id}", delete_thread, methods=["DELETE"]),
]
