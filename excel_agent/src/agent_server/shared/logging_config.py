"""agent-server 统一日志配置：stdout 输出，供 systemd/journald 收集。"""

from __future__ import annotations

import logging.config
import os


class _RunContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from src.agent_server.shared.log_context import get_run_id, get_thread_id

        record.run_id = get_run_id()
        record.thread_id = get_thread_id()
        return True


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "run_context": {
                    "()": _RunContextFilter,
                },
            },
            "formatters": {
                "default": {
                    "format": (
                        "%(asctime)s %(levelname)-8s %(name)s "
                        "[run_id=%(run_id)s thread_id=%(thread_id)s] %(message)s"
                    ),
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["run_context"],
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "level": level,
                "handlers": ["stdout"],
            },
            "loggers": {
                "httpx": {"level": "WARNING"},
                "docker": {"level": "WARNING"},
                "asyncio": {"level": "WARNING"},
                "watchfiles": {"level": "WARNING"},
            },
        }
    )
