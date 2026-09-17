"""Structured JSON logging setup.

Produces machine-parseable log entries on stdout.  Each log line is
a JSON object with timestamp, level, logger name, and message.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class _JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    # Standard LogRecord attributes that should NOT appear in "extra".
    _BUILTIN_ATTRS: frozenset[str] = frozenset({
        "args", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message",
        "module", "msecs", "msg", "name", "pathname", "process",
        "processName", "relativeCreated", "stack_info", "thread",
        "threadName", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Include any extra={...} fields passed by the caller.
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = str(record.exc_info[1])
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure the root logger with structured JSON output.

    Args:
        level: Python logging level name (case-insensitive).
            Defaults to ``"INFO"``.

    Returns:
        The configured root logger so callers can assign it to
        ``Container.logger``.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JSONFormatter())

    root = logging.getLogger()
    root.handlers.clear()  # Remove any default handlers
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Keep third-party loggers quieter by default
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)

    logger = logging.getLogger("job_hunter")
    logger.info("Logging configured", extra={"level": level.upper()})
    return logger
