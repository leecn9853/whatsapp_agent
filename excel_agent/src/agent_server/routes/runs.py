"""POST /v1/threads/{thread_id}/runs：跑一次 agent 执行，SSE 流式返回进度和结果。

实际的 attempt 重试/续跑/工具调用事件提取都在 _engine.run_agent_turn 里，和
whatsapp/processor.py 共用同一份实现；这里只负责把工具调用事件转成 SSE 帧、把
最终结果/异常转成 done/error 帧。
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from src.context import ContextSchema
from src.agent_server import _runtime
from src.agent_server._engine import RunFailed, RunResult, run_agent_turn
from src.agent_server.utils.security import local_only


def _sse_frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _sse(thread_id: str, message: str, caller: str):
    run_id = await _runtime.runs_store.acreate_run(thread_id)
    context = ContextSchema(caller=caller, user_id=thread_id, run_id=run_id)

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
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    message = body.get("message")
    if not message:
        return JSONResponse({"error": "message 不能为空"}, status_code=400)
    caller = body.get("caller") or "api"
    return StreamingResponse(_sse(thread_id, message, caller), media_type="text/event-stream")


routes = [Route("/v1/threads/{thread_id}/runs", create_run, methods=["POST"])]
