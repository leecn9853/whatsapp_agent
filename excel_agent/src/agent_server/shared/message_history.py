"""从 checkpoint 历史里按游标分页重建消息列表，供 channels/tob/admin.py 的对话历史
滚动加载用。

LangGraph 的消息状态本身没有"单条消息时间戳"：`aget_state` 只给出最新一份 messages
快照，时间信息只存在于每个 checkpoint 的 `created_at`（StateSnapshot 字段）上。要把
消息按时间归类，只能靠 `aget_state_history` 逐个 checkpoint 回溯，把"这个 checkpoint
比上一个（更新的）checkpoint 多出来的消息"归到它的 created_at 下——同一个 checkpoint
里新增的消息共享同一个时间戳，这是 langgraph 能给到的最细粒度，不是偷懒。

这里按消息 `id` 做差集，不能按列表长度/前缀比较——SummarizationMiddleware
（src/agent/main.py）触发压缩时会用 RemoveMessage 把旧消息从 state 里删掉、换成一条
摘要消息，messages 列表不是单纯 append-only。langgraph 的 add_messages reducer会给
所有缺 id 的消息在写入 state 时分配一个 UUID 且此后不再改变（见
langgraph.graph.message.add_messages），所以每条消息的 id 在其存在期间是稳定唯一的，
按 id 差集能正确地把"什么时候第一次出现"这件事归因到正确的 checkpoint，不受后续
删除/压缩影响。

分页设计：不能像早期版本那样一次性 `aget_state_history(config)`（不带 limit）走完
整个 thread 的 checkpoint 历史去建"日期骨架"——thread 变长之后这个无差别回溯本身就
很慢，跟消息本身是否按需加载无关。改成游标分页：游标就是某个 checkpoint 的
checkpoint_id，翻页时先对它做一次 O(1) 点查（`aget_state`）拿到它的 messages/
created_at 作为"更新一侧"的种子，再用它的 `.config` 当 `aget_state_history` 的
`before=` 参数继续回溯——这样游标只需要传一个字符串，不用把消息 id 列表倒来倒去。

`_SCAN_PAGE`/`_SCAN_CAP` 是内部实现细节：有些 checkpoint 只更新非消息字段（比如
路由/计划字段），diff 出来的 added 是空的，如果拿"凑够多少个非空 batch"当停止条件，
会把"这批 checkpoint 全是空更新"误判成"已经翻到 thread 起点"，导致更早的历史丢失。
正确判断方式是看这一轮请求实际拿到了多少个 checkpoint（`n_yielded`），不是看非空
batch 数；同时设一个硬扫描上限，避免消息稀疏的 thread 让单次请求无限扫描下去。
"""

from __future__ import annotations

from typing import Any

from src.agent_server.shared.messages import serialize_message

_SCAN_PAGE = 50
_SCAN_CAP = 500


async def alist_messages_page(
    agent: Any, thread_id: str, *, before: str | None = None, limit: int = 50
) -> dict:
    """按游标分页取一批消息，旧->新排列。

    返回 {"messages": [...], "next_cursor": str | None, "has_more": bool}。
    """
    config = {"configurable": {"thread_id": thread_id}}

    if before is None:
        newer_messages: list | None = None
        newer_created_at: str | None = None
        before_config = None
    else:
        boundary = await agent.aget_state(
            {"configurable": {"thread_id": thread_id, "checkpoint_id": before}}
        )
        newer_messages = boundary.values.get("messages", [])
        newer_created_at = boundary.created_at
        before_config = boundary.config

    batches: list[tuple[str | None, list]] = []
    scanned = 0
    hit_limit = False
    reached_true_end = False

    while scanned < _SCAN_CAP:
        page_size = min(_SCAN_PAGE, _SCAN_CAP - scanned)
        n_yielded = 0
        async for snapshot in agent.aget_state_history(config, before=before_config, limit=page_size):
            n_yielded += 1
            scanned += 1
            messages = snapshot.values.get("messages", [])
            if newer_messages is not None:
                older_ids = {m.id for m in messages}
                added = [m for m in newer_messages if m.id not in older_ids]
                if added:
                    batches.append((newer_created_at, added))
            newer_messages = messages
            newer_created_at = snapshot.created_at
            before_config = snapshot.config
            if len(batches) >= limit:
                hit_limit = True
                break
        if hit_limit:
            break
        if n_yielded < page_size:
            # 这一批拿到的 checkpoint 数比要求的少，说明已经扫到 thread 真正起点。
            reached_true_end = True
            break
        # 这一批扫满了但还没凑够 limit 个非空 batch（比如全是非消息字段的更新），继续扫下一批。

    if reached_true_end:
        # thread 第一个 checkpoint 没有更旧的可以 diff，它的全部消息都算"新增"。
        if newer_messages:
            batches.append((newer_created_at, newer_messages))
        next_cursor: str | None = None
        has_more = False
    elif hit_limit:
        next_cursor = before_config["configurable"]["checkpoint_id"]
        has_more = True
    else:
        # 扫到了硬上限（_SCAN_CAP）还没凑够 limit 也没扫到起点：把断点交给下一页继续扫。
        next_cursor = before_config["configurable"]["checkpoint_id"] if before_config else None
        has_more = before_config is not None

    batches.reverse()
    out_messages: list[dict] = []
    for created_at, batch in batches:
        for m in batch:
            entry = serialize_message(m)
            entry["created_at"] = created_at
            out_messages.append(entry)
    return {"messages": out_messages, "next_cursor": next_cursor, "has_more": has_more}
