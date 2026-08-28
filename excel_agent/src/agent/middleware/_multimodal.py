"""跨模块共享的多模态内容过滤 helper。

deepagents 的 `FilesystemMiddleware` 只在发给主模型的临时 `ModelRequest` 上按
`model.profile` 清洗过不支持的 multimodal content block（比如 `read_file` 预览生成
图片产出的 image block），从不修改持久化的 `state["messages"]`。任何绕开主模型节点、
自己直接拿 `state["messages"]` 发起 `.ainvoke()` 的代码（`topic_gate`、
`ConversationSummaryAuditMiddleware` 等）都不会受益于那层清洗，需要各自过滤一遍再传
给不支持图片等多模态输入的模型。
"""

from __future__ import annotations

from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage

_MULTIMODAL_BLOCK_TYPES = frozenset({"image", "audio", "video", "file"})


def strip_multimodal_content(messages: list[AnyMessage]) -> list[AnyMessage]:
    """把多模态 content block 换成文字占位符，其余内容原样保留。"""
    result: list[AnyMessage] = []
    for message in messages:
        if not isinstance(message, (ToolMessage, HumanMessage)):
            result.append(message)
            continue
        blocks = message.content_blocks
        new_blocks = [
            block
            if block["type"] not in _MULTIMODAL_BLOCK_TYPES
            else {"type": "text", "text": f"[已省略 {block['type']} 内容，该模型不支持多模态输入]"}
            for block in blocks
        ]
        result.append(message if new_blocks == blocks else message.model_copy(update={"content": new_blocks}))
    return result
