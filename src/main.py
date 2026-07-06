"""Async application entry point for the DingTalk AI assistant."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

from src.infra.log import configure_logging, get_logger

if TYPE_CHECKING:
    from src.adapters.dingtalk import InboundMessage
    from src.infra.config import AppConfig

logger = get_logger("im_assistant")


async def main(*, start_stream: bool = False, config: AppConfig | None = None) -> None:
    """Start the assistant runtime."""

    configure_logging()
    logger.info("DingTalk AI assistant starting")

    if not start_stream:
        return

    from src.infra.config import load_config

    app_config = config or load_config()
    configure_logging(app_config.logging.level, force=True)
    from src.adapters.dingtalk import DingTalkStreamAdapter

    await DingTalkStreamAdapter(app_config.dingtalk, _on_inbound_message).start()


async def _on_inbound_message(message: InboundMessage) -> None:
    """Accept normalized messages until the LLM flow is added in the next task."""

    logger.debug("dingtalk_inbound_message_accepted", extra={"msg_id": message.msg_id})


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for the assistant entry point."""

    parser = argparse.ArgumentParser(description="Run the DingTalk AI assistant.")
    parser.add_argument(
        "--stream",
        action="store_true",
        help="connect DingTalk Stream and log normalized inbound chatbot messages",
    )
    return parser.parse_args(argv)


def cli(argv: Sequence[str] | None = None) -> None:
    """Run the assistant command-line entry point."""

    args = parse_args(argv)
    asyncio.run(main(start_stream=args.stream))


if __name__ == "__main__":
    cli()
