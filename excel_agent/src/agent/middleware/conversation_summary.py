"""结构化对话摘要：Schema、prompt 常量、格式化函数，以及驱动实际压缩的中间件。

`StructuredSummarizationMiddleware` 用 `ConversationSummarySchema` 生成结构化摘要，
`render_summary_text` 把它格式化成喂给模型的替换文本，同一次调用产出的结构化字段再
落库（`summaries_store`）供审计对比压缩前后的内容。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentState, SummarizationMiddleware
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    messages_to_dict,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from src.agent.middleware._multimodal import strip_multimodal_content
from src.context import ContextSchema

if TYPE_CHECKING:
    # 只在类型检查时导入：src.agent_server 是个"重"包，其 __init__.py 会顺带
    # import 回 src.agent.main（进而可能触碰这个模块），运行时导入会有循环导入风险。
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


SUMMARY_INSTRUCTION = (
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
JSON_MODE_SCHEMA_HINT = SystemMessage(
    content=(
        "接下来请只输出一个符合以下 JSON Schema 的 JSON 对象，不要输出任何多余的"
        f"文字、代码块标记：\n{json.dumps(ConversationSummarySchema.model_json_schema(), ensure_ascii=False)}"
    )
)


def render_summary_text(summary: ConversationSummarySchema) -> str:
    """把结构化摘要格式化成喂给主模型的替换文本（取代原来的自由文本摘要）。

    格式是给模型读的，不是给人读的展示格式（那个在 toB 查看页面里单独渲染），所以
    直接用字段本身的语义当标题，没有信息的字段就不输出对应小节。
    """
    lines = [f"会话意图：{summary.session_intent or '（未说明）'}"]
    if summary.excel_context:
        lines.append(f"涉及表格：{'、'.join(summary.excel_context)}")
    if summary.decisions:
        lines.append("已确定的结论/偏好：")
        lines.extend(f"- {d}" for d in summary.decisions)
    if summary.next_steps:
        lines.append("待完成事项：")
        lines.extend(f"- {s}" for s in summary.next_steps)
    if summary.artifacts:
        lines.append("已生成/引用的文件：")
        lines.extend(f"- {a}" for a in summary.artifacts)
    return "\n".join(lines)


class StructuredSummarizationMiddleware(SummarizationMiddleware[Any, ContextSchema]):
    """在 langchain 内置 `SummarizationMiddleware` 基础上，把摘要生成换成结构化输出，
    并把同一次调用的结构化字段落库审计。

    为什么不能直接用基类：基类的 `_create_summary`/`_acreate_summary` 用自由文本
    `DEFAULT_SUMMARY_PROMPT` 生成摘要，字段覆盖和信息密度不受控。这里换成
    `ConversationSummarySchema` 的结构化输出——更贴合这个 Excel 报表助手的场景
    （`session_intent`/`excel_context`/`artifacts` 等字段对"追问已生成报表"这类场景
    的召回率明显更高），同一次模型调用产出的结构化字段还会落库到 `summaries_store`
    供 toB 查看页面审计对比（以前是另一个独立中间件单独再调一次模型做落库这件事，
    等于每次触发都打两次几乎重复的模型调用；现在合并成一次）。

    只覆写异步路径 `abefore_model`：这个项目里 agent 只通过
    `agent_server/shared/engine.py` 的 `agent.astream` 驱动，没有任何地方同步调用
    `agent.invoke`，同步 `before_model` 在这个部署里不可达；而且 `SummariesStore`
    只有异步接口（基于 `AsyncConnectionPool`），没必要为一条不可达路径造一个同步
    兜底。如果以后真的接入同步调用，这里会静默退回基类的自由文本摘要且不落库
    审计——到时候需要同步补一份同步版本。
    """

    def __init__(
        self,
        model: Any,
        *,
        trigger: Any,
        keep: Any,
        store: "SummariesStore",
    ) -> None:
        super().__init__(model=model, trigger=trigger, keep=keep)
        self.store = store
        # 用传入的 model 形参（类型是 Any）构建，不用 self.model：基类把 self.model
        # 存成了泛化的 Runnable[LanguageModelInput, AIMessage]，静态类型检查上没有
        # with_structured_output 这个方法（那是 BaseChatModel 才有的）。
        # 顺序不能反：with_structured_output 之后包 with_retry 才行——反过来的
        # with_retry() 返回的 RunnableRetry 是通用 Runnable 包装，没有
        # with_structured_output 这个 BaseChatModel 专属方法。
        self._structured_summary_model = model.with_structured_output(
            ConversationSummarySchema, method="json_mode"
        ).with_retry()

    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[ContextSchema]
    ) -> dict[str, Any] | None:
        """基类 `abefore_model` 的编排逻辑原样保留（触发判断、安全断点、AI/Tool
        消息配对保护都在 `_should_summarize`/`_determine_cutoff_index`/
        `_partition_messages` 里，这里不重新发明），只把生成摘要那一步换成下面的
        `_acreate_structured_summary_and_persist`。
        """
        messages = state["messages"]
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_messages(messages, cutoff_index)

        summary_text = await self._acreate_structured_summary_and_persist(
            messages_to_summarize, total_tokens, runtime
        )
        new_messages = self._build_new_messages(summary_text)

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *new_messages,
                *preserved_messages,
            ]
        }

    async def _acreate_structured_summary_and_persist(
        self,
        messages_to_summarize: list[AnyMessage],
        token_count_before: int,
        runtime: Runtime[ContextSchema],
    ) -> str:
        """生成结构化摘要、落库审计，返回格式化后的替换文本。

        落库的 `raw_messages` 是压缩前的完整消息（不是下面裁剪/清洗多模态内容之后
        的版本）——审计要看到"实际被压缩掉的是什么"，跟喂给摘要模型看的裁剪版本
        是两件事。
        """
        if not messages_to_summarize:
            return "暂无更早的对话历史。"

        trimmed = self._trim_messages_for_summary(messages_to_summarize)
        if not trimmed:
            # 基类 trim_messages（start_on="human"）把整段裁空时，仍然要落库：
            # 这恰恰是内容被压缩掉最多的一次，审计页面最需要看到"压缩前"是什么，
            # 不能因为没生成摘要就跳过记录。
            fallback_summary = ConversationSummarySchema(
                session_intent="（此前对话过长，未能生成摘要）"
            )
            await self.store.acreate_summary(
                thread_id=runtime.context.user_id or "debug",
                token_count_before=token_count_before,
                raw_messages=messages_to_dict(messages_to_summarize),
                summary=fallback_summary,
            )
            return render_summary_text(fallback_summary)
        sanitized = strip_multimodal_content(trimmed)

        raw_summary = await self._structured_summary_model.ainvoke(
            [JSON_MODE_SCHEMA_HINT, *sanitized, HumanMessage(content=SUMMARY_INSTRUCTION)]
        )
        if not isinstance(raw_summary, ConversationSummarySchema):
            msg = f"结构化摘要输出类型不符合预期：{type(raw_summary)!r}"
            raise TypeError(msg)

        await self.store.acreate_summary(
            thread_id=runtime.context.user_id or "debug",
            token_count_before=token_count_before,
            raw_messages=messages_to_dict(messages_to_summarize),
            summary=raw_summary,
        )
        return render_summary_text(raw_summary)
