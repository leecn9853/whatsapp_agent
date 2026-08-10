import base64
import logging
import mimetypes
import os
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage, ToolMessage
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.context import ContextSchema
from src.main import agent

logger = logging.getLogger(__name__)

WHATSAPP_SIMULATOR_URL = os.getenv("WHATSAPP_SIMULATOR_URL", "http://localhost:3000")


def _files_saved_this_turn(messages: list) -> list[Path]:
    """从本轮（最后一条 HumanMessage 之后）的 save_file 工具调用结果里提取文件路径。

    thread_id 按 chat_id 复用，result["messages"] 会带上该会话的完整历史，
    所以只取最后一条 HumanMessage 之后的部分，避免把之前几轮已经发过的文件重新发一遍。
    """
    human_indices = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    start = human_indices[-1] if human_indices else 0

    paths = []
    for msg in messages[start:]:
        if isinstance(msg, ToolMessage) and msg.name == "save_file":
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            path = Path(content)
            if path.is_file():
                paths.append(path)
    return paths


async def _send_file(client: httpx.AsyncClient, chat_id: str, path: Path) -> None:
    mimetype, _ = mimetypes.guess_type(path.name)
    media_base64 = base64.b64encode(path.read_bytes()).decode()
    resp = await client.post(
        f"{WHATSAPP_SIMULATOR_URL}/messages/media",
        json={
            "to": chat_id,
            "mediaBase64": media_base64,
            "mimetype": mimetype or "application/octet-stream",
            "filename": path.name,
        },
    )
    resp.raise_for_status()


async def webhook(request: Request) -> JSONResponse:
    payload = await request.json()

    if payload.get("event") != "message":
        return JSONResponse({"ok": True})

    data = payload.get("data") or {}
    chat_id = data.get("from")
    body = data.get("body")

    if not chat_id or not body:
        return JSONResponse({"ok": True})

    if chat_id.endswith("@g.us"):
        # 默认不自动回复群聊，避免机器人在群里刷屏
        return JSONResponse({"ok": True})

    try:
        result = await run_in_threadpool(
            agent.invoke,
            {"messages": [HumanMessage(content=body)]},
            config={"configurable": {"thread_id": chat_id}},
            context=ContextSchema(caller="whatsapp", user_id=chat_id),
        )
        reply = result["messages"][-1].content
        files = _files_saved_this_turn(result["messages"])

        async with httpx.AsyncClient(timeout=60) as client:
            reply_resp = await client.post(
                f"{WHATSAPP_SIMULATOR_URL}/messages",
                json={"to": chat_id, "message": reply},
            )
            reply_resp.raise_for_status()
            for path in files:
                try:
                    await _send_file(client, chat_id, path)
                except httpx.HTTPStatusError as e:
                    logger.error(
                        "发送文件 %s 给 %s 失败：%s %s",
                        path,
                        chat_id,
                        e.response.status_code,
                        e.response.text,
                    )
                except Exception:
                    logger.exception("发送文件 %s 给 %s 失败", path, chat_id)
    except Exception:
        logger.exception("处理来自 %s 的 webhook 消息失败", chat_id)

    return JSONResponse({"ok": True})


app = Starlette(routes=[Route("/webhook", webhook, methods=["POST"])])
