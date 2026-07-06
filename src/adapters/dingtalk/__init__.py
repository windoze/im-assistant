"""DingTalk Stream and OpenAPI adapter package."""

from src.adapters.dingtalk.message import (
    InboundMessage,
    MessageNormalizationError,
    normalize_chatbot_callback,
    normalize_chatbot_message,
)
from src.adapters.dingtalk.stream import DingTalkChatbotCallbackHandler, DingTalkStreamAdapter

__all__ = [
    "DingTalkChatbotCallbackHandler",
    "DingTalkStreamAdapter",
    "InboundMessage",
    "MessageNormalizationError",
    "normalize_chatbot_callback",
    "normalize_chatbot_message",
]
