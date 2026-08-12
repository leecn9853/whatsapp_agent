"""本机专用的只读排障页面：看对话历史、看用户长期记忆。

替代原来的 scripts/inspect_data.py：那个脚本 `from src.main import agent, store`,
为了看一眼本地 sqlite 文件就要把整个生产 agent（真实 LLM 客户端、工具、subagents、
权限规则）构建出来,还得配好 DEEPSEEK_API_KEY；而且是另开一个进程直接打开
checkpoints.sqlite,和服务进程里 AsyncSqliteSaver 持有的长连接并发访问同一个文件,
有 "database is locked" 的隐患。

这里换成让服务进程自己暴露路由,直接复用已经在跑的那唯一一份 agent/store 实例——
不用重新构建、不用配 key、也没有第二个进程碰同一个文件。agent 由 lifespan 异步
构建后存在 src/webhook/_runtime.py 里，这里不能在模块顶层直接 import 它本身
（此时还没构建好），要用 `_runtime.agent` 每次访问最新值。

不对外暴露：即便 uvicorn 绑定的是 0.0.0.0,这里也在代码里显式校验
request.client.host 必须是本机 loopback 地址,不依赖网络拓扑/防火墙。
"""

from __future__ import annotations

import asyncio
import html
import sqlite3
from functools import wraps
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from src.main import DATA_DIR, store
from src.webhook import _runtime

CHECKPOINTS_DB = DATA_DIR / "checkpoints.sqlite"
MEMORY_DB = DATA_DIR / "memory_store.sqlite"
MEMORY_KEY = "/AGENTS.md"  # CompositeBackend 把 "/memories/" 前缀路由后剥掉了

_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def local_only(handler):
    @wraps(handler)
    async def wrapper(request: Request):
        host = request.client.host if request.client else None
        if host not in _LOOPBACK_HOSTS:
            return PlainTextResponse("Forbidden: admin routes are only accessible from localhost", status_code=403)
        return await handler(request)

    return wrapper


def _page(title: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 2rem; color: #222; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; }}
  a {{ color: #0645ad; }}
  form {{ margin: 1rem 0; }}
  pre {{ white-space: pre-wrap; word-break: break-word; }}
</style></head>
<body>
<nav><a href="/admin/">Admin home</a></nav>
<h1>{html.escape(title)}</h1>
{body_html}
</body></html>""")


@local_only
async def admin_index(_request: Request) -> HTMLResponse:
    body = """
    <ul>
      <li><a href="/admin/threads">Threads（对话历史）</a></li>
      <li><a href="/admin/users">Users（长期记忆）</a></li>
    </ul>
    """
    return _page("Admin", body)


def _fetch_threads() -> list[tuple[str, int]]:
    conn = sqlite3.connect(str(CHECKPOINTS_DB))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        return conn.execute(
            "SELECT thread_id, COUNT(*) AS n FROM checkpoints GROUP BY thread_id ORDER BY n DESC"
        ).fetchall()
    finally:
        conn.close()


@local_only
async def admin_threads(request: Request) -> HTMLResponse | RedirectResponse:
    user_id = request.query_params.get("user_id")
    if user_id:
        return RedirectResponse(f"/admin/threads/{quote(user_id, safe='')}")

    body = """<form method="get">
        <input name="user_id" placeholder="user_id, e.g. 12345@c.us">
        <button type="submit">查看对话</button>
    </form>"""

    if not CHECKPOINTS_DB.exists():
        return _page("Threads", body + "<p>暂无对话记录。</p>")

    rows = await asyncio.to_thread(_fetch_threads)
    if not rows:
        body += "<p>暂无对话记录。</p>"
    else:
        trs = "".join(
            f'<tr><td><a href="/admin/threads/{quote(thread_id, safe="")}">{html.escape(thread_id)}</a></td>'
            f"<td>{count}</td></tr>"
            for thread_id, count in rows
        )
        body += f"<table><tr><th>user_id</th><th>checkpoint 数</th></tr>{trs}</table>"
    return _page("Threads", body)


@local_only
async def admin_conversation(request: Request) -> HTMLResponse:
    user_id = request.path_params["user_id"]
    snapshot = await _runtime.agent.aget_state({"configurable": {"thread_id": user_id}})
    messages = snapshot.values.get("messages", [])

    if not messages:
        body = f"<p>没有找到 user_id={html.escape(user_id)} 的对话记录。</p>"
    else:
        trs = "".join(
            f"<tr><td>{html.escape(type(msg).__name__)}</td>"
            f"<td><pre>{html.escape(str(getattr(msg, 'content', msg)))}</pre></td></tr>"
            for msg in messages
        )
        body = f"<table><tr><th>Role</th><th>Content</th></tr>{trs}</table>"
    return _page(f"Conversation: {user_id}", body)


def _fetch_namespaces() -> list[str]:
    conn = sqlite3.connect(str(MEMORY_DB))
    try:
        return [row[0] for row in conn.execute("SELECT DISTINCT namespace FROM items ORDER BY namespace").fetchall()]
    finally:
        conn.close()


@local_only
async def admin_users(request: Request) -> HTMLResponse | RedirectResponse:
    user_id = request.query_params.get("user_id")
    if user_id:
        return RedirectResponse(f"/admin/users/{quote(user_id, safe='')}")

    body = """<form method="get">
        <input name="user_id" placeholder="user_id, e.g. 12345@c.us">
        <button type="submit">查看记忆</button>
    </form>"""

    if not MEMORY_DB.exists():
        return _page("Users", body + "<p>暂无用户记忆。</p>")

    namespaces = await asyncio.to_thread(_fetch_namespaces)
    if not namespaces:
        body += "<p>暂无用户记忆。</p>"
    else:
        lis = "".join(
            f'<li><a href="/admin/users/{quote(ns, safe="")}">{html.escape(ns)}</a></li>' for ns in namespaces
        )
        body += f"<ul>{lis}</ul>"
    return _page("Users", body)


@local_only
async def admin_memory(request: Request) -> HTMLResponse:
    user_id = request.path_params["user_id"]
    # user_id 传原始值即可（比如带句点的 "12345@c.us"），这里按 src/main.py 里
    # namespace 工厂用的同一种规则（句点替换成下划线）转换后再查找；对已经是
    # 安全 namespace 值（从 /admin/users 表格点进来）的输入，.replace 是空操作。
    namespace = (user_id.replace(".", "_"),)
    item = await store.aget(namespace, MEMORY_KEY)
    if item is None:
        body = f"<p>没有找到 namespace={html.escape(str(namespace))} 对应的记忆。</p>"
    else:
        body = f"<pre>{html.escape(item.value['content'])}</pre>"
    return _page(f"Memory: {user_id}", body)


routes = [
    Route("/admin/", admin_index),
    Route("/admin/threads", admin_threads),
    Route("/admin/threads/{user_id}", admin_conversation),
    Route("/admin/users", admin_users),
    Route("/admin/users/{user_id}", admin_memory),
]
