"""Async application entry point for the DingTalk AI assistant."""

from __future__ import annotations

import asyncio
import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    """Configure startup logging until the structured logger is added."""
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)


async def main() -> None:
    """Start the assistant runtime."""
    configure_logging()
    logging.getLogger("im_assistant").info("DingTalk AI assistant starting")


if __name__ == "__main__":
    asyncio.run(main())
