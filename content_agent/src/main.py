import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import SecretStr
from langchain_openai import ChatOpenAI
from langchain.agents.middleware import AgentState, ModelRequest, before_agent, dynamic_prompt, wrap_tool_call
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore
from langsmith import traceable
from deepagents import create_deep_agent, FilesystemPermission
from deepagents.backends import CompositeBackend, FilesystemBackend, StoreBackend
from deepagents.middleware.subagents import SubAgent
from src.context import ContextSchema
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
backend = CompositeBackend(
    default=fs_backend,
    routes={
        "/memories/": StoreBackend(namespace=lambda rt: (rt.context.user_id or "debug",)),
    },
)

MEMORY_PATH = "/memories/AGENTS.md"

# agent 不再注册为 LangGraph 平台图（见 langgraph.json），而是由 webhook.py
# 直接 invoke，所以持久化（跨 thread 记忆 + 对话历史）需要自己管理，不能依赖
# 平台自动注入的 store/checkpointer。当前用内存实现，进程重启后数据会丢失；
# 如需重启后保留，替换为基于磁盘/数据库的 Store 与 Checkpointer 实现即可。
store = InMemoryStore()
checkpointer = InMemorySaver()


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
    middleware=[seed_default_memory, caller_prompt],
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
