"""POST /webhook：接收 WhatsApp 网关推送的消息事件，解析后交给 channels/whatsapp/
processor.py 处理，处理过程转入后台任务。

上传文件格式校验、下载失败提示这类"跟有没有调用 agent 无关"的短路回复留在这里；
真正调用 agent、推送结果的逻辑在 channels/whatsapp/processor.py。

这里拿到的 `phone` 是 WhatsApp 网关认的原始手机号，只用来发消息；调 processor 的
run_agent_turn 相关函数时要转成 `thread_ids.whatsapp_thread_id(phone)` 再传，不能
把裸手机号当 thread_id 用（否则会和其它渠道的 thread_id 撞命名空间）。
"""

import asyncio
import base64
import contextlib
import logging
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.agent.tools.excel_tools import save_uploaded_file
from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.thread_ids import whatsapp_thread_id
from src.agent_server.channels.whatsapp.client import send_text
from src.agent_server.channels.whatsapp.processor import process_message, reset_thread
from src.agent_server.channels.whatsapp.voice import process_voice_message

logger = logging.getLogger(__name__)

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
        phone = data.get("from")
        if phone and not phone.endswith("@g.us"):
            task = asyncio.create_task(reset_thread(whatsapp_thread_id(phone)))
            _track(task)
        return JSONResponse({"ok": True})

    if payload.get("event") != "message":
        return JSONResponse({"ok": True})

    data = payload.get("data") or {}
    phone = data.get("from")
    body = data.get("body") or ""
    media = data.get("media")
    media_error = data.get("mediaError")

    if not phone or not (body or media or media_error):
        return JSONResponse({"ok": True})

    if phone.endswith("@g.us"):
        # 默认不自动回复群聊，避免机器人在群里刷屏
        return JSONResponse({"ok": True})

    async with httpx.AsyncClient(timeout=60) as client:
        if media_error:
            with contextlib.suppress(Exception):
                await send_text(client, phone, MEDIA_ERROR_MESSAGE)
            return JSONResponse({"ok": True})

        if media:
            mimetype = (media.get("mimetype") or "").split(";")[0].strip().lower()
            if data.get("type") == "ptt" or mimetype.startswith("audio/"):
                audio_bytes = base64.b64decode(media["data"])
                thread_id = whatsapp_thread_id(phone)
                run_id = await _runtime.runs_store.acreate_run(thread_id)
                logger.info("收到语音消息，开始处理 phone=%s thread_id=%s run_id=%s", phone, thread_id, run_id)
                task = asyncio.create_task(
                    process_voice_message(phone, thread_id, run_id, audio_bytes, mimetype)
                )
                _track(task)
                return JSONResponse({"ok": True})

            suffix = Path(media.get("filename") or "").suffix.lower()
            if suffix not in ALLOWED_EXCEL_EXTENSIONS:
                with contextlib.suppress(Exception):
                    await send_text(client, phone, UNSUPPORTED_FORMAT_MESSAGE)
                return JSONResponse({"ok": True})

            saved_path = save_uploaded_file(phone, media["filename"], base64.b64decode(media["data"]))
            notice = f"[用户上传了文件：{saved_path.name}]"
            body = f"{body}\n{notice}" if body else notice

    thread_id = whatsapp_thread_id(phone)
    run_id = await _runtime.runs_store.acreate_run(thread_id)
    logger.info("收到消息，开始处理 phone=%s thread_id=%s run_id=%s", phone, thread_id, run_id)
    task = asyncio.create_task(process_message(phone, thread_id, run_id, body))
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
