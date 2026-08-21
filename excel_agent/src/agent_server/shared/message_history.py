"""从 checkpoint 历史里按日期重建消息列表，供 channels/tob/admin.py 的对话历史手风琴用。

LangGraph 的消息状态本身没有"单条消息时间戳"：`aget_state` 只给出最新一份 messages
快照，时间信息只存在于每个 checkpoint 的 `created_at`（StateSnapshot 字段）上。要把
消息按天分组，只能靠 `aget_state_history` 逐个 checkpoint 回溯，把"这个 checkpoint
比上一个（更新的）checkpoint 多出来的消息"归到它的 created_at 日期下。

这里按消息 `id` 做差集，不能按列表长度/前缀比较——SummarizationMiddleware
（src/agent/main.py）触发压缩时会用 RemoveMessage 把旧消息从 state 里删掉、换成一条
摘要消息，messages 列表不是单纯 append-only。langgraph 的 add_messages reducer会给
所有缺 id 的消息在写入 state 时分配一个 UUID 且此后不再改变（见
langgraph.graph.message.add_messages），所以每条消息的 id 在其存在期间是稳定唯一的，
按 id 差集能正确地把"什么时候第一次出现"这件事归因到正确的 checkpoint，不受后续
删除/压缩影响。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from src.agent_server.shared.messages import serialize_message


async def _awalk_batches(agent: Any, thread_id: str) -> AsyncIterator[tuple[str | None, list]]:
    """按时间倒序（新->旧）yield (created_at, messages_batch)。

    messages_batch 是该 checkpoint 相对于"更新的那个 checkpoint"新增的消息，
    保持原始（时间正序）相对顺序。
    """
    config = {"configurable": {"thread_id": thread_id}}
    newer_messages: list | None = None
    newer_created_at: str | None = None
    async for snapshot in agent.aget_state_history(config):
        messages = snapshot.values.get("messages", [])
        if newer_messages is not None:
            older_ids = {m.id for m in messages}
            added = [m for m in newer_messages if m.id not in older_ids]
            if added:
                yield newer_created_at, added
        newer_messages = messages
        newer_created_at = snapshot.created_at
    if newer_messages:
        yield newer_created_at, newer_messages


async def alist_message_dates(agent: Any, thread_id: str) -> list[dict]:
    """按天分组的消息条数骨架，新->旧排列。"""
    order: list[str] = []
    counts: dict[str, int] = {}
    async for created_at, batch in _awalk_batches(agent, thread_id):
        date = (created_at or "unknown")[:10]
        if date not in counts:
            counts[date] = 0
            order.append(date)
        counts[date] += len(batch)
    return [{"date": date, "count": counts[date]} for date in order]


async def alist_messages_for_date(agent: Any, thread_id: str, date: str) -> list[dict]:
    """某一天的消息，按时间正序。倒序遍历历史，一旦跨过目标日期就提前 break，
    不用读完整个 thread 的 checkpoint 历史。
    """
    batches: list[list] = []
    started = False
    async for created_at, batch in _awalk_batches(agent, thread_id):
        batch_date = (created_at or "unknown")[:10]
        if batch_date == date:
            started = True
            batches.append(batch)
        elif started:
            break
    batches.reverse()
    return [serialize_message(m) for batch in batches for m in batch]
