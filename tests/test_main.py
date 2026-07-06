"""Smoke tests for the initial application entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence

import pytest

from src.adapters.dingtalk import InboundEvent, InboundMessage, UnsupportedInboundMessage
from src.core import (
    GROUP_WELCOME_REPLY,
    Actor,
    BotIdentity,
    Principal,
    Session,
    SessionRouteResult,
)
from src.main import ASSISTANT_SYSTEM_PROMPT, handle_inbound_event, main


def test_main_logs_startup(caplog) -> None:
    """The entry point should start cleanly and emit a startup log."""
    with caplog.at_level(logging.INFO):
        asyncio.run(main())

    assert "DingTalk AI assistant starting" in caplog.text


@pytest.mark.asyncio
async def test_handle_inbound_event_replies_to_triggered_text_message() -> None:
    outbound = FakeOutbound()
    llm_client = FakeLLMClient("LLM reply")
    event = _text_event()

    await handle_inbound_event(event, outbound=outbound, llm_client=llm_client)

    assert llm_client.calls == [
        (
            ASSISTANT_SYSTEM_PROMPT,
            [{"role": "user", "content": "hello"}],
        )
    ]
    assert outbound.replies == [(event, "LLM reply")]


@pytest.mark.asyncio
async def test_handle_inbound_event_routes_session_and_sends_group_welcome() -> None:
    outbound = FakeOutbound()
    llm_client = FakeLLMClient("LLM reply")
    event = _text_event(
        conversation_type=2,
        conversation_id="group-conversation",
        open_conversation_id="open-group-1",
    )
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=Session(
                session_id="dingtalk:group:group-conversation",
                conversation_id="group-conversation",
                kind="group",
                bot=BotIdentity(id="robot-code"),
                principal=Principal(kind="group", id="group:open-group-1"),
                actor=Actor(id="user-1", display_name="Alice"),
            ),
            created=True,
            should_send_welcome=True,
        )
    )

    await handle_inbound_event(
        event,
        outbound=outbound,
        llm_client=llm_client,
        session_manager=session_manager,
    )

    assert session_manager.events == [event]
    assert llm_client.calls == [
        (
            ASSISTANT_SYSTEM_PROMPT,
            [{"role": "user", "content": "hello"}],
        )
    ]
    assert outbound.replies == [(event, GROUP_WELCOME_REPLY), (event, "LLM reply")]


@pytest.mark.asyncio
async def test_handle_inbound_event_replies_to_unsupported_message_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outbound = FakeOutbound()
    llm_client = FakeLLMClient("unused")
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
        await handle_inbound_event(event, outbound=outbound, llm_client=llm_client)

    assert llm_client.calls == []
    assert outbound.replies == [(event, "暂只支持文本")]
    assert any(record.message == "dingtalk_unsupported_message_type" for record in caplog.records)


def _text_event(
    *,
    conversation_type: int = 1,
    conversation_id: str = "conversation-1",
    open_conversation_id: str = "conversation-1",
) -> InboundMessage:
    return InboundMessage(
        text="hello",
        sender_staff_id="user-1",
        sender_nick="Alice",
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        open_conversation_id=open_conversation_id,
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


class FakeLLMClient:
    calls: list[tuple[str, list[dict[str, str]]]]

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls = []

    async def complete(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        self.calls.append((system, [dict(message) for message in messages]))
        return self._reply


class FakeSessionManager:
    events: list[InboundEvent]

    def __init__(self, result: SessionRouteResult) -> None:
        self._result = result
        self.events = []

    async def get_or_create_for_event(self, event: InboundEvent) -> SessionRouteResult:
        self.events.append(event)
        return self._result
