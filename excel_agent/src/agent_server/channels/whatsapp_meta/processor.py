"""处理一条已经解析好的 WhatsApp Meta 文本消息：跑一次 agent 执行、推送进度、发最终结果。

跟 channels/whatsapp/processor.py 同构，区分 `phone`（Graph API 认的手机号，只用来发消息）
和 `thread_id`（带 `wa_meta:` 前缀的引擎/checkpoint key，见 shared/thread_ids.py）。本次不发
媒体，没有 send_file 分支。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

import httpx

from src.context import ContextSchema
from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.engine import RunFailed, RunResult, run_agent_turn
from src.agent_server.channels.whatsapp_meta import dedup
from src.agent_server.channels.whatsapp_meta.client import send_text

logger = logging.getLogger(__name__)

PROCESSING_NOTICE_SECONDS = float(os.getenv("PROCESSING_NOTICE_SECONDS", "20"))
FAILURE_MESSAGE = "抱歉，刚刚处理你的消息时出错了，请稍后再试一次。"

# 24 小时会话窗口：超过这个时长没有该用户的新 inbound 消息，就不能再发自由文本，只能发
# 已审批的消息模板（本次未实现消息模板，超窗只记日志，见 docs/whatsapp-meta-channel-design.md）。
SESSION_WINDOW_SECONDS = 24 * 60 * 60

TOOL_PROGRESS_MESSAGES: dict[str, str] = {
    "list_excel_files": "正在查看你的表格文件…",
    "inspect_excel": "正在查看表格内容…",
    "aggregate_excel_sheet": "正在按你的要求汇总数据…",
    "create_chart_sheet": "正在生成图表…",
    "web_search": "正在联网搜索…",
    "save_file": "正在保存文件…",
    "task": "正在委托子任务处理，请稍候…",
}


async def _notify_if_slow(client: httpx.AsyncClient, phone: str) -> None:
    await asyncio.sleep(PROCESSING_NOTICE_SECONDS)
    try:
        await send_text(client, phone, "正在处理中，请稍候…")
    except Exception:
        logger.warning("发送'处理中'提示给 %s 失败", phone, exc_info=True)


def _within_session_window(phone: str) -> bool:
    last = dedup.last_inbound_at(phone)
    return last is not None and (time.monotonic() - last) < SESSION_WINDOW_SECONDS


async def _reply(client: httpx.AsyncClient, phone: str, text: str) -> None:
    """发自由文本前先判断 24 小时窗口，超窗不强行调用 API（会被拒），改成记日志。"""
    if not _within_session_window(phone):
        logger.error(
            "用户 %s 已超过 24 小时会话窗口，无法发自由文本回复（需要消息模板，本次未实现）：%s",
            phone,
            text,
        )
        return
    await send_text(client, phone, text)


async def process_message(phone: str, thread_id: str, run_id: str, body: str) -> None:
    async with _runtime.lock_for(thread_id), httpx.AsyncClient(timeout=60) as client:
        context = ContextSchema(caller="whatsapp_meta", user_id=thread_id, run_id=run_id)
        notice_task = asyncio.create_task(_notify_if_slow(client, phone))

        result: RunResult | None = None
        try:
            async for event in run_agent_turn(thread_id, body, context, run_id=run_id):
                if isinstance(event, RunResult):
                    result = event
                    continue
                text = TOOL_PROGRESS_MESSAGES.get(event)
                if text:
                    with contextlib.suppress(Exception):
                        await _reply(client, phone, text)
        except asyncio.CancelledError:
            await _runtime.runs_store.amark_cancelled(run_id)
            raise
        except RunFailed:
            logger.exception("处理来自 %s 的消息失败", phone)
            with contextlib.suppress(Exception):
                await _reply(client, phone, FAILURE_MESSAGE)
            return
        finally:
            notice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notice_task

        assert result is not None
        try:
            await _reply(client, phone, result.reply)
        except Exception:
            logger.exception("发送回复给 %s 失败", phone)


async def reset_thread(thread_id: str) -> None:
    async with _runtime.lock_for(thread_id):
        await _runtime.agent.checkpointer.adelete_thread(thread_id)
