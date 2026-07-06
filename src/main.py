"""Async application entry point for the DingTalk AI assistant."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from src.infra.log import configure_logging, get_logger

if TYPE_CHECKING:
    from src.adapters.dingtalk import InboundEvent, InboundMessage
    from src.infra.config import AppConfig

logger = get_logger("im_assistant")
DEFAULT_TRIGGER_REPLY = "收到"


class ReplySender(Protocol):
    """Protocol for objects that can reply to DingTalk inbound events."""

    async def reply(self, inbound: InboundEvent, text: str) -> object:
        """Send a reply to the source conversation for an inbound event."""


async def main(*, start_stream: bool = False, config: AppConfig | None = None) -> None:
    """Start the assistant runtime."""

    configure_logging()
    logger.info("DingTalk AI assistant starting")

    if not start_stream:
        return

    from src.infra.config import load_config
    from src.infra.dingtalk_client import DingTalkClient

    app_config = config or load_config()
    configure_logging(app_config.logging.level, force=True)
    from src.adapters.dingtalk import DingTalkOutbound, DingTalkStreamAdapter

    async with DingTalkClient(app_config.dingtalk) as dingtalk_client:
        async with DingTalkOutbound(dingtalk_client) as outbound:

            async def on_event(event: InboundEvent) -> None:
                await handle_inbound_event(event, outbound=outbound)

            await DingTalkStreamAdapter(app_config.dingtalk, on_event).start()


async def handle_inbound_event(event: InboundEvent, *, outbound: ReplySender) -> None:
    """Apply trigger rules and route a normalized DingTalk inbound event."""

    from src.adapters.dingtalk import (
        UNSUPPORTED_MESSAGE_REPLY,
        UnsupportedInboundMessage,
        is_triggered,
    )

    if not is_triggered(event):
        logger.debug(
            "dingtalk_inbound_message_ignored",
            extra={"msg_id": event.msg_id, "conversation_type": event.conversation_type},
        )
        return

    if isinstance(event, UnsupportedInboundMessage):
        logger.info(
            "dingtalk_unsupported_message_type",
            extra={"msg_id": event.msg_id, "message_type": event.message_type},
        )
        await outbound.reply(event, UNSUPPORTED_MESSAGE_REPLY)
        return

    await _on_inbound_message(event, outbound=outbound)


async def _on_inbound_message(message: InboundMessage, *, outbound: ReplySender) -> None:
    """Reply with fixed text until the LLM flow is added in the next task."""

    logger.debug("dingtalk_inbound_message_accepted", extra={"msg_id": message.msg_id})
    await outbound.reply(message, DEFAULT_TRIGGER_REPLY)


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
