"""Async application entry point for the DingTalk AI assistant."""

from __future__ import annotations

import asyncio

from src.infra.log import configure_logging, get_logger

logger = get_logger("im_assistant")


async def main() -> None:
    """Start the assistant runtime."""
    configure_logging()
    logger.info("DingTalk AI assistant starting")


if __name__ == "__main__":
    asyncio.run(main())
