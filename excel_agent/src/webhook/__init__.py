import contextlib

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.applications import Starlette

from src.main import DATA_DIR, build_agent
from src.webhook import _runtime
from src.webhook.admin import routes as _admin_routes
from src.webhook.whatsapp import lifespan as _whatsapp_lifespan, routes as _whatsapp_routes


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette):
    # AsyncSqliteSaver 要求构造时有运行中的事件循环，只能在这里（lifespan 内，
    # 事件循环已启动）构造，不能在 src/main.py 模块顶层构造（见该文件里
    # build_agent 上方的说明）。_runtime.agent 供 whatsapp.py/admin.py 在
    # 构造完成后取用最新值。
    async with AsyncSqliteSaver.from_conn_string(str(DATA_DIR / "checkpoints.sqlite")) as checkpointer:
        _runtime.agent = build_agent(checkpointer)
        async with _whatsapp_lifespan(app):
            yield


app = Starlette(routes=[*_whatsapp_routes, *_admin_routes], lifespan=_lifespan)
