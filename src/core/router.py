"""Deterministic routing before messages reach the agent loop."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from src.core.agent_loop import ConfirmCallbackResult
from src.core.session import Session


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


class CommandMessageHandler(Protocol):
    """Command-handler surface used by the inbound message router."""

    async def handle_command(
        self,
        session: Session | None,
        command_text: str,
        event: object,
    ) -> str | None:
        """Handle one slash command and return optional direct reply text."""


class TextInboundEvent(Protocol):
    """Minimal text event shape needed to classify slash commands."""

    text: str


InboundMessageRouteKind = Literal["pending_interaction", "command", "agent_loop"]


@dataclass(frozen=True, slots=True)
class InboundMessageRoute:
    """Deterministic pre-agent route chosen for one inbound chatbot event."""

    kind: InboundMessageRouteKind
    command_text: str | None = None


def classify_inbound_message(
    event: object,
    *,
    session: Session | None = None,
) -> InboundMessageRoute:
    """Classify an inbound event before it can reach Claude."""

    if session is not None and session.state == "AwaitingInteraction":
        return InboundMessageRoute(kind="pending_interaction")

    command_text = extract_command_text(event, session=session)
    if command_text is not None:
        return InboundMessageRoute(kind="command", command_text=command_text)

    return InboundMessageRoute(kind="agent_loop")


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


def extract_command_text(
    event: object,
    *,
    session: Session | None = None,
) -> str | None:
    """Return normalized slash-command text for DM or group mention syntax."""

    text = getattr(event, "text", None)
    if not isinstance(text, str):
        return None
    stripped = text.lstrip()
    if stripped.startswith("/"):
        return stripped
    if session is not None and session.kind == "group":
        return _group_mention_command_text(stripped)
    return None


def _group_mention_command_text(stripped_text: str) -> str | None:
    remaining = stripped_text
    while remaining.startswith("@"):
        mention_parts = remaining.split(maxsplit=1)
        if len(mention_parts) != 2:
            return None
        remaining = mention_parts[1].lstrip()
        if remaining.startswith("/"):
            return remaining
    return None


COMMANDS_NOT_CONFIGURED_REPLY = "指令通道尚未启用"


__all__ = [
    "CardCallback",
    "COMMANDS_NOT_CONFIGURED_REPLY",
    "CommandMessageHandler",
    "ConfirmCallbackResolver",
    "InboundMessageRoute",
    "InboundMessageRouteKind",
    "InteractionCallbackRouter",
    "TextInboundEvent",
    "classify_inbound_message",
    "extract_command_text",
]
