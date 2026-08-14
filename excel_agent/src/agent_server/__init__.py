"""agent-server 的 Starlette app + lifespan：Postgres 版 checkpointer/store/连接池
在这里异步构造（要求已有运行中的事件循环），构造完成后存进 _runtime 供各路由模块
用 `_runtime.xxx` 取最新值。
"""

import contextlib
import os

from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.store.postgres.base import PoolConfig
from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette

load_dotenv()

from src.agent.main import build_agent
from src.agent_server import _runtime
from src.agent_server.routes import routes
from src.agent_server.routes.webhook import lifespan as _whatsapp_lifespan
from src.agent_server.runs_store import RunsStore

DATABASE_URL = os.environ["DATABASE_URL"]


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette):
    async with (
        AsyncPostgresSaver.from_conn_string(DATABASE_URL) as checkpointer,
        AsyncPostgresStore.from_conn_string(
            DATABASE_URL, pool_config=PoolConfig(min_size=1, max_size=10)
        ) as store,
    ):
        await checkpointer.setup()
        await store.setup()

        # 单独一个连接池给 threads.py 的 checkpoints 表原始 SQL 查询、runs_store
        # 用，不复用 AsyncPostgresStore 内部管理的池（那个只服务 store 自己的方法）。
        pool: AsyncConnectionPool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)
        await pool.open()
        try:
            runs_store = RunsStore(pool)
            await runs_store.setup()

            _runtime.agent = build_agent(checkpointer, store)
            _runtime.store = store
            _runtime.runs_store = runs_store
            _runtime.pool = pool
            async with _whatsapp_lifespan(app):
                yield
        finally:
            await pool.close()


app = Starlette(
    routes=routes,
    lifespan=_lifespan,
)
