"""Smoke tests for the initial application entry point."""

from __future__ import annotations

import asyncio
import logging

from src.main import main


def test_main_logs_startup(caplog) -> None:
    """The entry point should start cleanly and emit a startup log."""
    with caplog.at_level(logging.INFO):
        asyncio.run(main())

    assert "DingTalk AI assistant starting" in caplog.text
