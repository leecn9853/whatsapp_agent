import os
import warnings
from pathlib import Path
from dotenv import load_dotenv
from pydantic import SecretStr
from langchain_openai import ChatOpenAI
from langchain.agents.middleware import (
    AgentState,
    ContextEditingMiddleware,
    ModelRequest,
    SummarizationMiddleware,
    before_agent,
    before_model,
    dynamic_prompt,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from deepagents import create_deep_agent, FilesystemPermission
from deepagents.backends import CompositeBackend, FilesystemBackend, StoreBackend
from deepagents.middleware.subagents import SubAgent
from src.context import ContextSchema
from src.agent.stores.sqlite_store import SqliteStore
from src.agent.tools.excel_tools import (
    aggregate_excel_sheet,
    create_chart_sheet,
    inspect_excel,
    list_excel_files,
)
from src.agent.tools.save_file import save_file
from src.agent.tools.tavily_search import web_search

# LangGraph 在把我们传入的 context=ContextSchema(...) 序列化进 checkpoint/tracing
# 时会触发这条警告，纯粹是噪音——不影响 context 实际的传递和使用，只是刷屏。
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

# 在初始化任何 LangChain / DeepAgents 实例之前，先加载 .env 环境变量
load_dotenv()

deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
if not deepseek_api_key:
    raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置，请检查 .env 文件")

# 初始化 DeepSeek-V4 模型 (使用 OpenAI 兼容 API 接入)
llm = ChatOpenAI(
    model="deepseek-v4-flash",  # 指定为 DeepSeek 模型名称
    api_key=SecretStr(deepseek_api_key),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
    temperature=0.3,
)

# 工具列表
tools = [
    web_search,
    save_file,
    list_excel_files,
    inspect_excel,
    aggregate_excel_sheet,
    create_chart_sheet,
]

# 中间件：拦截并记录每一次工具调用（横切关注点示例）
call_count = [0]


@dynamic_prompt
def caller_prompt(request: ModelRequest[ContextSchema]) -> str:
    caller = request.runtime.context.caller
    print(f"[Middleware] caller: {caller}")
    return f"[Middleware] caller: {caller}"


# 子代理：通过网络搜索获取最新信息的子代理，通过 task 工具由主代理按需委托调用
web_search_subagent: SubAgent = {
    "name": "web-search-agent",
    "description": "用于通过网络搜索获取最新信息，适合需要多轮联网搜索、交叉验证信息的调研任务",
    "system_prompt": "你是一名网络信息捕手，擅长通过多轮联网搜索深入调研问题，并在结论中注明信息来源链接。",
    "tools": [web_search],
    # 未指定 model，默认继承主代理的模型 (llm)
}
subagents = [web_search_subagent]

# skills/ 目录需要挂载到真实磁盘才能被 SkillsMiddleware 读取（默认的内存态
# StateBackend 读不到磁盘文件）。root_dir 限定在 src/ 一级，避免把项目根目录下的
# .env、out_files/ 暴露给内置文件工具。
SRC_DIR = Path(__file__).resolve().parent
fs_backend = FilesystemBackend(root_dir=SRC_DIR)

# /memories/ 路由到 StoreBackend，按 user_id 隔离，实现跨 thread 的用户专属记忆；
# 其余路径（skills 等）仍走磁盘 FilesystemBackend。
# WhatsApp 的 user_id（如 "12345@c.us"）带句点，而 LangGraph store 的命名空间
# 标签不允许包含句点，所以这里要替换掉，否则真实用户消息一律会报
# InvalidNamespaceError。
backend = CompositeBackend(
    default=fs_backend,
    routes={
        "/memories/": StoreBackend(
            namespace=lambda rt: ((rt.context.user_id or "debug").replace(".", "_"),)
        ),
    },
)

MEMORY_PATH = "/memories/AGENTS.md"

# agent 不注册为 LangGraph 平台图，而是由 webhook 包直接 invoke，所以持久化
# （跨 thread 记忆 + 对话历史）需要自己管理，不能依赖平台自动注入的
# store/checkpointer。
DATA_DIR = SRC_DIR.parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# store：管 /memories/ 下按 user_id 隔离的跨对话长期记忆。官方没有现成的 SQLite
# Store 实现（只有内存版和 Postgres 版），这里用 src/agent/stores/sqlite_store.py 里
# 自己写的简易版本（细节和限制见该文件的模块说明）。
store = SqliteStore(DATA_DIR / "memory_store.sqlite")


@before_agent
def seed_default_memory(
    state: AgentState, runtime: Runtime[ContextSchema]
) -> None:  # noqa: ARG001
    """新用户第一次对话、还没有专属记忆时，把磁盘上的默认模板写入其记忆。

    之后完全由 Agent 通过 edit_file 自行维护，这里只负责起点。
    """
    if backend.read(MEMORY_PATH).error is None:
        return None  # 该用户已经有记忆了，不覆盖

    template = fs_backend.read(MEMORY_PATH)  # 读磁盘上的默认模板 src/memories/AGENTS.md
    if template.error is not None or template.file_data is None:
        return None

    backend.write(MEMORY_PATH, template.file_data["content"])
    return None


_OFF_TOPIC_REPLY = "这个问题超出我的职责范围（我只处理 Excel 表格/图表相关的请求），建议换用其他 AI 助手咨询。"

_TOPIC_GATE_SYSTEM_PROMPT = (
    "判断以下对话里用户最新这句话是否是 Excel 表格处理/图表生成相关的请求"
    "（包括基于前面上下文对已有任务的追问、调整）。只回答 yes 或 no，不要解释。"
)


@before_model(can_jump_to=["end"])
async def topic_gate(
    state: AgentState, runtime: Runtime[ContextSchema]
) -> dict | None:  # noqa: ARG001
    """在主模型（带 web_search/task 等工具）被调用前，先做一次不绑工具的范围判断。

    system_prompt 里"只处理 Excel 任务"这条规则单靠主模型自觉并不可靠——deepseek-v4-flash
    看到像"台湾的大学怎么样"这类事实性问题，会倾向于直接调 web_search，覆盖掉规则本身。
    这里用同一个 llm 客户端单独问一句 yes/no（这次调用没有绑定任何工具，天然不会调用
    web_search），判定跑题就直接 jump_to="end"，主模型和工具全程不会被调用。

    只在本轮第一次进入模型时判断（即最后一条消息是用户刚发的 HumanMessage），避免对
    同一轮里"工具结果之后的续跑"重复判断，误伤正在执行中的正常 Excel 任务。
    """
    messages = state["messages"]
    if not messages or not isinstance(messages[-1], HumanMessage):
        return None

    reply = await llm.ainvoke(
        [SystemMessage(content=_TOPIC_GATE_SYSTEM_PROMPT), *messages]
    )
    on_topic = str(reply.content).strip().lower().startswith("y")
    if on_topic:
        return None
    return {"jump_to": "end", "messages": [AIMessage(content=_OFF_TOPIC_REPLY)]}


# checkpointer 管每个 user_id（= thread_id）的对话历史/运行状态（messages、
# todos、待处理的工具调用等）：
# - 触发/流程完全是 LangGraph 内置的，不需要也不应该自己手动调用：每次
#   agent.invoke 开始时，自动按 thread_id 取最新一条 checkpoint 恢复状态；图的
#   每一步（每个节点跑完）自动落盘一次；下一次同 thread_id 的请求自动接着最新
#   状态续跑。
# - 官方的持久化 SqliteSaver 只有同步接口（aget_tuple/aput/aput_writes/alist
#   全部 raise NotImplementedError），而异步版 AsyncSqliteSaver 要求构造时
#   已经有一个运行中的事件循环（__init__ 里调用 asyncio.get_running_loop()）。
#   本模块在 uvicorn 事件循环启动之前就被 import，所以这里不能直接构造
#   checkpointer，只能提供 build_agent(checkpointer) 工厂函数，交给
#   src/webhook/__init__.py 的 Starlette lifespan（此时事件循环已经在跑）
#   用 AsyncSqliteSaver.from_conn_string(...) 构造好之后再传进来。
# - 增长问题：deepagents 的 DeepAgentState 已经给 messages 字段配了
#   DeltaChannel（见 deepagents/graph.py），每一步存的是增量而不是全量快照
#   （每 50 步才存一次完整快照），把 checkpoint 存储量从 O(对话轮数²) 降到了
#   O(对话轮数)，所以换成持久化存储不会引入"越聊越大"的爆炸式增长。LangGraph
#   新增了 BaseCheckpointSaver.prune() 用于手动裁剪旧 checkpoint，但目前装的
#   版本里没有任何 checkpointer 实现它（包括 InMemorySaver），且其文档明确警告
#   对使用 DeltaChannel 的图做朴素裁剪会悄悄弄断历史链、丢数据——所以这里不额外
#   加自定义的裁剪/删除逻辑，等真的遇到数据库文件过大再处理。
def build_agent(checkpointer):
    """创建 DeepAgent 实例。checkpointer 由调用方（webhook lifespan）异步构造后传入。

    deepagents 内部会自动注入：
    - 任务规划中间件 (TodoListMiddleware / write_todos)
    - 文件系统中间件 (基于上面的 CompositeBackend；真实文件落盘统一走
      上面的 save_file 工具，所以下面用 permissions 禁掉了内置工具在 /memories/
      之外的写权限)
    - 记忆中间件 (MemoryMiddleware，由 memory= 参数触发，把 /memories/AGENTS.md
      的内容注入系统提示，并允许 Agent 通过 edit_file 自主更新)
    """
    return create_deep_agent(
        model=llm,
        tools=tools,
        middleware=[
            # topic_gate 放最前面：跑题时要在 ContextEditingMiddleware/Summarization
            # 这些为"正常 Excel 任务"服务的中间件跑之前就 jump_to="end"，避免它们
            # 对一条马上要被拒答的消息做无意义的加工。
            topic_gate,
            # 例子：控制单个 thread（user_id）的上下文体量，避免陪聊越久单次调用
            # token 越贵、最终超出模型上下文窗口。两个中间件管的是不同层面的膨胀，
            # 按下面的顺序叠加：先精简工具结果，再对消息本身做摘要。
            #
            # 1) ContextEditingMiddleware：只清理"工具调用的返回结果"（比如
            #    web_search 返回的长网页正文），不动对话本身。累计输入 token 超过
            #    阈值时，把较早的工具结果替换成占位符。对应 Anthropic
            #    clear_tool_uses 的思路，用默认配置即可。
            ContextEditingMiddleware(),
            # 2) SummarizationMiddleware：管对话消息本身。累计 token 数超过
            #    trigger 阈值时，自动把较早的消息压缩成一段摘要，保留最近 keep
            #    条原始消息，AI/Tool 消息对不会被拆散。
            #    trigger=("tokens", 4000)：超过 4000 token 才触发，避免正常的
            #    短对话被频繁摘要；keep=("messages", 20)：摘要后至少保留最近
            #    20 条原始消息。这里已经有 /memories/AGENTS.md 承担"值得长期
            #    记住的信息"，所以原始聊天记录被摘掉不会丢失真正重要的内容。
            SummarizationMiddleware(
                model=llm, trigger=("tokens", 4000), keep=("messages", 30)
            ),
            seed_default_memory,
            caller_prompt,
        ],
        subagents=subagents,
        skills=["./skills/"],
        backend=backend,
        memory=["/memories/AGENTS.md"],
        context_schema=ContextSchema,
        store=store,
        checkpointer=checkpointer,
        permissions=[
            # /memories/ 需要保留写权限，否则 memory 中间件的 edit_file 会被下面的
            # 全局 deny 规则拦住，规则按声明顺序匹配，第一条命中的生效。
            FilesystemPermission(
                operations=["write"], paths=["/memories/**"], mode="allow"
            ),
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ],
        system_prompt="""你是一个通过 WhatsApp 与用户对话的 Excel 数据处理与图表助手，
擅长读懂表格结构、按需聚合数据、在表格里生成图表（具体规范见已挂载的技能文件）。

【任务范围（硬性红线）】
只处理和 Excel 表格处理/图表生成相关的请求。凡是与此无关的内容——不论具体是
时事新闻、天气、其他领域知识问答、闲聊还是别的什么——都不要调用任何工具，不要
展开回答，用一两句话简短说明这不是你的职责范围，并提示对方可以用其他 AI 助手
获取相关信息，到此结束。

【任务执行】
1. 优先使用 write_todos 工具将复杂任务拆解为多步规划。
2. 涉及 Excel 表格/图表/汇总相关的任务时，先用 read_file 读取 excel-chart 技能的
   完整说明并照做——不确定文件名/sheet、要不要先聚合、选什么图表类型、多个工具
   之间怎么链式调用，这些判断逻辑和操作顺序都写在该技能里，这里不重复。
3. 只在完成 Excel 任务本身确实需要时才使用 web_search 工具联网搜索（比如任务里
   提到的某个行业术语、当前汇率/指标等）；遇到需要多轮搜索、交叉验证的深入调研
   任务时，通过 task 工具委托给 web-search-agent 子代理完成，再基于其结论继续
   后续步骤。不是为了回答用户随口提出的、和当前 Excel 任务无关的问题去联网——
   那种情况按上面的【任务范围】处理。

【输出规范】
- 回复内容会直接作为 WhatsApp 消息发给用户，只呈现结果和必要的自然语言说明，
  保持简洁、分段清晰；不要向用户提及你使用了什么工具、调用了什么函数、内部
  经过了哪些处理步骤。
- 处理 Excel 任务时产出的文件本身就是交付物，聚合/生成图表后会自动发给用户，
  不需要用户额外说"保存"。仅当用户想要的是纯文字内容（不是表格文件）时才需要
  save_file，同样只在用户明确要求保存/导出时调用。

【记忆策略】
你的记忆（memory）是按当前这个 WhatsApp 联系人隔离保存的。当了解到该用户
称呼、常处理的表格/部门场景、图表与聚合口味偏好等跨对话仍有参考价值的信息时，
主动更新记忆，方便下次这个人来聊天时直接接续上下文；不要记录一次性的、临时的
请求。
""",
    )
