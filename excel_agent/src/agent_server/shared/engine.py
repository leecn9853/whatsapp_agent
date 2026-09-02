"""跑一次 agent 对话轮次的共享执行引擎：attempt 重试 + 从 checkpoint 续跑 + 工具调用
事件提取，供 channels/tob/routes.py（SSE API）和 channels/whatsapp/processor.py（webhook）复用。

run_agent_turn 是个异步生成器：过程中每出现一次工具调用就 yield 一次工具名（str），
成功时最后 yield 一个 RunResult 然后正常结束；全部 attempt 耗尽后 raise RunFailed
（带原始异常和本轮已落盘的文件）。两个调用方唯一的区别是拿到工具调用名/RunResult
后"怎么呈现"（转成 SSE 帧 / 按 TOOL_PROGRESS_MESSAGES 主动推送 WhatsApp 消息），这
由调用方在消费这个生成器时自己决定，引擎本身不关心呈现方式。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from langchain.agents.middleware import InputAgentState
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from src.context import ContextSchema
from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.files import files_saved_this_turn
from src.agent_server.shared.log_context import bind_run_context

logger = logging.getLogger(__name__)

# 单次 attempt 的超时窗口：从本次 attempt 的续跑点开始，跑完这次「剩余的所有
# 步骤」（可能是好几个节点，包含调用方消费 yield 出的事件所花的时间）算作一次
# attempt，不是单个节点的超时。
AGENT_ATTEMPT_TIMEOUT_SECONDS = float(os.getenv("AGENT_ATTEMPT_TIMEOUT_SECONDS", "300"))
# 最多尝试几次（含第一次）。续跑机制下重试不会重跑已提交的节点，主要用来兜住网络
# 抖动、偶发超时/5xx 这类瞬时故障，不是为了"给任务更多时间"。
AGENT_MAX_ATTEMPTS = int(os.getenv("AGENT_MAX_ATTEMPTS", "3"))
# 两次尝试之间等多久再重试，按 attempt 次数线性增长（1st retry 等待 3s，2nd retry 等待 6s...）。
AGENT_RETRY_BACKOFF_SECONDS = float(os.getenv("AGENT_RETRY_BACKOFF_SECONDS", "3"))


@dataclass
class RunResult:
    reply: str
    files: list[Path]


class RunFailed(Exception):
    """全部 attempt 耗尽后抛出，携带原始异常和本轮失败前已经落盘的文件。"""

    def __init__(self, cause: Exception, files: list[Path]) -> None:
        super().__init__(str(cause) or type(cause).__name__)
        self.cause = cause
        self.files = files


async def _files_from_checkpoint(config: RunnableConfig) -> list[Path]:
    try:
        snapshot = await _runtime.agent.aget_state(config)
    except Exception:
        logger.exception("读取 checkpoint 失败，无法收集本轮已生成的文件")
        return []
    return files_saved_this_turn(snapshot.values.get("messages") or [])


async def _stream_tool_calls(
    input_: InputAgentState | None,
    config: RunnableConfig,
    context: ContextSchema,
) -> AsyncIterator[str]:
    """跑一次 agent.astream，把工具调用名逐个 yield 出来；异常直接从生成器里抛出，
    交给上层的重试循环处理。
    """
    async for chunk in _runtime.agent.astream(
        input_,
        config=config,
        context=context,
        stream_mode="updates",
        durability="sync",
    ):
        node_output = chunk.get("model")
        if not node_output:
            continue
        for msg in node_output.get("messages", []):
            seen: set[str] = set()
            for call in getattr(msg, "tool_calls", None) or []:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
                if not name or name in seen:
                    continue
                seen.add(name)
                yield name


async def run_agent_turn(
    thread_id: str,
    message: str,
    context: ContextSchema,
    *,
    run_id: str,
) -> AsyncIterator[str | RunResult]:
    """跑一次完整对话轮次：attempt 重试 + checkpoint 续跑。期间每次工具调用 yield
    一个工具名（str），成功时最后 yield 一个 RunResult；全部 attempt 耗尽后 raise
    RunFailed（带原始异常和本轮已落盘的文件）。

    重试不是把同一条用户消息重新灌一遍从头跑，而是从上次中断的 checkpoint 续跑
    （agent.astream(None, ...)）：已经成功提交的节点（包括已经执行过的工具调用）
    不会重新执行，避免有副作用的工具被重复触发。只有第 1 次尝试真正带上用户消息；
    第 2 次起如果发现根本没有可续的 checkpoint（极端情况：连第一次调用都没能写入
    任何 checkpoint 就失败了），才退化为重新带上原始消息。
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    last_error: Exception | None = None
    started_at = time.monotonic()

    await _runtime.runs_store.amark_running(run_id)

    with bind_run_context(run_id, thread_id):
        logger.info("开始处理消息")

        for attempt in range(1, AGENT_MAX_ATTEMPTS + 1):
            await _runtime.runs_store.arecord_attempt(run_id, attempt)

            input_: InputAgentState | None
            if attempt == 1:
                input_ = {"messages": [HumanMessage(content=message)]}
            else:
                input_ = None
                snapshot = await _runtime.agent.aget_state(config)
                if not snapshot.values.get("messages"):
                    input_ = {"messages": [HumanMessage(content=message)]}

            try:
                async with asyncio.timeout(AGENT_ATTEMPT_TIMEOUT_SECONDS):
                    async for name in _stream_tool_calls(input_, config, context):
                        yield name

                snapshot = await _runtime.agent.aget_state(config)
                messages = snapshot.values["messages"]
                await _runtime.runs_store.amark_success(run_id)
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                logger.info("处理完成，耗时 %dms，尝试 %d 次", elapsed_ms, attempt)
                yield RunResult(reply=messages[-1].content, files=files_saved_this_turn(messages))
                return
            except TimeoutError:
                last_error = TimeoutError(f"agent 执行超过 {AGENT_ATTEMPT_TIMEOUT_SECONDS}s")
            except Exception as e:
                last_error = e

            logger.warning(
                "第 %d/%d 次调用 agent 失败（thread_id=%s）：%s",
                attempt,
                AGENT_MAX_ATTEMPTS,
                thread_id,
                last_error,
                exc_info=last_error,
            )
            if attempt < AGENT_MAX_ATTEMPTS:
                await asyncio.sleep(AGENT_RETRY_BACKOFF_SECONDS * attempt)

        assert last_error is not None
        await _runtime.runs_store.amark_error(
            run_id, f"{type(last_error).__name__}: {last_error}" if str(last_error) else type(last_error).__name__
        )
        files = await _files_from_checkpoint(config)
        raise RunFailed(last_error, files) from last_error
