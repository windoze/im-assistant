"""Trigger classification for normalized DingTalk chatbot events."""

from __future__ import annotations

from src.adapters.dingtalk.message import InboundEvent

UNSUPPORTED_MESSAGE_REPLY = "暂只支持文本"


def is_triggered(event: InboundEvent) -> bool:
    """Return whether a normalized DingTalk event should enter assistant handling."""

    if event.conversation_type == 1:
        return True
    if event.conversation_type == 2:
        return True
    return False
