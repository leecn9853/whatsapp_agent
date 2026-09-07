import asyncio
import base64
import contextlib
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

# 单次 agent.invoke 的超时时间；超过就当作失败去重试
AGENT_TIMEOUT_SECONDS = float(os.getenv("AGENT_TIMEOUT_SECONDS", "60"))
# 最多尝试几次（含第一次），失败后仍带着同一条用户消息重试
AGENT_MAX_ATTEMPTS = int(os.getenv("AGENT_MAX_ATTEMPTS", "3"))
AGENT_RETRY_BACKOFF_SECONDS = float(os.getenv("AGENT_RETRY_BACKOFF_SECONDS", "3"))
# 处理超过这么久还没回复，先提示用户一句"还在处理"，避免用户以为卡死了
PROCESSING_NOTICE_SECONDS = float(os.getenv("PROCESSING_NOTICE_SECONDS", "20"))

FAILURE_MESSAGE = "抱歉，刚刚处理你的消息时出错了，请稍后再试一次。"

# 同一个 chat_id 的消息必须串行处理：如果用户连续发两条消息，前一条还没处理完，
# 两个 agent.invoke 会并发跑在同一个 thread_id 上，而 LangGraph 的 checkpointer
# 对同一 thread 的并发写入行为是未定义的，可能导致本轮生成的文件被错误地
# 归属到上一轮、或者消息重复/漏发。不同 chat_id 之间没有这个问题，仍可并发。
_chat_locks: dict[str, asyncio.Lock] = {}


def _lock_for(chat_id: str) -> asyncio.Lock:
    return _chat_locks.setdefault(chat_id, asyncio.Lock())


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


async def _send_text(client: httpx.AsyncClient, chat_id: str, message: str) -> None:
    resp = await client.post(
        f"{WHATSAPP_SIMULATOR_URL}/messages",
        json={"to": chat_id, "message": message},
    )
    resp.raise_for_status()


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


async def _notify_if_slow(client: httpx.AsyncClient, chat_id: str) -> None:
    """处理超过 PROCESSING_NOTICE_SECONDS 还没结束就提示用户一句，被取消时安静退出。"""
    await asyncio.sleep(PROCESSING_NOTICE_SECONDS)
    try:
        await _send_text(client, chat_id, "正在处理中，请稍候…")
    except Exception:
        logger.warning("发送'处理中'提示给 %s 失败", chat_id, exc_info=True)


async def _invoke_agent(chat_id: str, body: str):
    return await run_in_threadpool(
        agent.invoke,
        {"messages": [HumanMessage(content=body)]},
        config={"configurable": {"thread_id": chat_id}},
        context=ContextSchema(caller="whatsapp", user_id=chat_id),
    )


async def _invoke_with_retry(chat_id: str, body: str):
    """带超时和重试地调用 agent。

    每次重试都带着同一条用户消息重新调用（而不是尝试从 checkpoint 断点续跑），
    实现简单可靠：用户消息保证不会被静默丢弃。代价是如果失败发生在某个工具
    已经执行之后（比如 save_file 已经落盘），重试会让该工具再跑一次——对
    save_file 这种自带时间戳/随机数、无副作用冲突的工具是安全的，最多多出一个
    文件，不会覆盖或报错。
    """
    last_error: Exception | None = None
    for attempt in range(1, AGENT_MAX_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(
                _invoke_agent(chat_id, body), timeout=AGENT_TIMEOUT_SECONDS
            )
        except Exception as e:
            last_error = e
            logger.warning(
                "第 %d/%d 次调用 agent 失败（chat_id=%s）：%s",
                attempt,
                AGENT_MAX_ATTEMPTS,
                chat_id,
                e,
                exc_info=True,
            )
            if attempt < AGENT_MAX_ATTEMPTS:
                await asyncio.sleep(AGENT_RETRY_BACKOFF_SECONDS * attempt)
    assert last_error is not None
    raise last_error


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

    async with _lock_for(chat_id), httpx.AsyncClient(timeout=60) as client:
        notice_task = asyncio.create_task(_notify_if_slow(client, chat_id))
        try:
            result = await _invoke_with_retry(chat_id, body)
        except Exception:
            logger.exception("处理来自 %s 的 webhook 消息失败", chat_id)
            with contextlib.suppress(Exception):
                await _send_text(client, chat_id, FAILURE_MESSAGE)
            return JSONResponse({"ok": True})
        finally:
            notice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notice_task

        reply = result["messages"][-1].content
        files = _files_saved_this_turn(result["messages"])

        try:
            await _send_text(client, chat_id, reply)
        except Exception:
            logger.exception("发送回复给 %s 失败", chat_id)
            return JSONResponse({"ok": True})

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

    return JSONResponse({"ok": True})


app = Starlette(routes=[Route("/webhook", webhook, methods=["POST"])])
