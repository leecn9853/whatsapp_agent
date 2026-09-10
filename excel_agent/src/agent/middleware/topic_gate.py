"""跑题拦截中间件：技能范围加载、gate prompt 拼装、历史清洗，以及驱动实际判断的
`create_topic_gate` 中间件。

`create_topic_gate(llm)` 而不是模块级现成的中间件实例：这次判断复用的是主 agent
同一个 `llm`，但 `llm` 定义在 `src/agent/main.py`，这个模块如果反过来 import 它会
和 main.py（import 本模块）互相循环 import，所以改成由 main.py 构造好 llm 后传进来。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from langchain.agents.middleware import AgentState, before_model
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.runtime import Runtime

from src.agent.middleware._multimodal import strip_multimodal_content
from src.context import ContextSchema

_OFF_TOPIC_REPLY = "问题可能超出我的知识范围，或者您可以把问题描述得更加具体一些。"

# 技能目录跟 docker-compose.yml 里挂进沙箱的 /workspace/skills 是同一份宿主机目录
# （见 `./src/agent/skills:/workspace/skills:ro`），这里直接读本地文件，不通过
# backend/沙箱那一套（避免依赖 Docker 可用性、避免依赖 SkillsMiddleware 的
# before_agent 是否已经在本轮跑过写好 state["skills_metadata"]）。topic_gate 拿到的
# 技能范围因此始终是"当前实际挂载的技能"，技能增减时不需要手动同步这段 prompt。
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _load_skill_summaries() -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    if not _SKILLS_DIR.is_dir():
        return summaries
    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", skill_md.read_text(encoding="utf-8"), re.DOTALL)
        if not match:
            continue
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(frontmatter, dict):
            continue
        name = str(frontmatter.get("name", "")).strip()
        description = str(frontmatter.get("description", "")).strip()
        if name and description:
            summaries.append({"name": name, "description": description})
    return summaries


_SKILL_SUMMARIES = _load_skill_summaries()


def _strip_tool_turns(messages: list[AnyMessage]) -> list[AnyMessage]:
    """去掉消息历史里的工具调用结构（AIMessage.tool_calls / ToolMessage），只保留人类
    可读的对话文本，供 topic_gate 这个不绑定任何工具的独立 LLM 调用使用。

    背景：一旦某个 thread 里执行过一次工具调用，state["messages"] 里就会混入真实的
    AIMessage.tool_calls / ToolMessage。topic_gate 的这次 ainvoke 没有声明任何 tools，
    但历史里这些结构会把 DeepSeek 带偏——它会尝试续写一次工具调用（用一种没有 tools
    声明时无法被正常解析的伪造格式文本），而不是老实回答 yes/no，导致 `on_topic`
    判断误把几乎所有请求当成"no"拒答，且这个偏差会在同一 thread 内一直持续（因为
    历史里的工具调用记录不会消失）。过滤掉这些结构后 topic_gate 才能稳定拿到 yes/no。
    """
    result: list[AnyMessage] = []
    for m in messages:
        if isinstance(m, ToolMessage):
            continue
        if isinstance(m, AIMessage) and m.tool_calls:
            if not m.content:
                continue
            m = m.model_copy(update={"tool_calls": []})
        result.append(m)
    return result


def _format_skills_for_gate(skills: list[dict[str, str]]) -> str:
    if not skills:
        return "（当前没有加载到任何技能说明）"
    return "\n".join(f"- {s['name']}：{s['description']}" for s in skills)


_TOPIC_GATE_SYSTEM_PROMPT = (
    "你的任务范围严格限定在下面这些技能覆盖的场景内：\n\n"
    f"{_format_skills_for_gate(_SKILL_SUMMARIES)}\n\n"
    "判断以下对话里用户最新这句话是否满足下面任一条件：\n"
    "1) 请求内容落在上面某个技能描述的场景范围内（包括基于前面上下文对已有任务的"
    "追问、调整）；\n"
    "2) 前面的对话里已经生成过报表/表格/图表，这句话是针对那份已生成结果的数据"
    "提问、解读、对比、筛选、排序等（例如\"哪个类别成功率最高\"\"这两个月比"
    "怎么样\"），即使措辞本身没有出现任何技能描述里的关键词，只要能看出是在问"
    "已生成结果里的内容，也算在范围内；\n"
    "3) 是打招呼、问候、寒暄、感谢、告别、确认收到、闲聊式的礼貌用语等社交性"
    "发言，且不构成一个具体的、超出上面技能范围的知识/信息请求。\n"
    "只要满足其中一条就回答 yes，否则回答 no。只回答 yes 或 no，不要解释。"
)


def create_topic_gate(llm):
    """构造 topic_gate 中间件：绑定传入的 `llm` 做判断，避免和 main.py 循环 import。"""

    @before_model(can_jump_to=["end"])
    async def topic_gate(
        state: AgentState, runtime: Runtime[ContextSchema]
    ) -> dict | None:  # noqa: ARG001
        """主模型被调用前先用一次不绑工具的独立 LLM 调用判断本轮是否跑题，跑题就
        jump_to="end"（system_prompt 里的规则单靠主模型自觉不可靠）。

        判断标准对照 `_SKILL_SUMMARIES` 而非关键词匹配；社交性发言、针对已生成报表的
        追问（即使措辞不命中技能描述）也算 on-topic，避免误拒。只在最后一条消息是刚发的
        HumanMessage 时判断一次，不重复判断同一轮的工具续跑。

        历史必须先过 `_strip_tool_turns`：这次调用没绑 tools，但历史里若有真实的
        AIMessage.tool_calls/ToolMessage，DeepSeek 会被带偏去续写一段无法解析的伪造
        工具调用而不是回答 yes/no，导致这个 thread 之后几乎全部被误判成"no"。
        """
        messages = state["messages"]
        if not messages or not isinstance(messages[-1], HumanMessage):
            return None

        reply = await llm.ainvoke(
            [
                SystemMessage(content=_TOPIC_GATE_SYSTEM_PROMPT),
                *_strip_tool_turns(strip_multimodal_content(messages)),
            ]
        )
        on_topic = str(reply.content).strip().lower().startswith("y")
        if on_topic:
            return None
        return {"jump_to": "end", "messages": [AIMessage(content=_OFF_TOPIC_REPLY)]}

    return topic_gate
