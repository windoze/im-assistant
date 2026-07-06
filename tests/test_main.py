"""Smoke tests for the initial application entry point."""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.adapters.dingtalk import InboundEvent, InboundMessage, UnsupportedInboundMessage
from src.main import DEFAULT_TRIGGER_REPLY, handle_inbound_event, main


def test_main_logs_startup(caplog) -> None:
    """The entry point should start cleanly and emit a startup log."""
    with caplog.at_level(logging.INFO):
        asyncio.run(main())

    assert "DingTalk AI assistant starting" in caplog.text


@pytest.mark.asyncio
async def test_handle_inbound_event_replies_to_triggered_text_message() -> None:
    outbound = FakeOutbound()
    event = _text_event()

    await handle_inbound_event(event, outbound=outbound)

    assert outbound.replies == [(event, DEFAULT_TRIGGER_REPLY)]


@pytest.mark.asyncio
async def test_handle_inbound_event_replies_to_unsupported_message_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outbound = FakeOutbound()
    event = UnsupportedInboundMessage(
        message_type="picture",
        sender_staff_id="user-1",
        sender_nick="Alice",
        conversation_type=2,
        conversation_id="conversation-1",
        open_conversation_id="open-conversation-1",
        session_webhook="https://webhook.example.com/session",
        msg_id="msg-1",
    )

    with caplog.at_level(logging.INFO):
        await handle_inbound_event(event, outbound=outbound)

    assert outbound.replies == [(event, "暂只支持文本")]
    assert any(record.message == "dingtalk_unsupported_message_type" for record in caplog.records)


def _text_event() -> InboundMessage:
    return InboundMessage(
        text="hello",
        sender_staff_id="user-1",
        sender_nick="Alice",
        conversation_type=1,
        conversation_id="conversation-1",
        open_conversation_id="conversation-1",
        session_webhook="https://webhook.example.com/session",
        msg_id="msg-1",
    )


class FakeOutbound:
    replies: list[tuple[InboundEvent, str]]

    def __init__(self) -> None:
        self.replies = []

    async def reply(self, inbound: InboundEvent, text: str) -> object:
        self.replies.append((inbound, text))
        return None
