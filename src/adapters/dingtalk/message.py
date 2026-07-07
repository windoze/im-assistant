"""Normalize DingTalk Stream chatbot callbacks into application messages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from dingtalk_stream import CallbackMessage, CardCallbackMessage, ChatbotMessage


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
    session_webhook_expired_time: int | None = None
    message_type: str = "text"


@dataclass(frozen=True, slots=True)
class UnsupportedInboundMessage:
    """Inbound message metadata for content types the assistant cannot process yet."""

    message_type: str
    sender_staff_id: str
    sender_nick: str
    conversation_type: int
    conversation_id: str
    open_conversation_id: str
    session_webhook: str
    msg_id: str
    session_webhook_expired_time: int | None = None


InboundEvent = InboundMessage | UnsupportedInboundMessage
CardDecision = Literal["confirm", "cancel"]


@dataclass(frozen=True, slots=True)
class CardCallbackEvent:
    """Normalized DingTalk interactive-card button callback."""

    correlation_id: str
    responder_id: str
    decision: CardDecision
    card_instance_id: str
    raw: Mapping[str, Any]


def normalize_chatbot_callback(
    source: CallbackMessage | ChatbotMessage | Mapping[str, Any],
) -> InboundMessage:
    """Convert an SDK callback, SDK chatbot message, or raw callback payload."""

    chatbot_message = _coerce_chatbot_message(source)
    return normalize_chatbot_message(chatbot_message)


def normalize_chatbot_event(
    source: CallbackMessage | ChatbotMessage | Mapping[str, Any],
) -> InboundEvent:
    """Convert a DingTalk callback into text or unsupported inbound metadata."""

    chatbot_message = _coerce_chatbot_message(source)
    return normalize_chatbot_message_event(chatbot_message)


def normalize_card_callback(
    source: CallbackMessage | CardCallbackMessage | Mapping[str, Any],
) -> CardCallbackEvent:
    """Convert a DingTalk card callback into a stable interaction decision."""

    payload = _coerce_card_callback_payload(source)
    content = _json_mapping(payload.get("content"), "content")
    top_extension = _json_mapping(payload.get("extension"), "extension")
    content_extension = _json_mapping(content.get("extension"), "content.extension")
    value = _json_mapping(content.get("value"), "content.value")
    callback_data = {
        **top_extension,
        **content_extension,
        **value,
        **{
            key: nested_value
            for key, nested_value in content.items()
            if isinstance(key, str) and key not in {"extension", "value"}
        },
    }
    correlation_id = _first_string(
        callback_data,
        ("correlation_id", "correlationId"),
    ) or _required_string(payload.get("outTrackId"), "outTrackId")
    decision = _card_decision(
        _first_string(callback_data, ("decision", "action", "actionValue", "value"))
    )
    return CardCallbackEvent(
        correlation_id=correlation_id,
        responder_id=_required_string(payload.get("userId"), "userId"),
        decision=decision,
        card_instance_id=_required_string(payload.get("outTrackId"), "outTrackId"),
        raw=_plain_json_object(payload, "card_callback"),
    )


def normalize_chatbot_message(message: ChatbotMessage) -> InboundMessage:
    """Convert an SDK chatbot message into the stable application message shape."""

    event = normalize_chatbot_message_event(message)
    if isinstance(event, InboundMessage):
        return event
    raise MessageNormalizationError(f"Unsupported DingTalk message type: {event.message_type}")


def normalize_chatbot_message_event(message: ChatbotMessage) -> InboundEvent:
    """Convert an SDK chatbot message into text or unsupported inbound metadata."""

    message_type = _required_string(_message_value(message, "message_type", "msgtype"), "msgtype")
    conversation_type = _required_conversation_type(
        _message_value(message, "conversation_type", "conversationType")
    )
    conversation_id = _required_string(
        _message_value(message, "conversation_id", "conversationId"),
        "conversationId",
    )
    open_conversation_id = _open_conversation_id(message, conversation_type, conversation_id)
    common = {
        "sender_staff_id": _required_string(
            _message_value(message, "sender_staff_id", "senderStaffId"),
            "senderStaffId",
        ),
        "sender_nick": _required_string(
            _message_value(message, "sender_nick", "senderNick"),
            "senderNick",
        ),
        "conversation_type": conversation_type,
        "conversation_id": conversation_id,
        "open_conversation_id": open_conversation_id,
        "session_webhook": _required_string(
            _message_value(message, "session_webhook", "sessionWebhook"),
            "sessionWebhook",
        ),
        "msg_id": _required_string(_message_value(message, "message_id", "msgId"), "msgId"),
        "session_webhook_expired_time": _optional_positive_int(
            _message_value(
                message,
                "session_webhook_expired_time",
                "sessionWebhookExpiredTime",
            ),
            "sessionWebhookExpiredTime",
        ),
    }

    if message_type != "text":
        return UnsupportedInboundMessage(message_type=message_type, **common)

    return InboundMessage(
        text=_required_text(message),
        message_type=message_type,
        **common,
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


def _coerce_card_callback_payload(
    source: CallbackMessage | CardCallbackMessage | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(source, CardCallbackMessage):
        return _card_callback_message_payload(source)
    if isinstance(source, CallbackMessage):
        return _extract_card_mapping_payload(source.data)
    if isinstance(source, Mapping):
        return _extract_card_mapping_payload(source)
    raise MessageNormalizationError(
        f"Unsupported DingTalk card callback source: {type(source).__name__}"
    )


def _card_callback_message_payload(message: CardCallbackMessage) -> dict[str, Any]:
    return {
        "extension": dict(getattr(message, "extension", {}) or {}),
        "corpId": getattr(message, "corp_id", ""),
        "userId": getattr(message, "user_id", ""),
        "content": dict(getattr(message, "content", {}) or {}),
        "outTrackId": getattr(message, "card_instance_id", ""),
    }


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


def _extract_card_mapping_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    if "outTrackId" in source or "userId" in source or "content" in source:
        return dict(source)

    raw_data = source.get("data")
    if isinstance(raw_data, Mapping):
        return dict(raw_data)
    if isinstance(raw_data, str) and raw_data.strip():
        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise MessageNormalizationError(
                f"Invalid DingTalk card callback data JSON: {exc}"
            ) from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)

    raise MessageNormalizationError("DingTalk card callback payload must include data")


def _json_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return {}
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {"value": stripped}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        if isinstance(parsed, str):
            return {"value": parsed}
    raise MessageNormalizationError(f"DingTalk card callback {field_name} must be JSON object data")


def _first_string(values: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()
    return None


def _card_decision(value: str | None) -> CardDecision:
    if value is None:
        raise MessageNormalizationError("DingTalk card callback missing decision")
    normalized = value.strip().lower()
    if normalized in {"confirm", "confirmed", "approve", "approved", "ok", "yes"}:
        return "confirm"
    if normalized in {"cancel", "cancelled", "canceled", "reject", "rejected", "deny", "no"}:
        return "cancel"
    raise MessageNormalizationError(f"Unsupported DingTalk card callback decision: {value}")


def _plain_json_object(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MessageNormalizationError(f"{field_name} must be a mapping")
    return {
        _required_string(key, f"{field_name}.key"): _plain_json_value(
            nested_value,
            f"{field_name}.{key}",
        )
        for key, nested_value in value.items()
    }


def _plain_json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return _plain_json_object(value, field_name)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item, field_name) for item in value]
    return str(value)


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


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MessageNormalizationError(f"DingTalk {field_name} must be a positive integer")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdecimal():
            raise MessageNormalizationError(f"DingTalk {field_name} must be a positive integer")
        value = int(stripped)
    if not isinstance(value, int) or value <= 0:
        raise MessageNormalizationError(f"DingTalk {field_name} must be a positive integer")
    return value
