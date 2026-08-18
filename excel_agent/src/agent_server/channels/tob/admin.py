"""toB 内部查看页面：给开发/维护人员用，排查所有渠道（WhatsApp + toB）的对话、执行
记录、长期记忆，不做渠道过滤——这是运维排查用的，需要看到全部数据，跟 routes.py 里
"调用方只能看自己 tob: 前缀数据"的隔离要求不一样，所以故意分成两个文件，以后各自升级
鉴权互不影响（比如 admin.py 以后要换成运营/客服账号体系，routes.py 换成真正的外部
toB 鉴权）。

页面本身是纯静态 HTML + fetch，不引入任何前端框架/构建步骤：列表页显示全部 thread，
点进详情页分三块——对话历史（复用 aget_state 拿到的 messages）、run 记录（状态/
attempt/失败原因/起止时间，来自 alist_runs_for_thread）、长期记忆（/memories/ 写入
的 AGENTS.md 内容，来自 shared/memory.py 的 aget_memory）。这个查记忆的能力原来是
顶层通用调试接口 /v1/memories*，现在收进 toB 的查看页面——WhatsApp 没有界面消费这
个数据，不需要单独开路由；toC 以后要给用户自己看记忆的话，应该复用 shared/memory.py，
不要重新拼 namespace escaping 规则。
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.memory import aget_memory
from src.agent_server.shared.security import local_only


@local_only
async def list_threads(_request: Request) -> JSONResponse:
    async with _runtime.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT thread_id, COUNT(*) AS n FROM checkpoints GROUP BY thread_id ORDER BY n DESC"
            )
            rows = await cur.fetchall()
    return JSONResponse([{"thread_id": thread_id, "checkpoint_count": n} for thread_id, n in rows])


@local_only
async def get_state(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    snapshot = await _runtime.agent.aget_state({"configurable": {"thread_id": thread_id}})
    messages = snapshot.values.get("messages", [])
    return JSONResponse(
        {
            "messages": [
                {"role": type(m).__name__, "content": str(getattr(m, "content", m))}
                for m in messages
            ]
        }
    )


@local_only
async def get_runs(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    runs = await _runtime.runs_store.alist_runs_for_thread(thread_id)
    return JSONResponse(runs)


@local_only
async def get_memory(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    content = await aget_memory(_runtime.store, thread_id)
    return JSONResponse({"content": content})


_ADMIN_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>toB 查看页面</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 0; display: flex; height: 100vh; }
  #list { width: 320px; overflow-y: auto; border-right: 1px solid #ddd; flex-shrink: 0; }
  #list ul { list-style: none; margin: 0; padding: 0; }
  #list li { padding: 8px 12px; border-bottom: 1px solid #eee; cursor: pointer; font-size: 13px; word-break: break-all; }
  #list li:hover, #list li.active { background: #f0f4ff; }
  #detail { flex: 1; overflow-y: auto; padding: 16px 24px; }
  h2 { font-size: 15px; color: #555; margin: 20px 0 8px; }
  .msg { padding: 8px 10px; margin-bottom: 6px; border-radius: 6px; background: #f5f5f5; white-space: pre-wrap; }
  .msg .role { font-weight: 600; font-size: 12px; color: #888; margin-bottom: 2px; }
  .memory { padding: 10px; background: #fafaf0; border: 1px solid #eee; border-radius: 6px; white-space: pre-wrap; font-size: 13px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; }
  th { background: #fafafa; }
  .status-success { color: #2a8a2a; }
  .status-error { color: #c33; }
  .status-cancelled { color: #999; }
  .empty { color: #999; padding: 20px; }
</style>
</head>
<body>
<div id="list"><ul id="thread-list"></ul></div>
<div id="detail"><p class="empty">从左侧选择一个 thread</p></div>
<script>
async function loadThreads() {
  const res = await fetch('/v1/tob/admin/threads');
  const threads = await res.json();
  const ul = document.getElementById('thread-list');
  ul.innerHTML = '';
  for (const t of threads) {
    const li = document.createElement('li');
    li.textContent = `${t.thread_id} (${t.checkpoint_count})`;
    li.onclick = () => selectThread(t.thread_id, li);
    ul.appendChild(li);
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function selectThread(threadId, li) {
  document.querySelectorAll('#thread-list li').forEach(el => el.classList.remove('active'));
  li.classList.add('active');

  const detail = document.getElementById('detail');
  detail.innerHTML = '<p class="empty">加载中…</p>';

  const [stateRes, runsRes, memoryRes] = await Promise.all([
    fetch(`/v1/tob/admin/threads/${encodeURIComponent(threadId)}/state`),
    fetch(`/v1/tob/admin/threads/${encodeURIComponent(threadId)}/runs`),
    fetch(`/v1/tob/admin/threads/${encodeURIComponent(threadId)}/memory`),
  ]);
  const state = await stateRes.json();
  const runs = await runsRes.json();
  const memory = await memoryRes.json();

  let html = `<h2>对话历史 — ${escapeHtml(threadId)}</h2>`;
  if (state.messages.length === 0) {
    html += '<p class="empty">无对话记录</p>';
  } else {
    for (const m of state.messages) {
      html += `<div class="msg"><div class="role">${escapeHtml(m.role)}</div>${escapeHtml(m.content)}</div>`;
    }
  }

  html += '<h2>Run 记录</h2>';
  if (runs.length === 0) {
    html += '<p class="empty">无 run 记录</p>';
  } else {
    html += '<table><tr><th>run_id</th><th>状态</th><th>attempt</th><th>失败原因</th><th>创建时间</th><th>更新时间</th></tr>';
    for (const r of runs) {
      html += `<tr><td>${escapeHtml(r.run_id)}</td><td class="status-${escapeHtml(r.status)}">${escapeHtml(r.status)}</td>` +
        `<td>${r.attempt}</td><td>${escapeHtml(r.error || '')}</td>` +
        `<td>${escapeHtml(r.created_at)}</td><td>${escapeHtml(r.updated_at)}</td></tr>`;
    }
    html += '</table>';
  }

  html += '<h2>长期记忆</h2>';
  html += memory.content
    ? `<div class="memory">${escapeHtml(memory.content)}</div>`
    : '<p class="empty">无长期记忆</p>';

  detail.innerHTML = html;
}

loadThreads();
</script>
</body>
</html>
"""


@local_only
async def admin_page(_request: Request) -> HTMLResponse:
    return HTMLResponse(_ADMIN_PAGE)


routes = [
    Route("/v1/tob/admin", admin_page),
    Route("/v1/tob/admin/threads", list_threads),
    Route("/v1/tob/admin/threads/{thread_id}/state", get_state),
    Route("/v1/tob/admin/threads/{thread_id}/runs", get_runs),
    Route("/v1/tob/admin/threads/{thread_id}/memory", get_memory),
]
