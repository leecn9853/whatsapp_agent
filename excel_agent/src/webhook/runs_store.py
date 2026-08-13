"""基于 SQLite 的任务生命周期状态表（runs）。

记录每一次 webhook 消息触发的 agent 执行的状态（pending/running/success/
error/cancelled），配合 webhook.py 里"立即 ack + 后台执行"的模式，让后台
任务的进度/结果可以脱离 HTTP 请求生命周期单独查询、排错。

风格上和 src/agent/stores/sqlite_store.py 保持一致：每次调用新开一个 sqlite3 连接、跑完就
关，不维护长连接（webhook 的后台任务可能跑在不同的 asyncio.to_thread 线程
里，避免共享连接的麻烦）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunsStore:
    """把每次 agent 执行的生命周期状态持久化到本地 SQLite 文件。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id     TEXT PRIMARY KEY,
                    user_id    TEXT NOT NULL,
                    status     TEXT NOT NULL DEFAULT 'pending',
                    attempt    INTEGER NOT NULL DEFAULT 0,
                    error      TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_user_id_created_at
                    ON runs(user_id, created_at);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _update_status(self, run_id: str, status: str, *, error: str | None = None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE run_id = ?",
                (status, error, self._now(), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ── 同步接口 ──────────────────────────────────────────────────────────

    def create_run(self, user_id: str) -> str:
        run_id = uuid.uuid4().hex
        now = self._now()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO runs (run_id, user_id, status, attempt, created_at, updated_at) "
                "VALUES (?, ?, 'pending', 0, ?, ?)",
                (run_id, user_id, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id

    def mark_running(self, run_id: str) -> None:
        self._update_status(run_id, "running")

    def record_attempt(self, run_id: str, attempt: int) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE runs SET status = 'running', attempt = ?, updated_at = ? WHERE run_id = ?",
                (attempt, self._now(), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_success(self, run_id: str) -> None:
        self._update_status(run_id, "success")

    def mark_error(self, run_id: str, error: str) -> None:
        self._update_status(run_id, "error", error=error)

    def mark_cancelled(self, run_id: str) -> None:
        self._update_status(run_id, "cancelled")

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ── 异步包装（和 SqliteStore.abatch 同一模式） ──────────────────────────

    async def acreate_run(self, user_id: str) -> str:
        return await asyncio.to_thread(self.create_run, user_id)

    async def amark_running(self, run_id: str) -> None:
        await asyncio.to_thread(self.mark_running, run_id)

    async def arecord_attempt(self, run_id: str, attempt: int) -> None:
        await asyncio.to_thread(self.record_attempt, run_id, attempt)

    async def amark_success(self, run_id: str) -> None:
        await asyncio.to_thread(self.mark_success, run_id)

    async def amark_error(self, run_id: str, error: str) -> None:
        await asyncio.to_thread(self.mark_error, run_id, error)

    async def amark_cancelled(self, run_id: str) -> None:
        await asyncio.to_thread(self.mark_cancelled, run_id)

    async def aget_run(self, run_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_run, run_id)
