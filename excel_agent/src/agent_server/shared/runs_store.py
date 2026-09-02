"""基于 Postgres 的任务生命周期状态表（runs）。

用连接池而不是每次单开一个连接，是因为 agent-server 要同时服务多个 HTTP 请求。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class RunsStore:
    """把每次 agent 执行的生命周期状态持久化到 Postgres。"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id     TEXT PRIMARY KEY,
                    user_id    TEXT NOT NULL,
                    status     TEXT NOT NULL DEFAULT 'pending',
                    attempt    INTEGER NOT NULL DEFAULT 0,
                    error      TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_user_id_created_at ON runs(user_id, created_at)"
            )

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _update_status(self, run_id: str, status: str, *, error: str | None = None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = %s, error = %s, updated_at = %s WHERE run_id = %s",
                (status, error, self._now(), run_id),
            )

    async def acreate_run(self, user_id: str) -> str:
        run_id = uuid.uuid4().hex
        now = self._now()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runs (run_id, user_id, status, attempt, created_at, updated_at) "
                "VALUES (%s, %s, 'pending', 0, %s, %s)",
                (run_id, user_id, now, now),
            )
        return run_id

    async def amark_running(self, run_id: str) -> None:
        await self._update_status(run_id, "running")

    async def arecord_attempt(self, run_id: str, attempt: int) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'running', attempt = %s, updated_at = %s WHERE run_id = %s",
                (attempt, self._now(), run_id),
            )

    async def amark_success(self, run_id: str) -> None:
        await self._update_status(run_id, "success")

    async def amark_error(self, run_id: str, error: str) -> None:
        await self._update_status(run_id, "error", error=error)

    async def amark_cancelled(self, run_id: str) -> None:
        await self._update_status(run_id, "cancelled")

    async def alist_runs_for_thread(self, thread_id: str) -> list[dict]:
        """给 toB 查看页面用：按时间倒序列出某个 thread 的 run 记录。"""
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT run_id, status, attempt, error, created_at, updated_at "
                    "FROM runs WHERE user_id = %s ORDER BY created_at DESC",
                    (thread_id,),
                )
                return await cur.fetchall()

    async def alist_recent_runs(self, minutes: int, *, limit: int = 200) -> list[dict]:
        """按时间倒序列出最近 N 分钟内所有 thread 的 run 记录。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT run_id, user_id AS thread_id, status, attempt, error, created_at, updated_at "
                    "FROM runs WHERE created_at >= %s ORDER BY created_at DESC LIMIT %s",
                    (cutoff, limit),
                )
                return await cur.fetchall()
