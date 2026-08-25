"""agent-server 的 Starlette app + lifespan：Postgres 版 checkpointer/store/连接池
在这里异步构造（要求已有运行中的事件循环），构造完成后存进 shared.runtime 供各路由
模块用 `_runtime.xxx` 取最新值。
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
from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.runs_store import RunsStore
from src.agent_server.shared.summaries_store import SummariesStore
from src.agent_server.channels import routes
from src.agent_server.channels.whatsapp.routes import lifespan as _whatsapp_lifespan
from src.agent_server.channels.whatsapp_meta.routes import lifespan as _whatsapp_meta_lifespan

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

        # 单独一个连接池给 channels/tob/{routes,admin}.py 的 checkpoints 表原始
        # SQL 查询、runs_store 用，不复用 AsyncPostgresStore 内部管理的池（那个
        # 只服务 store 自己的方法）。
        pool: AsyncConnectionPool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)
        await pool.open()
        try:
            runs_store = RunsStore(pool)
            await runs_store.setup()

            summaries_store = SummariesStore(pool)
            await summaries_store.setup()

            _runtime.agent = build_agent(checkpointer, store, summaries_store)
            _runtime.store = store
            _runtime.runs_store = runs_store
            _runtime.summaries_store = summaries_store
            _runtime.pool = pool
            async with _whatsapp_lifespan(app), _whatsapp_meta_lifespan(app):
                yield
        finally:
            await pool.close()


app = Starlette(
    routes=routes,
    lifespan=_lifespan,
)
