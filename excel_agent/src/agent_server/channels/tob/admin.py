"""toB 内部查看页面：给开发/维护人员用，排查所有渠道（WhatsApp + toB）的对话、执行
记录、长期记忆，不做渠道过滤——这是运维排查用的，需要看到全部数据，跟 routes.py 里
"调用方只能看自己 tob: 前缀数据"的隔离要求不一样，所以故意分成两个文件，以后各自升级
鉴权互不影响（比如 admin.py 以后要换成运营/客服账号体系，routes.py 换成真正的外部
toB 鉴权）。

页面本身是纯静态 HTML + fetch，不引入任何前端框架/构建步骤：列表页显示全部 thread，
点进详情页分四块——对话历史（复用 aget_state 拿到的 messages，超长时容器内滚动，
不撑爆整页）、摘要记录（ConversationSummaryAuditMiddleware 触发压缩时落库的结构化
摘要，来自 shared/summaries_store.py 的 alist_summaries_for_thread，没有触发过就
不显示）、run 记录（状态/attempt/失败原因/起止时间，来自 alist_runs_for_thread）、
长期记忆（/memories/ 写入的 AGENTS.md 内容，来自 shared/memory.py 的 aget_memory）。
这个查记忆的能力原来是顶层通用调试接口 /v1/memories*，现在收进 toB 的查看页面——
WhatsApp 没有界面消费这个数据，不需要单独开路由；toC 以后要给用户自己看记忆的话，
应该复用 shared/memory.py，不要重新拼 namespace escaping 规则。

messages 的序列化逻辑（含 tool_calls/artifact）放在 shared/messages.py，routes.py
的对外 state 接口复用同一份，避免两处字段定义分叉。
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.memory import aget_memory
from src.agent_server.shared.messages import serialize_message
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
    return JSONResponse({"messages": [serialize_message(m) for m in messages]})


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


@local_only
async def get_summaries(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    summaries = await _runtime.summaries_store.alist_summaries_for_thread(thread_id)
    return JSONResponse(summaries)


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
  #history { max-height: 420px; overflow-y: auto; border: 1px solid #eee; border-radius: 6px; padding: 8px; }
  .msg { padding: 8px 10px; margin-bottom: 6px; border-radius: 6px; background: #f5f5f5; white-space: pre-wrap; }
  .msg .role { font-weight: 600; font-size: 12px; color: #888; margin-bottom: 2px; }
  .tool-call { font-size: 12px; color: #555; margin-top: 4px; }
  .tool-artifact { font-size: 12px; color: #a66; margin-top: 4px; font-family: monospace; }
  .memory { padding: 10px; background: #fafaf0; border: 1px solid #eee; border-radius: 6px; white-space: pre-wrap; font-size: 13px; }
  .summary-card { padding: 10px; background: #f0f8ff; border: 1px solid #dce8f5; border-radius: 6px; margin-bottom: 8px; font-size: 13px; }
  .summary-card dt { font-weight: 600; color: #567; margin-top: 6px; }
  .summary-card dt:first-child { margin-top: 0; }
  .summary-card dd { margin: 2px 0 0 0; white-space: pre-wrap; }
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

  const [stateRes, runsRes, memoryRes, summariesRes] = await Promise.all([
    fetch(`/v1/tob/admin/threads/${encodeURIComponent(threadId)}/state`),
    fetch(`/v1/tob/admin/threads/${encodeURIComponent(threadId)}/runs`),
    fetch(`/v1/tob/admin/threads/${encodeURIComponent(threadId)}/memory`),
    fetch(`/v1/tob/admin/threads/${encodeURIComponent(threadId)}/summaries`),
  ]);
  const state = await stateRes.json();
  const runs = await runsRes.json();
  const memory = await memoryRes.json();
  const summaries = await summariesRes.json();

  let html = `<h2>对话历史 — ${escapeHtml(threadId)}</h2>`;
  if (state.messages.length === 0) {
    html += '<p class="empty">无对话记录</p>';
  } else {
    html += '<div id="history">';
    for (const m of state.messages) {
      let body = escapeHtml(m.content);
      if (m.tool_calls && m.tool_calls.length > 0) {
        body += m.tool_calls.map(tc =>
          `<div class="tool-call">调用工具 <b>${escapeHtml(tc.name || '')}</b>(${escapeHtml(JSON.stringify(tc.args || {}))})</div>`
        ).join('');
      }
      if (m.role === 'ToolMessage') {
        body = `<div class="tool-call">工具 <b>${escapeHtml(m.tool_name || '')}</b> 返回：</div>` + body;
        if (m.artifact) {
          body += `<div class="tool-artifact">artifact: ${escapeHtml(m.artifact)}</div>`;
        }
      }
      html += `<div class="msg"><div class="role">${escapeHtml(m.role)}</div>${body}</div>`;
    }
    html += '</div>';
  }

  if (summaries.length > 0) {
    html += '<h2>摘要记录</h2>';
    for (const s of summaries) {
      html += '<div class="summary-card"><dl>' +
        `<dt>会话意图</dt><dd>${escapeHtml(s.session_intent || '')}</dd>` +
        `<dt>涉及表格</dt><dd>${escapeHtml((s.excel_context || []).join('、') || '（无）')}</dd>` +
        `<dt>已确定的结论/偏好</dt><dd>${escapeHtml((s.decisions || []).join('\\n') || '（无）')}</dd>` +
        `<dt>待完成事项</dt><dd>${escapeHtml((s.next_steps || []).join('\\n') || '（无）')}</dd>` +
        `<dt>生成/引用的文件</dt><dd>${escapeHtml((s.artifacts || []).join('\\n') || '（无）')}</dd>` +
        `<dt>触发时 token 数 / 时间</dt><dd>${s.token_count_before} / ${escapeHtml(s.created_at)}</dd>` +
        '</dl></div>';
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
    Route("/v1/tob/admin/threads/{thread_id}/summaries", get_summaries),
]
