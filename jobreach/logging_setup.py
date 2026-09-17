"""Logging helpers shared by the CLI and the scrapers.

Logs always go to **stderr** so that ``jobreach ... --json`` keeps stdout as
clean, machine-parseable JSON. This was a real bug in the previous
implementation, where structured logs shared stdout with the payload.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

LOGGER_NAME = "jobreach"


class _JsonFormatter(logging.Formatter):
    """One JSON object per log line, with ``extra=`` fields inlined."""

    _BUILTIN = frozenset(
        {
            "args", "created", "exc_info", "exc_text", "filename", "funcName",
            "levelname", "levelno", "lineno", "message", "module", "msecs", "msg",
            "name", "pathname", "process", "processName", "relativeCreated",
            "stack_info", "thread", "threadName", "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            payload["error"] = str(record.exc_info[1])
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "WARNING") -> logging.Logger:
    """Configure the ``jobreach`` logger and return it.

    Args:
        level: Standard level name. The default is deliberately quiet — a
            scrape is expected to be noisy at ``INFO`` and the Hermes agent
            only wants output when something is wrong.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())

    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, str(level).upper(), logging.WARNING))
    logger.propagate = False

    # Third-party chatter that would otherwise drown the real signal.
    for noisy in ("playwright", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    return logger


def get_logger(suffix: str = "") -> logging.Logger:
    """Return ``jobreach`` or one of its children (``jobreach.<suffix>``)."""
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)
