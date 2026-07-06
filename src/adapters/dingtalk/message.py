"""Normalize DingTalk Stream chatbot callbacks into application messages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from dingtalk_stream import CallbackMessage, ChatbotMessage


class MessageNormalizationError(ValueError):
    """Raised when a DingTalk callback cannot be converted to an inbound message."""


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Text message shape consumed by the assistant core."""

    text: str
    sender_staff_id: str
    sender_nick: str
    conversation_type: int
    conversation_id: str
    open_conversation_id: str
    session_webhook: str
    msg_id: str


def normalize_chatbot_callback(
    source: CallbackMessage | ChatbotMessage | Mapping[str, Any],
) -> InboundMessage:
    """Convert an SDK callback, SDK chatbot message, or raw callback payload."""

    chatbot_message = _coerce_chatbot_message(source)
    return normalize_chatbot_message(chatbot_message)


def normalize_chatbot_message(message: ChatbotMessage) -> InboundMessage:
    """Convert an SDK chatbot message into the stable application message shape."""

    message_type = _required_string(_message_value(message, "message_type", "msgtype"), "msgtype")
    if message_type != "text":
        raise MessageNormalizationError(f"Unsupported DingTalk message type: {message_type}")

    conversation_type = _required_conversation_type(
        _message_value(message, "conversation_type", "conversationType")
    )
    conversation_id = _required_string(
        _message_value(message, "conversation_id", "conversationId"),
        "conversationId",
    )
    open_conversation_id = _open_conversation_id(message, conversation_type, conversation_id)

    return InboundMessage(
        text=_required_text(message),
        sender_staff_id=_required_string(
            _message_value(message, "sender_staff_id", "senderStaffId"),
            "senderStaffId",
        ),
        sender_nick=_required_string(
            _message_value(message, "sender_nick", "senderNick"),
            "senderNick",
        ),
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        open_conversation_id=open_conversation_id,
        session_webhook=_required_string(
            _message_value(message, "session_webhook", "sessionWebhook"),
            "sessionWebhook",
        ),
        msg_id=_required_string(_message_value(message, "message_id", "msgId"), "msgId"),
    )


def _coerce_chatbot_message(
    source: CallbackMessage | ChatbotMessage | Mapping[str, Any],
) -> ChatbotMessage:
    if isinstance(source, ChatbotMessage):
        return source
    if isinstance(source, CallbackMessage):
        return _chatbot_message_from_payload(source.data)
    if isinstance(source, Mapping):
        return _chatbot_message_from_payload(_extract_mapping_payload(source))
    raise MessageNormalizationError(
        f"Unsupported DingTalk callback source: {type(source).__name__}"
    )


def _chatbot_message_from_payload(payload: Mapping[str, Any]) -> ChatbotMessage:
    try:
        return ChatbotMessage.from_dict(dict(payload))
    except (KeyError, TypeError, ValueError) as exc:
        raise MessageNormalizationError(f"Invalid DingTalk chatbot payload: {exc}") from exc


def _extract_mapping_payload(source: Mapping[str, Any]) -> Mapping[str, Any]:
    if "msgtype" in source:
        return source

    raw_data = source.get("data")
    if isinstance(raw_data, Mapping):
        return raw_data
    if isinstance(raw_data, str) and raw_data.strip():
        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise MessageNormalizationError(f"Invalid DingTalk callback data JSON: {exc}") from exc
        if isinstance(parsed, Mapping):
            return parsed

    raise MessageNormalizationError("DingTalk callback payload must include chatbot message data")


def _required_text(message: ChatbotMessage) -> str:
    text_content = getattr(getattr(message, "text", None), "content", None)
    if not isinstance(text_content, str) or text_content.strip() == "":
        raise MessageNormalizationError("DingTalk text message must include text.content")
    return text_content


def _message_value(message: ChatbotMessage, attribute_name: str, extension_name: str) -> Any:
    value = getattr(message, attribute_name, None)
    if value is not None:
        return value

    extensions = getattr(message, "extensions", None)
    if isinstance(extensions, Mapping):
        return extensions.get(extension_name)
    return None


def _open_conversation_id(
    message: ChatbotMessage,
    conversation_type: int,
    conversation_id: str,
) -> str:
    value = _optional_string(_message_value(message, "open_conversation_id", "openConversationId"))
    if value is not None:
        return value
    if conversation_type == 1:
        return conversation_id
    raise MessageNormalizationError("DingTalk callback missing required field: openConversationId")


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() != "":
        return value.strip()
    return None


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise MessageNormalizationError(f"DingTalk callback missing required field: {field_name}")
    return value.strip()


def _required_conversation_type(value: Any) -> int:
    if isinstance(value, bool):
        raise MessageNormalizationError("DingTalk conversationType must be 1 or 2")
    if isinstance(value, str):
        if not value.isdecimal():
            raise MessageNormalizationError("DingTalk conversationType must be 1 or 2")
        value = int(value)
    if value not in (1, 2):
        raise MessageNormalizationError("DingTalk conversationType must be 1 or 2")
    return value
