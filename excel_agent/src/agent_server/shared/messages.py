"""把 checkpointer 里的 langchain message 序列化成 JSON：channels/tob/admin.py
（内部排查页）和 channels/tob/routes.py（对外 state 接口）都要把 messages 转成
JSON 返回给前端，字段要求一样（含 tool_calls/artifact），放这里避免两份实现
分叉、也避免 admin.py 和 routes.py 互相 import（两者故意保持独立，见 admin.py
顶部说明）。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage


def serialize_message(m) -> dict:
    entry: dict = {
        "id": getattr(m, "id", None),
        "role": type(m).__name__,
        "content": str(getattr(m, "content", m)),
    }
    if isinstance(m, AIMessage) and m.tool_calls:
        entry["tool_calls"] = [
            {"name": tc.get("name"), "args": tc.get("args"), "id": tc.get("id")}
            for tc in m.tool_calls
        ]
    if isinstance(m, ToolMessage):
        entry["tool_name"] = m.name
        entry["tool_call_id"] = m.tool_call_id
        if m.artifact is not None:
            entry["artifact"] = str(m.artifact)
    return entry
