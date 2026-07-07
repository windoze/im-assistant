"""Deterministic routing for out-of-band interaction callbacks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol

from src.core.agent_loop import ConfirmCallbackResult


class CardCallback(Protocol):
    """Minimal card callback shape needed to resolve a pending interaction."""

    correlation_id: str
    responder_id: str
    decision: Literal["confirm", "cancel"]
    raw: Mapping[str, Any]


class ConfirmCallbackResolver(Protocol):
    """Agent-loop surface used by callback routing."""

    async def resolve_confirm_callback(
        self,
        correlation_id: str,
        *,
        responder: str,
        approved: bool,
        callback_payload: Mapping[str, Any] | None = None,
    ) -> ConfirmCallbackResult:
        """Resolve one pending confirm callback."""


class InteractionCallbackRouter:
    """Route DingTalk card callbacks to pending interactions without invoking the LLM."""

    def __init__(self, resolver: ConfirmCallbackResolver) -> None:
        self._resolver = resolver

    async def handle_card_callback(self, event: CardCallback) -> ConfirmCallbackResult:
        """Resolve a confirm/cancel card callback by correlation id and responder."""

        return await self._resolver.resolve_confirm_callback(
            event.correlation_id,
            responder=event.responder_id,
            approved=event.decision == "confirm",
            callback_payload=event.raw,
        )


__all__ = [
    "CardCallback",
    "ConfirmCallbackResolver",
    "InteractionCallbackRouter",
]
