"""按 thread_id 查长期记忆（/memories/ 写入的 AGENTS.md）。

`src/agent/main.py` 里 `/memories/` 的 namespace 是 `(rt.context.user_id or "debug").replace(".", "_")`，
即 thread_id 本身（见 shared/thread_ids.py），只是把句号换成下划线；这里复用同一条 escaping 规则，
避免调用方各自拼一遍容易写错。

目前只有 channels/tob/admin.py（内部查看页面，看任意用户的记忆）在用；以后 toC 如果要给用户加一个
"查看我的记忆"的自助页面，也应该复用这个函数，不要重新拼 namespace。
"""

from __future__ import annotations

MEMORY_KEY = "/AGENTS.md"


async def aget_memory(store, thread_id: str) -> str | None:
    item = await store.aget((thread_id.replace(".", "_"),), MEMORY_KEY)
    return item.value["content"] if item is not None else None
