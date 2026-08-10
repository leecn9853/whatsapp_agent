import os
import sqlite3
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
    dynamic_prompt,
)
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.runtime import Runtime
from deepagents import create_deep_agent, FilesystemPermission
from deepagents.backends import CompositeBackend, FilesystemBackend, StoreBackend
from deepagents.middleware.subagents import SubAgent
from src.context import ContextSchema
from src.stores.sqlite_store import SqliteStore
from src.tools.save_file import save_file
from src.tools.tavily_search import web_search

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
tools = [web_search, save_file]

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
# WhatsApp 的 chat_id（如 "12345@c.us"）带句点，而 LangGraph store 的命名空间
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

# agent 不注册为 LangGraph 平台图，而是由 webhook.py 直接 invoke，所以持久化
# （跨 thread 记忆 + 对话历史）需要自己管理，不能依赖平台自动注入的
# store/checkpointer。
DATA_DIR = SRC_DIR.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# checkpointer：管每个 chat_id（= thread_id）的对话历史/运行状态（messages、
# todos、待处理的工具调用等）。
# - 触发/流程完全是 LangGraph 内置的，不需要也不应该自己手动调用：每次
#   agent.invoke 开始时，自动按 thread_id 取最新一条 checkpoint 恢复状态；图的
#   每一步（每个节点跑完）自动落盘一次；下一次同 thread_id 的请求自动接着最新
#   状态续跑。这里只是把默认的 InMemorySaver 换成落盘的 SqliteSaver，触发方式
#   本身没有变化。
# - 增长问题：deepagents 的 DeepAgentState 已经给 messages 字段配了
#   DeltaChannel（见 deepagents/graph.py），每一步存的是增量而不是全量快照
#   （每 50 步才存一次完整快照），把 checkpoint 存储量从 O(对话轮数²) 降到了
#   O(对话轮数)，所以换成持久化存储不会引入"越聊越大"的爆炸式增长。LangGraph
#   新增了 BaseCheckpointSaver.prune() 用于手动裁剪旧 checkpoint，但目前装的
#   版本里没有任何 checkpointer 实现它（包括 InMemorySaver），且其文档明确警告
#   对使用 DeltaChannel 的图做朴素裁剪会悄悄弄断历史链、丢数据——所以这里不额外
#   加自定义的裁剪/删除逻辑，等真的遇到数据库文件过大再处理。
checkpointer = SqliteSaver(sqlite3.connect(str(DATA_DIR / "checkpoints.sqlite"), check_same_thread=False))

# store：管 /memories/ 下按 user_id 隔离的跨对话长期记忆。官方没有现成的 SQLite
# Store 实现（只有内存版和 Postgres 版），这里用 src/stores/sqlite_store.py 里
# 自己写的简易版本（细节和限制见该文件的模块说明）。
store = SqliteStore(DATA_DIR / "memory_store.sqlite")


@before_agent
def seed_default_memory(state: AgentState, runtime: Runtime[ContextSchema]) -> None:  # noqa: ARG001
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


# 创建 DeepAgent 实例
# deepagents 内部会自动注入：
# - 任务规划中间件 (TodoListMiddleware / write_todos)
# - 文件系统中间件 (基于上面的 CompositeBackend；真实文件落盘统一走
#   上面的 save_file 工具，所以下面用 permissions 禁掉了内置工具在 /memories/
#   之外的写权限)
# - 记忆中间件 (MemoryMiddleware，由 memory= 参数触发，把 /memories/AGENTS.md
#   的内容注入系统提示，并允许 Agent 通过 edit_file 自主更新)
agent = create_deep_agent(
    model=llm,
    tools=tools,
    middleware=[
        # 例子：控制单个 thread（chat_id）的上下文体量，避免陪聊越久单次调用
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
        SummarizationMiddleware(model=llm, trigger=("tokens", 4000), keep=("messages", 30)),
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
        FilesystemPermission(operations=["write"], paths=["/memories/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ],
    system_prompt="""你是一个通过 WhatsApp 与用户对话的内容创作助手，
擅长人际沟通、情商相关的内容创作（具体写作规范见已挂载的技能文件）。
执行任务时，请遵守以下流程：
1. 优先使用 write_todos 工具将复杂任务拆解为多步规划。
2. 如果技能列表中有匹配当前话题的技能，先用 read_file 读取该技能的完整说明并照做。
3. 遇到需要最新信息或外部事实时，使用 web_search 工具进行联网搜索；遇到需要多轮搜索、
   交叉验证的深入调研任务时，通过 task 工具委托给 web-search-agent 子代理完成，
   再基于其结论继续后续步骤。
4. 回复内容会直接作为 WhatsApp 消息发给用户，保持简洁、分段清晰，不要写成大段无重点的文字。
5. 仅当用户在本次任务中明确要求生成/保存/导出文件时，才调用 save_file
   工具保存最终内容；未被要求时不要主动创建任何文件。该工具会自动写入
   output 目录并自动生成带时间戳的文件名，无需自己处理路径或重名问题。
6. 你的记忆（memory）是按当前这个 WhatsApp 联系人隔离保存的。当了解到该用户
   反复出现的具体人际情况、称呼、语气偏好等跨对话仍有参考价值的信息时，主动
   更新记忆，方便下次这个人来聊天时直接接续上下文；不要记录一次性的、临时的
   请求。
""",
)
