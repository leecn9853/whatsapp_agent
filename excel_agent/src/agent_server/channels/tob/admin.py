"""toB 内部查看页面：给开发/维护人员用，排查所有渠道（WhatsApp + toB）的对话、执行
记录、长期记忆，不做渠道过滤——这是运维排查用的，需要看到全部数据，跟 routes.py 里
"调用方只能看自己 tob: 前缀数据"的隔离要求不一样，所以故意分成两个文件，以后各自升级
鉴权互不影响（比如 admin.py 以后要换成运营/客服账号体系，routes.py 换成真正的外部
toB 鉴权）。

页面本身是纯静态 HTML + fetch，不引入任何前端框架/构建步骤：列表页显示全部 thread，
选中后右侧按 tab 切换四块内容，默认展示"对话"tab，其余 tab（摘要/run 记录/长期记忆）
只有点开才 fetch，不再像早期版本那样选中 thread 就 Promise.all 一次性拉全部四块——
数据量涨起来之后那样做选中 thread 会明显卡顿。

对话历史本身是滚动加载的：进"对话"tab 先拉最新一页（messages?limit=...，不带
before），前端渲染完自动滚到底部；往上滚到接近顶部才用上一页返回的 next_cursor
带 before= 再拉更早一页，往上拼接。这是靠 shared/message_history.py 的
alist_messages_page 按游标分页实现的——游标是某个 checkpoint 的 checkpoint_id，
每次请求只回溯有限数量的 checkpoint，不会像早期版本一次性无差别回溯整条
thread 历史去建"日期骨架"。具体的按消息 id 差集重建"消息何时出现"的原理见该
函数所在文件顶部 docstring。

摘要记录（`StructuredSummarizationMiddleware`（src/agent/middleware/conversation_summary.py）
触发压缩时落库的结构化
摘要，来自 shared/summaries_store.py 的 alist_summaries_for_thread，没有触发过就不显示；
点开单条摘要的"对比原始消息"按钮会再按需请求 aget_summary_detail，拿到压缩前的完整
raw_messages 跟摘要字段并排展示）、run 记录（状态/attempt/失败原因/起止时间，来自
alist_runs_for_thread）、长期记忆（/memories/ 写入的 AGENTS.md 内容，来自
shared/memory.py 的 aget_memory）这三块沿用原来的接口，只是前端改成点 tab 才请求。这个
查记忆的能力原来是顶层通用调试接口 /v1/memories*，现在收进 toB 的查看页面——WhatsApp
没有界面消费这个数据，不需要单独开路由；toC 以后要给用户自己看记忆的话，应该复用
shared/memory.py，不要重新拼 namespace escaping 规则。

messages 的序列化逻辑（含 tool_calls/artifact）放在 shared/messages.py，routes.py
的对外 state 接口复用同一份，避免两处字段定义分叉；message_history.py 只做"按游标
分页重建消息列表"这一层，序列化仍然委托给 messages.py，避免字段定义再分叉一次。
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.memory import aget_memory
from src.agent_server.shared.message_history import alist_messages_page
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
async def get_messages(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    before = request.query_params.get("before")
    limit = min(int(request.query_params.get("limit", 50)), 200)
    page = await alist_messages_page(_runtime.agent, thread_id, before=before, limit=limit)
    return JSONResponse(page)


@local_only
async def get_runs(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    runs = await _runtime.runs_store.alist_runs_for_thread(thread_id)
    return JSONResponse(runs)


@local_only
async def get_recent_runs(request: Request) -> JSONResponse:
    minutes = int(request.query_params.get("minutes", "60"))
    limit = int(request.query_params.get("limit", "200"))
    runs = await _runtime.runs_store.alist_recent_runs(minutes, limit=limit)
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


@local_only
async def get_summary_detail(request: Request) -> JSONResponse:
    """给"对比原始消息 vs 摘要"用，按需取单条摘要审计记录的完整 raw_messages。"""
    summary_id = request.path_params["summary_id"]
    detail = await _runtime.summaries_store.aget_summary_detail(summary_id)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(detail)


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
  #detail { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  h2 { font-size: 15px; color: #555; margin: 20px 0 8px; }
  #tabs { display: flex; gap: 6px; flex-shrink: 0; border-bottom: 1px solid #eee; padding: 16px 24px 10px; }
  #tabs button { border: 1px solid #ddd; background: #fff; border-radius: 6px; padding: 5px 12px; font-size: 13px; cursor: pointer; color: #555; }
  #tabs button:hover { background: #f5f7fb; }
  #tabs button.active { background: #4a6cf7; border-color: #4a6cf7; color: #fff; }
  #tab-body { flex: 1; overflow-y: auto; padding: 16px 24px; }
  #conv-top-loading { text-align: center; font-size: 12px; color: #999; padding: 6px 0; }
  .day-divider { text-align: center; font-size: 12px; color: #999; margin: 12px 0 8px; }
  .msg { padding: 8px 10px; margin-bottom: 6px; border-radius: 6px; background: #f5f5f5; position: relative; }
  .msg .role { font-weight: 600; font-size: 12px; color: #888; margin-bottom: 2px; }
  .msg .role .time { font-weight: 400; color: #bbb; margin-left: 6px; }
  .msg-content { white-space: pre-wrap; position: relative; }
  .msg-content.collapsed { max-height: 300px; overflow: hidden; padding-bottom: 20px; }
  .msg-content.collapsed::before { content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 36px; background: linear-gradient(to bottom, rgba(245,245,245,0), #f5f5f5); pointer-events: none; }
  .expand-btn { position: absolute; right: 10px; bottom: 6px; border: 1px solid #ccc; background: rgba(255,255,255,0.9); border-radius: 4px; font-size: 11px; padding: 2px 8px; cursor: pointer; color: #556; }
  .expand-btn:hover { background: #fff; }
  .tool-call { font-size: 12px; color: #555; margin-top: 4px; }
  .tool-artifact { font-size: 12px; color: #a66; margin-top: 4px; font-family: monospace; }
  .memory { padding: 10px; background: #fafaf0; border: 1px solid #eee; border-radius: 6px; white-space: pre-wrap; font-size: 13px; }
  .summary-card { padding: 10px; background: #f0f8ff; border: 1px solid #dce8f5; border-radius: 6px; margin-bottom: 8px; font-size: 13px; }
  .summary-card dt { font-weight: 600; color: #567; margin-top: 6px; }
  .summary-card dt:first-child { margin-top: 0; }
  .summary-card dd { margin: 2px 0 0 0; white-space: pre-wrap; }
  .compare-btn { margin-top: 8px; border: 1px solid #ccd; background: #fff; border-radius: 4px; font-size: 12px; padding: 3px 10px; cursor: pointer; color: #456; }
  .compare-btn:hover { background: #f5f7fb; }
  .summary-compare { display: flex; gap: 12px; margin-top: 10px; }
  .summary-compare > div { flex: 1; min-width: 0; max-height: 420px; overflow-y: auto; background: #fff; border: 1px solid #e4e4e4; border-radius: 6px; padding: 8px; }
  .summary-compare h4 { margin: 0 0 6px; font-size: 12px; color: #888; }
  .summary-compare .msg { font-size: 12px; }
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
const TABS = [
  { key: 'conversation', label: '对话' },
  { key: 'summaries', label: '摘要' },
  { key: 'runs', label: 'Run 记录' },
  { key: 'memory', label: '长期记忆' },
];

let currentThreadId = null;
let currentTab = 'conversation';
const cache = {};

const PAGE_SIZE = 50;
const COLLAPSE_HEIGHT = 300; // 长消息默认折叠高度（px），改这个数字调整
const convState = {};

function getConvState(threadId) {
  if (!convState[threadId]) {
    convState[threadId] = { messages: [], nextCursor: null, hasMore: true, loaded: false, loading: false, expandedIds: new Set() };
  }
  return convState[threadId];
}

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

function beijingDateKey(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return (iso || '').slice(0, 10);
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
}

function beijingTimeLabel(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso || '';
  const time = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(d);
  return `${beijingDateKey(iso)} ${time}`;
}

async function fetchJsonCached(cacheKey, url) {
  if (cache[cacheKey]) return cache[cacheKey];
  const res = await fetch(url);
  const data = await res.json();
  cache[cacheKey] = data;
  return data;
}

function selectThread(threadId, li) {
  document.querySelectorAll('#thread-list li').forEach(el => el.classList.remove('active'));
  li.classList.add('active');
  currentThreadId = threadId;
  currentTab = 'conversation';
  renderShell();
}

function renderShell() {
  const detail = document.getElementById('detail');
  const tabsHtml = TABS.map(t =>
    `<button data-tab="${t.key}" class="${t.key === currentTab ? 'active' : ''}">${t.label}</button>`
  ).join('');
  detail.innerHTML = `<div id="tabs">${tabsHtml}</div><div id="tab-body"></div>`;
  detail.querySelectorAll('#tabs button').forEach(btn => {
    btn.onclick = () => {
      currentTab = btn.dataset.tab;
      renderShell();
    };
  });
  document.getElementById('tab-body').addEventListener('scroll', onTabBodyScroll);
  renderTab();
}

function onTabBodyScroll() {
  if (currentTab !== 'conversation' || !currentThreadId) return;
  const body = document.getElementById('tab-body');
  if (body.scrollTop < 40) maybeLoadMoreMessages();
}

async function renderTab() {
  const body = document.getElementById('tab-body');
  body.innerHTML = '<p class="empty">加载中…</p>';
  if (currentTab === 'conversation') return renderConversationTab(body);
  if (currentTab === 'summaries') return renderSummariesTab(body);
  if (currentTab === 'runs') return renderRunsTab(body);
  if (currentTab === 'memory') return renderMemoryTab(body);
}

function renderMessage(m, expandedIds) {
  let msgBody = escapeHtml(m.content);
  if (m.tool_calls && m.tool_calls.length > 0) {
    msgBody += m.tool_calls.map(tc =>
      `<div class="tool-call">调用工具 <b>${escapeHtml(tc.name || '')}</b>(${escapeHtml(JSON.stringify(tc.args || {}))})</div>`
    ).join('');
  }
  if (m.role === 'ToolMessage') {
    msgBody = `<div class="tool-call">工具 <b>${escapeHtml(m.tool_name || '')}</b> 返回：</div>` + msgBody;
    if (m.artifact) {
      msgBody += `<div class="tool-artifact">artifact: ${escapeHtml(m.artifact)}</div>`;
    }
  }
  const time = m.created_at ? `<span class="time">${escapeHtml(beijingTimeLabel(m.created_at))}</span>` : '';
  const expandedClass = expandedIds.has(m.id) ? ' expanded' : '';
  return `<div class="msg">` +
    `<div class="role">${escapeHtml(m.role)}${time}</div>` +
    `<div class="msg-content${expandedClass}" data-msg-id="${escapeHtml(m.id || '')}">${msgBody}</div>` +
    `</div>`;
}

function applyCollapse(body, state) {
  body.querySelectorAll('.msg-content').forEach(el => {
    if (el.classList.contains('expanded') || el.scrollHeight <= COLLAPSE_HEIGHT) return;
    el.classList.add('collapsed');
    const btn = document.createElement('button');
    btn.className = 'expand-btn';
    btn.textContent = '展开全部';
    btn.onclick = () => {
      el.classList.remove('collapsed');
      el.classList.add('expanded');
      state.expandedIds.add(el.dataset.msgId);
      btn.remove();
    };
    el.appendChild(btn);
  });
}

function renderConversationMessages(body, state) {
  if (state.messages.length === 0) {
    body.innerHTML = '<p class="empty">无对话记录</p>';
    return;
  }
  let html = state.hasMore ? '<div id="conv-top-loading">上拉加载更多…</div>' : '';
  let prevDate = null;
  for (const m of state.messages) {
    const date = m.created_at ? beijingDateKey(m.created_at) : '';
    if (date && date !== prevDate) {
      html += `<div class="day-divider">${escapeHtml(date)}</div>`;
      prevDate = date;
    }
    html += renderMessage(m, state.expandedIds);
  }
  body.innerHTML = html;
  applyCollapse(body, state);
}

async function loadConversationPage(threadId, state) {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (state.nextCursor) params.set('before', state.nextCursor);
  const res = await fetch(`/v1/tob/admin/threads/${encodeURIComponent(threadId)}/messages?${params}`);
  const data = await res.json();
  state.messages = [...data.messages, ...state.messages];
  state.nextCursor = data.next_cursor;
  state.hasMore = data.has_more;
  state.loaded = true;
}

async function renderConversationTab(body) {
  const threadId = currentThreadId;
  const state = getConvState(threadId);
  if (!state.loaded) {
    await loadConversationPage(threadId, state);
  }
  renderConversationMessages(body, state);
  body.scrollTop = body.scrollHeight;
}

async function maybeLoadMoreMessages() {
  const threadId = currentThreadId;
  const state = getConvState(threadId);
  if (!state.hasMore || state.loading) return;
  state.loading = true;
  const body = document.getElementById('tab-body');
  const prevHeight = body.scrollHeight;
  await loadConversationPage(threadId, state);
  state.loading = false;
  if (currentTab !== 'conversation' || currentThreadId !== threadId || body !== document.getElementById('tab-body')) return;
  renderConversationMessages(body, state);
  body.scrollTop += body.scrollHeight - prevHeight;
}

function renderSummaryCard(s) {
  return '<div class="summary-card">' +
    '<dl>' +
      `<dt>会话意图</dt><dd>${escapeHtml(s.session_intent || '')}</dd>` +
      `<dt>涉及表格</dt><dd>${escapeHtml((s.excel_context || []).join('、') || '（无）')}</dd>` +
      `<dt>已确定的结论/偏好</dt><dd>${escapeHtml((s.decisions || []).join('\\n') || '（无）')}</dd>` +
      `<dt>待完成事项</dt><dd>${escapeHtml((s.next_steps || []).join('\\n') || '（无）')}</dd>` +
      `<dt>生成/引用的文件</dt><dd>${escapeHtml((s.artifacts || []).join('\\n') || '（无）')}</dd>` +
      `<dt>触发时 token 数 / 时间</dt><dd>${s.token_count_before} / ${escapeHtml(s.created_at)}</dd>` +
    '</dl>' +
    `<button class="compare-btn" data-summary-id="${escapeHtml(s.id)}">对比原始消息</button>` +
    `<div class="summary-compare-slot" data-open="0"></div>` +
  '</div>';
}

async function renderSummariesTab(body) {
  const threadId = currentThreadId;
  const summaries = await fetchJsonCached(
    `${threadId}:summaries`,
    `/v1/tob/admin/threads/${encodeURIComponent(threadId)}/summaries`,
  );
  if (summaries.length === 0) {
    body.innerHTML = '<p class="empty">无摘要记录</p>';
    return;
  }
  body.innerHTML = summaries.map(renderSummaryCard).join('');
  body.querySelectorAll('.compare-btn').forEach(btn => {
    btn.onclick = () => toggleSummaryCompare(threadId, btn.dataset.summaryId, btn);
  });
}

// 渲染 raw_messages 里的一条记录——形状来自 langchain_core.messages_to_dict
// （{type, data: {content, tool_calls, name, ...}}），跟 renderMessage 消费的
// shared/messages.py 序列化形状不一样，所以单独写一个适配函数，不复用 renderMessage。
function renderRawMessage(entry) {
  const data = entry.data || {};
  const role = entry.type || 'unknown';
  const content = typeof data.content === 'string' ? data.content : JSON.stringify(data.content ?? '');
  let contentHtml = escapeHtml(content);
  if (data.tool_calls && data.tool_calls.length > 0) {
    contentHtml += data.tool_calls.map(tc =>
      `<div class="tool-call">调用工具 <b>${escapeHtml(tc.name || '')}</b>(${escapeHtml(JSON.stringify(tc.args || {}))})</div>`
    ).join('');
  }
  if (role === 'tool' && data.name) {
    contentHtml = `<div class="tool-call">工具 <b>${escapeHtml(data.name)}</b> 返回：</div>` + contentHtml;
  }
  return `<div class="msg"><div class="role">${escapeHtml(role)}</div><div class="msg-content">${contentHtml}</div></div>`;
}

async function toggleSummaryCompare(threadId, summaryId, btn) {
  const slot = btn.nextElementSibling;
  if (slot.dataset.open === '1') {
    slot.innerHTML = '';
    slot.dataset.open = '0';
    btn.textContent = '对比原始消息';
    return;
  }
  btn.textContent = '收起对比';
  slot.dataset.open = '1';
  slot.innerHTML = '<p class="empty">加载中…</p>';
  const detail = await fetchJsonCached(
    `${threadId}:summary-detail:${summaryId}`,
    `/v1/tob/admin/threads/${encodeURIComponent(threadId)}/summaries/${encodeURIComponent(summaryId)}`,
  );
  const rawHtml = (detail.raw_messages || []).map(renderRawMessage).join('') || '<p class="empty">无原始消息</p>';
  const summaryHtml = '<dl>' +
    `<dt>会话意图</dt><dd>${escapeHtml(detail.session_intent || '')}</dd>` +
    `<dt>涉及表格</dt><dd>${escapeHtml((detail.excel_context || []).join('、') || '（无）')}</dd>` +
    `<dt>已确定的结论/偏好</dt><dd>${escapeHtml((detail.decisions || []).join('\\n') || '（无）')}</dd>` +
    `<dt>待完成事项</dt><dd>${escapeHtml((detail.next_steps || []).join('\\n') || '（无）')}</dd>` +
    `<dt>生成/引用的文件</dt><dd>${escapeHtml((detail.artifacts || []).join('\\n') || '（无）')}</dd>` +
  '</dl>';
  slot.innerHTML = '<div class="summary-compare">' +
    `<div><h4>原始消息（压缩前）</h4>${rawHtml}</div>` +
    `<div><h4>摘要（压缩后）</h4>${summaryHtml}</div>` +
  '</div>';
}

async function renderRunsTab(body) {
  const threadId = currentThreadId;
  const runs = await fetchJsonCached(
    `${threadId}:runs`,
    `/v1/tob/admin/threads/${encodeURIComponent(threadId)}/runs`,
  );
  if (runs.length === 0) {
    body.innerHTML = '<p class="empty">无 run 记录</p>';
    return;
  }
  let html = '<table><tr><th>run_id</th><th>状态</th><th>attempt</th><th>失败原因</th><th>创建时间</th><th>更新时间</th></tr>';
  for (const r of runs) {
    html += `<tr><td>${escapeHtml(r.run_id)}</td><td class="status-${escapeHtml(r.status)}">${escapeHtml(r.status)}</td>` +
      `<td>${r.attempt}</td><td>${escapeHtml(r.error || '')}</td>` +
      `<td>${escapeHtml(r.created_at)}</td><td>${escapeHtml(r.updated_at)}</td></tr>`;
  }
  html += '</table>';
  body.innerHTML = html;
}

async function renderMemoryTab(body) {
  const threadId = currentThreadId;
  const memory = await fetchJsonCached(
    `${threadId}:memory`,
    `/v1/tob/admin/threads/${encodeURIComponent(threadId)}/memory`,
  );
  body.innerHTML = memory.content
    ? `<div class="memory">${escapeHtml(memory.content)}</div>`
    : '<p class="empty">无长期记忆</p>';
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
    Route("/v1/tob/admin/threads/{thread_id}/messages", get_messages),
    Route("/v1/tob/admin/threads/{thread_id}/runs", get_runs),
    Route("/v1/tob/admin/threads/{thread_id}/memory", get_memory),
    Route("/v1/tob/admin/threads/{thread_id}/summaries", get_summaries),
    Route("/v1/tob/admin/threads/{thread_id}/summaries/{summary_id}", get_summary_detail),
]
