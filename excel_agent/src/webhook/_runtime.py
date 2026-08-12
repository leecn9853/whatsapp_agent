"""进程内共享的运行时状态：agent 在 lifespan 启动时才异步构建，运行期由这里持有。

whatsapp.py/admin.py 不能 `from src.webhook._runtime import agent` 后直接用这个
名字——那样绑定的是 lifespan 运行前的旧值（此时还是 None），之后 lifespan 里
`_runtime.agent = ...` 的重新赋值它们看不到。必须 `import src.webhook._runtime as
_runtime`，每次用时写 `_runtime.agent`，才能读到最新值。
"""

from typing import Any

agent: Any = None
