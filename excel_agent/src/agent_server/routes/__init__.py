"""聚合 agent_server 全部路由：/v1/threads*、/v1/memories*、/webhook。

新增路由模块时，在这里加一行 `from ... import routes as _xxx_routes` 并塞进下面的
列表，根目录 `__init__.py` 只需要 `from src.agent_server.routes import routes` 一次。
"""

from __future__ import annotations

from src.agent_server.routes.memories import routes as _memories_routes
from src.agent_server.routes.runs import routes as _runs_routes
from src.agent_server.routes.threads import routes as _threads_routes
from src.agent_server.routes.webhook import routes as _webhook_routes

routes = [*_runs_routes, *_threads_routes, *_memories_routes, *_webhook_routes]
