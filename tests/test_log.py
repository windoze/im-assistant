"""Tests for structured logging helpers."""

from __future__ import annotations

import io
import json

from src.infra.log import configure_logging, get_logger


def test_get_logger_emits_json_line() -> None:
    """A configured logger should emit structured JSON records."""

    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, force=True)

    logger = get_logger("tests.structured")
    logger.info("logger works", extra={"request_id": "req-1"})

    payload = json.loads(stream.getvalue())
    assert payload["level"] == "INFO"
    assert payload["logger"] == "tests.structured"
    assert payload["message"] == "logger works"
    assert payload["request_id"] == "req-1"
    assert "timestamp" in payload
