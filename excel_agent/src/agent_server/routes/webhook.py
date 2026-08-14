"""POST /webhook：接收 WhatsApp 网关推送的消息事件，解析后交给 whatsapp/processor.py
处理，处理过程转入后台任务。

上传文件格式校验、下载失败提示这类"跟有没有调用 agent 无关"的短路回复留在这里；
真正调用 agent、推送结果的逻辑在 whatsapp/processor.py。
"""

import asyncio
import base64
import contextlib
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.agent.tools.excel_tools import save_uploaded_file
from src.agent_server import _runtime
from src.agent_server.whatsapp.client import send_text
from src.agent_server.whatsapp.processor import process_message, reset_thread

# 目前只接收 Excel 文件；document 类型消息的后缀不在这个集合里就直接告知用户不支持，
# 不创建 run、不调用 agent。
ALLOWED_EXCEL_EXTENSIONS = {".xlsx", ".xls"}
UNSUPPORTED_FORMAT_MESSAGE = "目前只支持 Excel 文件（.xlsx / .xls），暂不支持这个格式，请重新发送 Excel 文件。"
MEDIA_ERROR_MESSAGE = "刚才那个文件没有接收成功（可能太大或下载失败），请重新发送一次。"

# webhook 立即 ack 后，agent 执行转入后台任务；这里持有引用防止任务被 GC
# （asyncio 不会保留仅靠 create_task 创建、无人持有的任务的强引用）。
_background_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def webhook(request: Request) -> JSONResponse:
    payload = await request.json()

    if payload.get("event") == "chat_removed":
        data = payload.get("data") or {}
        user_id = data.get("from")
        if user_id and not user_id.endswith("@g.us"):
            task = asyncio.create_task(reset_thread(user_id))
            _track(task)
        return JSONResponse({"ok": True})

    if payload.get("event") != "message":
        return JSONResponse({"ok": True})

    data = payload.get("data") or {}
    user_id = data.get("from")
    body = data.get("body") or ""
    media = data.get("media")
    media_error = data.get("mediaError")

    if not user_id or not (body or media or media_error):
        return JSONResponse({"ok": True})

    if user_id.endswith("@g.us"):
        # 默认不自动回复群聊，避免机器人在群里刷屏
        return JSONResponse({"ok": True})

    async with httpx.AsyncClient(timeout=60) as client:
        if media_error:
            with contextlib.suppress(Exception):
                await send_text(client, user_id, MEDIA_ERROR_MESSAGE)
            return JSONResponse({"ok": True})

        if media:
            suffix = Path(media.get("filename") or "").suffix.lower()
            if suffix not in ALLOWED_EXCEL_EXTENSIONS:
                with contextlib.suppress(Exception):
                    await send_text(client, user_id, UNSUPPORTED_FORMAT_MESSAGE)
                return JSONResponse({"ok": True})

            saved_path = save_uploaded_file(user_id, media["filename"], base64.b64decode(media["data"]))
            notice = f"[用户上传了文件：{saved_path.name}]"
            body = f"{body}\n{notice}" if body else notice

    run_id = await _runtime.runs_store.acreate_run(user_id)
    task = asyncio.create_task(process_message(user_id, run_id, body))
    _track(task)

    return JSONResponse({"ok": True})


@contextlib.asynccontextmanager
async def lifespan(app):
    yield
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)


routes = [Route("/webhook", webhook, methods=["POST"])]
