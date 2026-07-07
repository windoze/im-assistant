"""Tests for DingTalk Stream callback normalization and registration."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

import pytest
from dingtalk_stream import (
    AckMessage,
    CallbackMessage,
    Card_Callback_Router_Topic,
    ChatbotHandler,
    ChatbotMessage,
)
from dingtalk_stream import Credential as StreamCredential

from src.adapters.dingtalk.message import (
    CardCallbackEvent,
    InboundEvent,
    InboundMessage,
    MessageNormalizationError,
    UnsupportedInboundMessage,
    normalize_card_callback,
    normalize_chatbot_callback,
    normalize_chatbot_event,
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


def _card_callback_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "outTrackId": " confirm-1 ",
        "userId": " user-1 ",
        "content": (
            '{"value":"{\\"correlation_id\\":\\"confirm-1\\",\\"decision\\":\\"confirm\\"}"}'
        ),
        "extension": "{}",
        "corpId": "corp-1",
    }
    payload.update(overrides)
    return payload


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


def test_normalize_chatbot_event_returns_unsupported_non_text_metadata() -> None:
    payload = _payload(
        msgtype="picture",
        content={"downloadCode": "image-1"},
        sessionWebhookExpiredTime="2000",
    )
    payload.pop("text")

    inbound = normalize_chatbot_event(payload)

    assert inbound == UnsupportedInboundMessage(
        message_type="picture",
        sender_staff_id="user-1",
        sender_nick="Alice",
        conversation_type=2,
        conversation_id="conversation-1",
        open_conversation_id="open-conversation-1",
        session_webhook="https://webhook.example.com/session",
        msg_id="msg-1",
        session_webhook_expired_time=2000,
    )


def test_normalize_card_callback_extracts_confirm_decision() -> None:
    callback = normalize_card_callback(_card_callback_payload())

    assert callback == CardCallbackEvent(
        correlation_id="confirm-1",
        responder_id="user-1",
        decision="confirm",
        card_instance_id="confirm-1",
        raw={
            "outTrackId": " confirm-1 ",
            "userId": " user-1 ",
            "content": (
                '{"value":"{\\"correlation_id\\":\\"confirm-1\\",\\"decision\\":\\"confirm\\"}"}'
            ),
            "extension": "{}",
            "corpId": "corp-1",
        },
    )


def test_normalize_card_callback_extracts_cancel_from_extension() -> None:
    callback = normalize_card_callback(
        _card_callback_payload(
            content={"extension": {"correlationId": "confirm-2", "decision": "cancel"}},
            outTrackId="card-2",
        )
    )

    assert callback.correlation_id == "confirm-2"
    assert callback.card_instance_id == "card-2"
    assert callback.decision == "cancel"


@pytest.mark.asyncio
async def test_stream_adapter_registers_chatbot_topic_and_dispatches_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    received: list[InboundEvent] = []

    async def on_message(message: InboundEvent) -> None:
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
async def test_stream_adapter_registers_and_dispatches_card_callbacks() -> None:
    received_messages: list[InboundEvent] = []
    received_cards: list[CardCallbackEvent] = []

    async def on_message(message: InboundEvent) -> None:
        received_messages.append(message)

    async def on_card_callback(callback: CardCallbackEvent) -> None:
        received_cards.append(callback)

    adapter = DingTalkStreamAdapter(
        _config(),
        on_message,
        on_card_callback=on_card_callback,
        client_factory=FakeStreamClient,
    )
    client = adapter.create_client()
    handler = client.handlers[Card_Callback_Router_Topic]

    code, message = await handler.process(_callback(_card_callback_payload()))

    assert code == AckMessage.STATUS_OK
    assert message == "ok"
    assert received_messages == []
    assert received_cards == [
        CardCallbackEvent(
            correlation_id="confirm-1",
            responder_id="user-1",
            decision="confirm",
            card_instance_id="confirm-1",
            raw=_card_callback_payload(),
        )
    ]


@pytest.mark.asyncio
async def test_stream_adapter_returns_bad_request_for_invalid_callback() -> None:
    received: list[InboundEvent] = []

    async def on_message(message: InboundEvent) -> None:
        received.append(message)

    adapter = DingTalkStreamAdapter(_config(), on_message, client_factory=FakeStreamClient)
    client = adapter.create_client()
    handler = client.handlers[ChatbotMessage.TOPIC]

    code, message = await handler.process(_callback({"msgtype": "text"}))

    assert code == AckMessage.STATUS_BAD_REQUEST
    assert "DingTalk" in message
    assert received == []


@pytest.mark.asyncio
async def test_stream_adapter_contains_on_message_exceptions_and_continues() -> None:
    received_msg_ids: list[str] = []
    fail_next = True

    async def on_message(message: InboundEvent) -> None:
        nonlocal fail_next
        received_msg_ids.append(message.msg_id)
        if fail_next:
            fail_next = False
            raise RuntimeError("downstream failure")

    adapter = DingTalkStreamAdapter(_config(), on_message, client_factory=FakeStreamClient)
    client = adapter.create_client()
    handler = client.handlers[ChatbotMessage.TOPIC]

    first_code, first_message = await handler.process(_callback(_payload(msgId="msg-1")))
    second_code, second_message = await handler.process(_callback(_payload(msgId="msg-2")))

    assert first_code == AckMessage.STATUS_SYSTEM_EXCEPTION
    assert first_message == "on_message failed"
    assert second_code == AckMessage.STATUS_OK
    assert second_message == "ok"
    assert received_msg_ids == ["msg-1", "msg-2"]


@pytest.mark.asyncio
async def test_stream_adapter_dispatches_unsupported_non_text_message() -> None:
    received: list[InboundEvent] = []

    async def on_message(message: InboundEvent) -> None:
        received.append(message)

    adapter = DingTalkStreamAdapter(_config(), on_message, client_factory=FakeStreamClient)
    client = adapter.create_client()
    handler = client.handlers[ChatbotMessage.TOPIC]
    payload = _payload(msgtype="picture", content={"downloadCode": "image-1"})
    payload.pop("text")

    code, message = await handler.process(_callback(payload))

    assert code == AckMessage.STATUS_OK
    assert message == "ok"
    assert received == [
        UnsupportedInboundMessage(
            message_type="picture",
            sender_staff_id="user-1",
            sender_nick="Alice",
            conversation_type=2,
            conversation_id="conversation-1",
            open_conversation_id="open-conversation-1",
            session_webhook="https://webhook.example.com/session",
            msg_id="msg-1",
        )
    ]


@pytest.mark.asyncio
async def test_stream_adapter_reconnects_with_exponential_backoff() -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    ReconnectingStreamClient.instances = []
    adapter = DingTalkStreamAdapter(
        _config(),
        _unused_on_message,
        client_factory=ReconnectingStreamClient,
        reconnect_initial_delay=0.5,
        reconnect_max_delay=1.0,
        sleep=sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await adapter.start()

    assert [client.started for client in ReconnectingStreamClient.instances] == [1, 1, 1]
    assert sleeps == [0.5, 1.0]


async def _unused_on_message(_message: InboundEvent) -> None:
    return None


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


class ReconnectingStreamClient:
    """Fake stream client that fails twice and then cancels the reconnect loop."""

    instances: ClassVar[list[ReconnectingStreamClient]] = []

    def __init__(self, credential: StreamCredential) -> None:
        self.credential = credential
        self.handlers: dict[str, ChatbotHandler] = {}
        self.started = 0
        self.index = len(self.instances)
        self.instances.append(self)

    def register_callback_handler(self, topic: str, handler: ChatbotHandler) -> None:
        self.handlers[topic] = handler

    async def start(self) -> None:
        self.started += 1
        if self.index < 2:
            raise RuntimeError("stream disconnected")
        raise asyncio.CancelledError
