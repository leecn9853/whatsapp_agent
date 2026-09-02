"""run_id / thread_id 日志上下文，通过 contextvars 在整条处理链路中自动注入。"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
_thread_id: contextvars.ContextVar[str] = contextvars.ContextVar("thread_id", default="-")


def get_run_id() -> str:
    return _run_id.get()


def get_thread_id() -> str:
    return _thread_id.get()


@contextmanager
def bind_run_context(run_id: str, thread_id: str) -> Iterator[None]:
    run_token = _run_id.set(run_id)
    thread_token = _thread_id.set(thread_id)
    try:
        yield
    finally:
        _run_id.reset(run_token)
        _thread_id.reset(thread_token)
