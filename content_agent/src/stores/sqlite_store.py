"""基于 SQLite 的简易 `BaseStore` 实现。

官方 `langgraph` 目前只提供内存版（`InMemoryStore`）和 Postgres 版
（`PostgresStore`/`AsyncPostgresStore`）的 Store，没有 SQLite 版本，所以这里自己实现一个。

`BaseStore` 真正要求子类实现的只有 `batch`/`abatch` 两个抽象方法——`get`/`put`/`search`/
`delete` 都是基类基于 `batch` 实现的语法糖（见 `langgraph.store.base.BaseStore` 源码）。
本项目里唯一的调用方 `deepagents.backends.store.StoreBackend` 实际只会发出
`GetOp`、`PutOp`（value=None 表示删除）、`SearchOp`（namespace 前缀查询），且
`SearchOp.query`/`filter` 始终是 None，所以这里不实现语义检索（向量索引），`search`
只做前缀匹配 + 可选的精确字段过滤。

限制（够当前场景用，但扩展前要注意）：
- 不支持 `query` 语义检索，传了会被忽略。
- 不支持 TTL（`supports_ttl` 默认 False，`put(..., ttl=...)` 会直接报错，符合基类约定）。
- `search`/`list_namespaces` 是整表扫描后在 Python 里过滤，量大时会慢——本项目每个
  用户只存 `/memories/AGENTS.md` 一个文件，数据量小，没有这个问题。
- 每次 `batch` 调用都新开一个 SQLite 连接、跑完就关，不维护长连接，避免多线程
  （webhook 走 `run_in_threadpool`，每次可能在不同线程执行）共享连接的麻烦。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    MatchCondition,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

_NAMESPACE_SEP = "\x1f"  # 分隔符，不会出现在真实的 namespace 分量里


def _encode_namespace(namespace: tuple[str, ...]) -> str:
    return _NAMESPACE_SEP.join(namespace)


def _decode_namespace(encoded: str) -> tuple[str, ...]:
    return tuple(encoded.split(_NAMESPACE_SEP))


def _matches_filter(value: dict[str, Any], filter: dict[str, Any] | None) -> bool:  # noqa: A002
    if not filter:
        return True
    ops = {
        "$eq": lambda a, b: a == b,
        "$ne": lambda a, b: a != b,
        "$gt": lambda a, b: a > b,
        "$gte": lambda a, b: a >= b,
        "$lt": lambda a, b: a < b,
        "$lte": lambda a, b: a <= b,
    }
    for field, expected in filter.items():
        actual = value.get(field)
        if isinstance(expected, dict) and len(expected) == 1 and next(iter(expected)) in ops:
            op_name, operand = next(iter(expected.items()))
            if actual is None or not ops[op_name](actual, operand):
                return False
        elif actual != expected:
            return False
    return True


class SqliteStore(BaseStore):
    """把 `/memories/` 等文件持久化到本地 SQLite 文件的 `BaseStore` 实现。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, key)
            )
            """
        )
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _row_to_item(self, namespace: tuple[str, ...], key: str, value: str, created_at: str, updated_at: str) -> Item:
        return Item(
            value=json.loads(value),
            key=key,
            namespace=namespace,
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at),
        )

    def _get(self, conn: sqlite3.Connection, op: GetOp) -> Item | None:
        row = conn.execute(
            "SELECT key, value, created_at, updated_at FROM items WHERE namespace = ? AND key = ?",
            (_encode_namespace(op.namespace), op.key),
        ).fetchone()
        if row is None:
            return None
        key, value, created_at, updated_at = row
        return self._row_to_item(op.namespace, key, value, created_at, updated_at)

    def _put(self, conn: sqlite3.Connection, op: PutOp) -> None:
        encoded_ns = _encode_namespace(op.namespace)
        if op.value is None:
            conn.execute("DELETE FROM items WHERE namespace = ? AND key = ?", (encoded_ns, op.key))
            return
        now = datetime.now(timezone.utc).isoformat()
        existing = conn.execute(
            "SELECT created_at FROM items WHERE namespace = ? AND key = ?",
            (encoded_ns, op.key),
        ).fetchone()
        created_at = existing[0] if existing else now
        conn.execute(
            """
            INSERT INTO items (namespace, key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (encoded_ns, op.key, json.dumps(op.value), created_at, now),
        )

    def _search(self, conn: sqlite3.Connection, op: SearchOp) -> list[SearchItem]:
        prefix = _encode_namespace(op.namespace_prefix) if op.namespace_prefix else ""
        rows = conn.execute("SELECT namespace, key, value, created_at, updated_at FROM items").fetchall()
        matches: list[SearchItem] = []
        for namespace, key, value, created_at, updated_at in rows:
            if prefix and not (namespace == prefix or namespace.startswith(prefix + _NAMESPACE_SEP)):
                continue
            decoded_value = json.loads(value)
            if not _matches_filter(decoded_value, op.filter):
                continue
            matches.append(
                SearchItem(
                    namespace=_decode_namespace(namespace),
                    key=key,
                    value=decoded_value,
                    created_at=datetime.fromisoformat(created_at),
                    updated_at=datetime.fromisoformat(updated_at),
                )
            )
        matches.sort(key=lambda item: item.updated_at, reverse=True)
        return matches[op.offset : op.offset + op.limit]

    def _list_namespaces(self, conn: sqlite3.Connection, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        rows = conn.execute("SELECT DISTINCT namespace FROM items").fetchall()
        namespaces = {_decode_namespace(row[0]) for row in rows}

        if op.max_depth is not None:
            namespaces = {ns[: op.max_depth] for ns in namespaces}

        def matches(ns: tuple[str, ...], condition: MatchCondition) -> bool:
            if condition.match_type == "prefix":
                return ns[: len(condition.path)] == condition.path
            if condition.match_type == "suffix":
                return ns[-len(condition.path) :] == condition.path
            msg = f"Unknown match_type: {condition.match_type}"
            raise ValueError(msg)

        if op.match_conditions:
            namespaces = {ns for ns in namespaces if all(matches(ns, c) for c in op.match_conditions)}

        sorted_namespaces = sorted(namespaces)
        return sorted_namespaces[op.offset : op.offset + op.limit]

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        conn = self._connect()
        try:
            results: list[Result] = []
            for op in ops:
                if isinstance(op, GetOp):
                    results.append(self._get(conn, op))
                elif isinstance(op, PutOp):
                    results.append(self._put(conn, op))
                elif isinstance(op, SearchOp):
                    results.append(self._search(conn, op))
                elif isinstance(op, ListNamespacesOp):
                    results.append(self._list_namespaces(conn, op))
                else:
                    msg = f"Unsupported op type: {type(op).__name__}"
                    raise NotImplementedError(msg)
            conn.commit()
            return results
        finally:
            conn.close()

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await asyncio.to_thread(self.batch, ops)
