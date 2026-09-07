from dataclasses import dataclass


@dataclass
class ContextSchema:
    """Agent 运行时上下文：区分调用来源（调试/WhatsApp）与用户身份。

    通过 create_deep_agent(context_schema=ContextSchema) 注册后，
    在 agent.invoke/astream 时传入 context=ContextSchema(...)，
    工具里用 runtime: ToolRuntime 注入后读 runtime.context 即可拿到。
    """

    caller: str = "debug"
    user_id: str | None = None
