"""处理一条已经解析好的 WhatsApp 消息：跑一次 agent 执行、推送进度、发最终结果。

routes/webhook.py 负责把原始 webhook payload 解析成 (user_id, body)，剩下"怎么跑
agent、怎么把结果推给用户"这部分业务逻辑都在这个模块里。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import httpx

from src.context import ContextSchema
from src.agent_server import _runtime
from src.agent_server._engine import RunFailed, RunResult, run_agent_turn
from src.agent_server.whatsapp.client import send_file, send_text

logger = logging.getLogger(__name__)

# 处理超过这么久还没回复，先提示用户一句"还在处理"，避免用户以为卡死了
PROCESSING_NOTICE_SECONDS = float(os.getenv("PROCESSING_NOTICE_SECONDS", "20"))
# 处理失败时给用户的通用报错提示，避免把 agent 内部的异常信息直接暴露给用户。
FAILURE_MESSAGE = "抱歉，刚刚处理你的消息时出错了，请稍后再试一次。"

# 每次执行遇到这些工具调用时，往 WhatsApp 推送一句人类可读的进度提示。不在这个
# 表里的工具（比如 write_todos，规划用、跑得快、对用户没有可读性价值）保持静默。
TOOL_PROGRESS_MESSAGES: dict[str, str] = {
    "list_excel_files": "正在查看你的表格文件…",
    "inspect_excel": "正在查看表格内容…",
    "aggregate_excel_sheet": "正在按你的要求汇总数据…",
    "create_chart_sheet": "正在生成图表…",
    "web_search": "正在联网搜索…",
    "save_file": "正在保存文件…",
    "task": "正在委托子任务处理，请稍候…",
}


async def _notify_if_slow(client: httpx.AsyncClient, user_id: str) -> None:
    """处理超过 PROCESSING_NOTICE_SECONDS 还没结束就提示用户一句，被取消时安静退出。

    和下面按工具调用推送的进度提示是互补关系：这句是"完全没有任何工具调用触发
    推送时"（比如模型长时间纯思考、或第一个工具本身就很慢）的兜底提示。
    """
    await asyncio.sleep(PROCESSING_NOTICE_SECONDS)
    try:
        await send_text(client, user_id, "正在处理中，请稍候…")
    except Exception:
        logger.warning("发送'处理中'提示给 %s 失败", user_id, exc_info=True)


async def process_message(user_id: str, run_id: str, body: str) -> None:
    """跑一次 agent 执行并把结果/进度推送给用户。

    routes/webhook.py 收到消息后立刻 ack，这个函数才是实际耗时的部分——不再阻塞
    HTTP 响应，结果和过程中的进度提示都通过 send_text/send_file 主动推送。
    """
    async with _runtime.lock_for(user_id), httpx.AsyncClient(timeout=60) as client:
        context = ContextSchema(caller="whatsapp", user_id=user_id, run_id=run_id)
        notice_task = asyncio.create_task(_notify_if_slow(client, user_id))

        result: RunResult | None = None
        try:
            async for event in run_agent_turn(user_id, body, context, run_id=run_id):
                if isinstance(event, RunResult):
                    result = event
                    continue
                text = TOOL_PROGRESS_MESSAGES.get(event)
                if text:
                    with contextlib.suppress(Exception):
                        await send_text(client, user_id, text)
        except asyncio.CancelledError:
            await _runtime.runs_store.amark_cancelled(run_id)
            raise
        except RunFailed as fail:
            logger.exception("处理来自 %s 的消息失败", user_id)
            with contextlib.suppress(Exception):
                await send_text(client, user_id, FAILURE_MESSAGE)
            for path in fail.files:
                try:
                    await send_file(client, user_id, path)
                except Exception:
                    logger.exception("发送文件 %s 给 %s 失败", path, user_id)
            return
        finally:
            notice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notice_task

        assert result is not None
        try:
            await send_text(client, user_id, result.reply)
        except Exception:
            logger.exception("发送回复给 %s 失败", user_id)
            return

        for path in result.files:
            try:
                await send_file(client, user_id, path)
            except httpx.HTTPStatusError as e:
                logger.error(
                    "发送文件 %s 给 %s 失败：%s %s",
                    path,
                    user_id,
                    e.response.status_code,
                    e.response.text,
                )
            except Exception:
                logger.exception("发送文件 %s 给 %s 失败", path, user_id)


async def reset_thread(user_id: str) -> None:
    """删除该用户的对话历史（checkpoint），不影响 /memories/ 长期记忆。

    复用 _runtime.lock_for(user_id) 是为了不和该用户正在处理中的 process_message
    并发：等它跑完再删，避免删除过程中还有新的 checkpoint 写入进来。
    """
    async with _runtime.lock_for(user_id):
        await _runtime.agent.checkpointer.adelete_thread(user_id)
