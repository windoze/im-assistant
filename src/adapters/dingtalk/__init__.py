"""DingTalk Stream and OpenAPI adapter package."""

from src.adapters.dingtalk.message import (
    CardCallbackEvent,
    CardDecision,
    InboundEvent,
    InboundMessage,
    MessageNormalizationError,
    UnsupportedInboundMessage,
    normalize_card_callback,
    normalize_chatbot_callback,
    normalize_chatbot_event,
    normalize_chatbot_message,
    normalize_chatbot_message_event,
)
from src.adapters.dingtalk.outbound import DingTalkOutbound, ReplyResult, reply
from src.adapters.dingtalk.stream import (
    DingTalkCardCallbackHandler,
    DingTalkChatbotCallbackHandler,
    DingTalkStreamAdapter,
)
from src.adapters.dingtalk.trigger import UNSUPPORTED_MESSAGE_REPLY, is_triggered

__all__ = [
    "DingTalkChatbotCallbackHandler",
    "DingTalkCardCallbackHandler",
    "DingTalkOutbound",
    "DingTalkStreamAdapter",
    "CardCallbackEvent",
    "CardDecision",
    "InboundEvent",
    "InboundMessage",
    "MessageNormalizationError",
    "ReplyResult",
    "UNSUPPORTED_MESSAGE_REPLY",
    "UnsupportedInboundMessage",
    "is_triggered",
    "normalize_card_callback",
    "normalize_chatbot_callback",
    "normalize_chatbot_event",
    "normalize_chatbot_message",
    "normalize_chatbot_message_event",
    "reply",
]
