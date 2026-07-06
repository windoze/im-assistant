"""Structured logging helpers for the assistant runtime."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

_RESERVED_LOG_RECORD_FIELDS = frozenset(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
) | {"asctime", "message"}


class JsonLineFormatter(logging.Formatter):
    """Format each log record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(
    level: str | int = "INFO",
    *,
    stream: TextIO | None = None,
    force: bool = False,
) -> None:
    """Configure root logging with JSON-line output."""

    log_level = _coerce_level(level)
    root_logger = logging.getLogger()

    if force:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            handler.close()

    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stderr if stream is None else stream)
        handler.setFormatter(JsonLineFormatter())
        root_logger.addHandler(handler)

    root_logger.setLevel(log_level)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that uses the configured structured handlers."""

    return logging.getLogger(name)


def _coerce_level(level: str | int) -> int:
    if isinstance(level, bool):
        raise ValueError("Log level must be a string name or integer level")
    if isinstance(level, int):
        return level

    parsed = logging.getLevelName(level.upper())
    if isinstance(parsed, str):
        raise ValueError(f"Unknown log level: {level}")
    return parsed
