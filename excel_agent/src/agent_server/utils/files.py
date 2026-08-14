"""从本轮（最后一条 HumanMessage 之后）产出文件的工具调用结果里提取文件路径。

目前唯一的调用方是 _engine.py（run_agent_turn 成功/失败两条路径都要用），单独
放一个模块是因为它是纯函数，不依赖 _runtime 的任何运行时状态。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import HumanMessage, ToolMessage

from src.agent.tools.excel_tools import OUTPUT_FILE_TOOL_NAMES

FILE_OUTPUT_TOOL_NAMES = {"save_file", *OUTPUT_FILE_TOOL_NAMES}


def files_saved_this_turn(messages: list) -> list[Path]:
    """thread_id 按 user_id 复用，result["messages"] 会带上该会话的完整历史，
    所以只取最后一条 HumanMessage 之后的部分，避免把之前几轮已经发过的文件重新发一遍。
    FILE_OUTPUT_TOOL_NAMES 里的工具都用 response_format="content_and_artifact"
    声明：真实的绝对路径只放在 ToolMessage.artifact 里，不会进入喂给模型的
    content（否则模型会拿这个真实路径去调内置的文件系统工具，而那些工具跑在
    虚拟路径空间里，根本找不到这个路径——历史上就踩过这个坑）。

    同一个文件路径只保留一份：aggregate_excel_sheet/create_chart_sheet 对已经在
    output/ 里的文件是原地覆盖（见 excel_tools._resolve_save_path），一次任务里
    先聚合再画图时两个工具调用会指向同一个文件，不去重会把同一份文件发两遍。
    """
    human_indices = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    start = human_indices[-1] if human_indices else 0

    paths: list[Path] = []
    seen: set[Path] = set()
    for msg in messages[start:]:
        if not (isinstance(msg, ToolMessage) and msg.name in FILE_OUTPUT_TOOL_NAMES):
            continue
        if not msg.artifact:
            continue
        path = Path(msg.artifact)
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(path)
    return paths
