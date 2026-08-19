"""进程内共享的运行时状态：全部由 lifespan 异步构建，运行期由这里持有。

模块级可变状态，构建前是 None，构建后由各路由模块和执行引擎用 `_runtime.xxx`
每次访问最新值，不能在模块顶层 `from src.agent_server.shared.runtime import agent`
后直接用这个名字（那样拿到的是构建之前的 None，不会跟着 lifespan 更新）。

pool 是给 channels/tob/routes.py 里"列出所有 thread"这条对 checkpoints 表的原始 SQL
用的 psycopg 连接池；runs_store 复用同一个 pool，不单独开连接。
"""

import asyncio
from typing import Any

agent: Any = None
store: Any = None
runs_store: Any = None
summaries_store: Any = None
pool: Any = None

# 同一个 thread_id 的 run 必须串行（并发写同一 thread 的 checkpoint 行为未定义），
# 且删除线程要等正在跑的 run 结束再删；channels/tob/routes.py 和 channels/whatsapp/
# processor.py 都要用同一份锁登记表，所以放在这个共享模块里，不是各自维护一份。
_thread_locks: dict[str, asyncio.Lock] = {}


def lock_for(thread_id: str) -> asyncio.Lock:
    return _thread_locks.setdefault(thread_id, asyncio.Lock())
