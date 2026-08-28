"""独立于 SummarizationMiddleware 的摘要审计中间件。

SummarizationMiddleware（langchain.agents.middleware）负责把对话历史压缩成自由文本
喂给模型，本身既不保证输出格式，也没有任何持久化钩子。这里另起一个完全独立的
AgentMiddleware：只读 state、只落库，永远返回 None、绝不修改 graph 状态，因此和
SummarizationMiddleware 之间没有任何调用关系，不依赖它的任何私有方法（如
_create_summary/_determine_cutoff_index）——两者唯一共享的东西是 token 触发阈值
这一个数字，别的各管各的。

触发节流：这个中间件在 middleware 列表里排在 SummarizationMiddleware 之前，且
用同样的 token 计数方式判断"是否达到阈值"。一旦达到阈值就落库一次，随后
SummarizationMiddleware 会把消息压下去，下一轮 token 数自然回落到阈值以下，不需要
额外的"本轮已审计"标记来防止重复触发。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, messages_to_dict
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from src.agent.middleware._multimodal import strip_multimodal_content
from src.context import ContextSchema

if TYPE_CHECKING:
    # 只在类型检查时导入：src.agent_server 是个"重"包，其 __init__.py 会顺带
    # import 回 src.agent.main，在运行时导入会造成循环导入。
    from src.agent_server.shared.summaries_store import SummariesStore


class ConversationSummarySchema(BaseModel):
    """对话摘要的固定输出格式，供落库和后续检索/报表使用。"""

    session_intent: str = Field(description="用户当前会话的核心目标/意图")
    excel_context: list[str] = Field(
        default_factory=list, description="本次会话涉及的表格文件名/sheet 名"
    )
    decisions: list[str] = Field(
        default_factory=list, description="已经确定下来的聚合口径、图表类型等偏好或结论"
    )
    next_steps: list[str] = Field(default_factory=list, description="尚待完成的事项")
    artifacts: list[str] = Field(
        default_factory=list, description="本次会话生成或引用过的文件路径"
    )


_SUMMARY_INSTRUCTION = (
    "阅读上面完整的对话历史，按给定字段结构提炼摘要：session_intent 是用户这次"
    "会话的核心目标；excel_context 列出涉及的表格文件/sheet；decisions 列出已经"
    "确定下来的聚合口径、图表类型等偏好或结论；next_steps 列出还没完成的事项；"
    "artifacts 列出本次会话生成或引用过的文件路径。没有对应信息的字段用空字符串"
    "或空列表，不要编造。"
)

# DeepSeek 的 OpenAI 兼容端点在 thinking 模式下不支持
# with_structured_output 默认走的 tool_choice 强制调用（method="function_calling"
# 报 "Thinking mode does not support this tool_choice"），native
# response_format=json_schema 也不支持（报 "This response_format type is
# unavailable now"）。改用 method="json_mode"：只按 response_format=json_object
# 走纯文本 JSON 输出，不会自动把 schema 注入 prompt，所以要靠下面这条
# SystemMessage 手动把 JSON Schema 喂给模型（json_object 模式本身要求 prompt
# 里出现"JSON"字样，这条消息顺带满足这个要求）。
_JSON_MODE_SCHEMA_HINT = SystemMessage(
    content=(
        "接下来请只输出一个符合以下 JSON Schema 的 JSON 对象，不要输出任何多余的"
        f"文字、代码块标记：\n{json.dumps(ConversationSummarySchema.model_json_schema(), ensure_ascii=False)}"
    )
)


class ConversationSummaryAuditMiddleware(AgentMiddleware[AgentState[Any], ContextSchema, Any]):
    """达到 token 阈值时，独立生成一份 Schema 化摘要并把压缩前/后的数据落库。"""

    def __init__(
        self,
        model: BaseChatModel,
        store: SummariesStore,
        *,
        trigger_tokens: int,
    ) -> None:
        super().__init__()
        self.store = store
        self.trigger_tokens = trigger_tokens
        self._structured_model = model.with_structured_output(
            ConversationSummarySchema, method="json_mode"
        )

    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[ContextSchema]
    ) -> None:
        messages = state["messages"]
        token_count = count_tokens_approximately(messages)
        if token_count < self.trigger_tokens:
            return None

        thread_id = runtime.context.user_id or "debug"
        sanitized_messages = strip_multimodal_content(messages)
        raw_summary = await self._structured_model.ainvoke(
            [_JSON_MODE_SCHEMA_HINT, *sanitized_messages, HumanMessage(content=_SUMMARY_INSTRUCTION)]
        )
        # with_structured_output 的静态返回类型是 dict[str, Any] | BaseModel（不管传入
        # 的 schema 是什么都一样宽），实际传入 pydantic 类时运行期必然是该类实例；
        # 这里用 isinstance 顺便把类型收窄给类型检查器。
        if not isinstance(raw_summary, ConversationSummarySchema):
            msg = f"结构化摘要输出类型不符合预期：{type(raw_summary)!r}"
            raise TypeError(msg)
        summary = raw_summary

        await self.store.acreate_summary(
            thread_id=thread_id,
            token_count_before=token_count,
            raw_messages=messages_to_dict(messages),
            summary=summary,
        )
        return None
