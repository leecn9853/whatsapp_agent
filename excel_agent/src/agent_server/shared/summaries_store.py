"""独立于 SummarizationMiddleware 的对话摘要审计表：记录每次触发压缩时"压缩前的
原始消息"与"压缩后的结构化摘要"，供离线查询/报表使用。

用连接池而不是每次单开一个连接，和 runs_store.py 复用同一份理由：agent-server
要同时服务多个 HTTP 请求。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

if TYPE_CHECKING:
    from src.agent.middleware.conversation_summary import ConversationSummarySchema


class SummariesStore:
    """把每次摘要审计的压缩前/后数据持久化到 Postgres。"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    id                 TEXT PRIMARY KEY,
                    thread_id          TEXT NOT NULL,
                    token_count_before INTEGER NOT NULL,
                    raw_messages       JSONB NOT NULL,
                    session_intent     TEXT NOT NULL,
                    excel_context      JSONB NOT NULL,
                    decisions          JSONB NOT NULL,
                    next_steps         JSONB NOT NULL,
                    artifacts          JSONB NOT NULL,
                    created_at         TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_summaries_thread_id_created_at "
                "ON conversation_summaries(thread_id, created_at)"
            )

    async def acreate_summary(
        self,
        *,
        thread_id: str,
        token_count_before: int,
        raw_messages: list[dict[str, Any]],
        summary: "ConversationSummarySchema",
    ) -> str:
        summary_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO conversation_summaries (
                    id, thread_id, token_count_before, raw_messages,
                    session_intent, excel_context, decisions, next_steps, artifacts,
                    created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    summary_id,
                    thread_id,
                    token_count_before,
                    json.dumps(raw_messages),
                    summary.session_intent,
                    json.dumps(summary.excel_context),
                    json.dumps(summary.decisions),
                    json.dumps(summary.next_steps),
                    json.dumps(summary.artifacts),
                    now,
                ),
            )
        return summary_id

    async def alist_summaries_for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """给 toB 查看页面用：按时间倒序列出某个 thread 触发过的摘要审计记录。
        不选 raw_messages——那是压缩前的原始消息全量转储，查看页面只需要摘要本身。
        """
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT id, token_count_before, session_intent, excel_context, "
                    "decisions, next_steps, artifacts, created_at "
                    "FROM conversation_summaries WHERE thread_id = %s ORDER BY created_at DESC",
                    (thread_id,),
                )
                return await cur.fetchall()

    async def aget_summary_detail(self, summary_id: str) -> dict[str, Any] | None:
        """给查看页面"对比原始消息 vs 摘要"用，按需取单条的 raw_messages（列表页
        不带，太大）。
        """
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT id, thread_id, token_count_before, raw_messages, session_intent, "
                    "excel_context, decisions, next_steps, artifacts, created_at "
                    "FROM conversation_summaries WHERE id = %s",
                    (summary_id,),
                )
                return await cur.fetchone()
