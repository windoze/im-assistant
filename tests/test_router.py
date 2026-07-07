"""Tests for deterministic inbound routing before the agent loop."""

from __future__ import annotations

from dataclasses import dataclass

from src.core import (
    Actor,
    BotIdentity,
    InboundMessageRoute,
    Principal,
    Session,
    classify_inbound_message,
)


def test_classifier_routes_awaiting_session_to_pending_before_command() -> None:
    """Pending interactions have priority over slash-command text."""

    route = classify_inbound_message(
        TextEvent(" /cancel"),
        session=_session(state="AwaitingInteraction"),
    )

    assert route == InboundMessageRoute(kind="pending_interaction")


def test_classifier_routes_slash_command_before_agent_loop() -> None:
    """Slash-prefixed text should enter the command branch, not Claude."""

    route = classify_inbound_message(TextEvent("  /reset now"), session=_session())

    assert route == InboundMessageRoute(kind="command", command_text="/reset now")


def test_classifier_routes_group_mention_slash_command() -> None:
    """Group @mention command syntax should still enter the command branch."""

    route = classify_inbound_message(TextEvent("@助手 /reset now"), session=_session(kind="group"))

    assert route == InboundMessageRoute(kind="command", command_text="/reset now")


def test_classifier_keeps_group_mention_natural_language_in_agent_loop() -> None:
    """A group @mention followed by normal text should not be treated as a command."""

    route = classify_inbound_message(
        TextEvent("@助手 please /reset"), session=_session(kind="group")
    )

    assert route == InboundMessageRoute(kind="agent_loop")


def test_classifier_routes_regular_text_to_agent_loop() -> None:
    """Natural language should continue to the normal agent loop."""

    route = classify_inbound_message(TextEvent("hello"), session=_session())

    assert route == InboundMessageRoute(kind="agent_loop")


def test_classifier_ignores_non_text_events_for_command_detection() -> None:
    """Non-text events can still flow through the existing unsupported-message path."""

    route = classify_inbound_message(object(), session=_session())

    assert route == InboundMessageRoute(kind="agent_loop")


@dataclass(frozen=True, slots=True)
class TextEvent:
    """Minimal text event used by router tests."""

    text: str


def _session(*, state: str = "Idle", kind: str = "dm") -> Session:
    return Session(
        session_id=f"dingtalk:{kind}:conversation-1",
        conversation_id="conversation-1",
        kind=kind,
        bot=BotIdentity(id="robot-code"),
        principal=Principal(
            kind="group" if kind == "group" else "user",
            id="group:open-group-1" if kind == "group" else "user:user-1",
        ),
        actor=Actor(id="user-1", display_name="Alice"),
        state=state,
    )
