"""Tests for DingTalk Stream callback normalization and registration."""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest
from dingtalk_stream import AckMessage, CallbackMessage, ChatbotHandler, ChatbotMessage
from dingtalk_stream import Credential as StreamCredential

from src.adapters.dingtalk.message import (
    InboundMessage,
    MessageNormalizationError,
    normalize_chatbot_callback,
)
from src.adapters.dingtalk.stream import DingTalkStreamAdapter
from src.infra.config import DingTalkConfig


def _config() -> DingTalkConfig:
    return DingTalkConfig(
        app_key="app-key",
        app_secret="app-secret",
        robot_code="robot-code",
        api_base="https://api.example.com",
        legacy_api_base="https://oapi.example.com",
    )


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "msgtype": "text",
        "text": {"content": " hello from dingtalk "},
        "senderStaffId": " user-1 ",
        "senderNick": " Alice ",
        "conversationType": "2",
        "conversationId": " conversation-1 ",
        "openConversationId": " open-conversation-1 ",
        "sessionWebhook": " https://webhook.example.com/session ",
        "msgId": " msg-1 ",
    }
    payload.update(overrides)
    return payload


def _callback(payload: dict[str, object] | None = None) -> CallbackMessage:
    message = CallbackMessage()
    message.data = _payload() if payload is None else payload
    return message


def test_normalize_chatbot_callback_returns_inbound_message() -> None:
    inbound = normalize_chatbot_callback(_payload())

    assert inbound == InboundMessage(
        text=" hello from dingtalk ",
        sender_staff_id="user-1",
        sender_nick="Alice",
        conversation_type=2,
        conversation_id="conversation-1",
        open_conversation_id="open-conversation-1",
        session_webhook="https://webhook.example.com/session",
        msg_id="msg-1",
    )


def test_normalize_chatbot_callback_rejects_missing_required_field() -> None:
    payload = _payload()
    payload.pop("openConversationId")

    with pytest.raises(MessageNormalizationError, match="openConversationId"):
        normalize_chatbot_callback(payload)


def test_normalize_chatbot_callback_uses_conversation_id_for_single_chat_open_id() -> None:
    payload = _payload(conversationType=1)
    payload.pop("openConversationId")

    inbound = normalize_chatbot_callback(payload)

    assert inbound.conversation_id == "conversation-1"
    assert inbound.open_conversation_id == "conversation-1"


def test_normalize_chatbot_callback_rejects_non_text_message() -> None:
    payload = _payload(msgtype="picture", content={"downloadCode": "image-1"})
    payload.pop("text")

    with pytest.raises(MessageNormalizationError, match="Unsupported DingTalk message type"):
        normalize_chatbot_callback(payload)


@pytest.mark.asyncio
async def test_stream_adapter_registers_chatbot_topic_and_dispatches_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    received: list[InboundMessage] = []

    async def on_message(message: InboundMessage) -> None:
        received.append(message)

    adapter = DingTalkStreamAdapter(_config(), on_message, client_factory=FakeStreamClient)
    client = adapter.create_client()
    handler = client.handlers[ChatbotMessage.TOPIC]

    with caplog.at_level(logging.INFO):
        code, message = await handler.process(_callback())

    assert client.credential.client_id == "app-key"
    assert client.credential.client_secret == "app-secret"
    assert code == AckMessage.STATUS_OK
    assert message == "ok"
    assert received == [
        InboundMessage(
            text=" hello from dingtalk ",
            sender_staff_id="user-1",
            sender_nick="Alice",
            conversation_type=2,
            conversation_id="conversation-1",
            open_conversation_id="open-conversation-1",
            session_webhook="https://webhook.example.com/session",
            msg_id="msg-1",
        )
    ]
    assert any(
        record.message == "dingtalk_inbound_message"
        and record.msg_id == "msg-1"
        and record.open_conversation_id == "open-conversation-1"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_stream_adapter_returns_bad_request_for_invalid_callback() -> None:
    received: list[InboundMessage] = []

    async def on_message(message: InboundMessage) -> None:
        received.append(message)

    adapter = DingTalkStreamAdapter(_config(), on_message, client_factory=FakeStreamClient)
    client = adapter.create_client()
    handler = client.handlers[ChatbotMessage.TOPIC]

    code, message = await handler.process(_callback({"msgtype": "text"}))

    assert code == AckMessage.STATUS_BAD_REQUEST
    assert "DingTalk" in message
    assert received == []


class FakeStreamClient:
    """Minimal SDK-compatible client for adapter tests."""

    instances: ClassVar[list[FakeStreamClient]] = []

    def __init__(self, credential: StreamCredential) -> None:
        self.credential = credential
        self.handlers: dict[str, ChatbotHandler] = {}
        self.started = False
        self.instances.append(self)

    def register_callback_handler(self, topic: str, handler: ChatbotHandler) -> None:
        self.handlers[topic] = handler

    async def start(self) -> None:
        self.started = True
