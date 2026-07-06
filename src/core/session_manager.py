"""Persistent Session routing for normalized inbound messages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from src.core.session import Actor, BotIdentity, Principal, Session, SessionKind
from src.infra.store import SessionRecord

DINGTALK_PLATFORM = "dingtalk"
GROUP_WELCOME_REPLY = "大家好，我是企业内 AI 助手，已在本群激活。之后 @我即可继续对话。"


class SessionEvent(Protocol):
    """Inbound event fields needed to resolve the target Session."""

    sender_staff_id: str
    sender_nick: str
    conversation_type: int
    conversation_id: str
    open_conversation_id: str
    msg_id: str


class SessionStore(Protocol):
    """Store methods used by the Session router."""

    async def get_session_by_conversation_id(
        self,
        conversation_id: str,
    ) -> SessionRecord | None:
        """Return the session row for one DingTalk conversation."""

    async def upsert_session(self, record: SessionRecord) -> SessionRecord:
        """Create or update a persistent session row."""


@dataclass(frozen=True, slots=True)
class SessionRouteResult:
    """Result of routing an inbound event into the Session runtime."""

    session: Session
    created: bool
    should_send_welcome: bool = False


class SessionManager:
    """Resolve one persistent Session per DM or group conversation."""

    def __init__(self, store: SessionStore, *, bot_id: str) -> None:
        self._store = store
        self._bot_id = _required_string(bot_id, "bot_id")

    async def get_or_create_for_event(self, event: SessionEvent) -> SessionRouteResult:
        """Return the existing Session for an event, or create it on first activation."""

        kind = _kind_from_conversation_type(event.conversation_type)
        actor = _actor_from_event(event)
        existing = await self._store.get_session_by_conversation_id(event.conversation_id)

        if existing is None:
            stored = await self._store.upsert_session(
                SessionRecord(
                    session_id=_session_id(kind, event.conversation_id),
                    conversation_id=_required_string(event.conversation_id, "conversation_id"),
                    kind=kind,
                    bot_id=self._bot_id,
                    principal_id=_principal_id(kind, event),
                    actor_id=actor.id,
                    state="Idle",
                    lifecycle="active",
                    context=_new_context(kind, event, actor),
                )
            )
            return SessionRouteResult(
                session=_record_to_session(stored, actor),
                created=True,
                should_send_welcome=kind == "group",
            )

        should_activate_group = kind == "group" and not bool(existing.context.get("activated"))
        stored = await self._store.upsert_session(
            replace(
                existing,
                actor_id=actor.id,
                context=_updated_context(existing.context, kind, event, actor),
            )
        )
        return SessionRouteResult(
            session=_record_to_session(stored, actor),
            created=False,
            should_send_welcome=should_activate_group,
        )


def _record_to_session(record: SessionRecord, actor: Actor) -> Session:
    kind = _kind_from_record(record)
    return Session(
        session_id=record.session_id,
        conversation_id=record.conversation_id,
        kind=kind,
        bot=BotIdentity(id=record.bot_id),
        principal=Principal(kind="user" if kind == "dm" else "group", id=record.principal_id),
        actor=actor,
        context=record.context,
        state=record.state,
        lifecycle=record.lifecycle,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _new_context(kind: SessionKind, event: SessionEvent, actor: Actor) -> dict[str, Any]:
    context = _base_context(event, actor)
    if kind == "group":
        context.update(
            {
                "activated": True,
                "activated_by": actor.id,
                "activation_msg_id": _required_string(event.msg_id, "msg_id"),
            }
        )
    return context


def _updated_context(
    context: Mapping[str, Any],
    kind: SessionKind,
    event: SessionEvent,
    actor: Actor,
) -> dict[str, Any]:
    updated = dict(context)
    updated.update(_base_context(event, actor))
    if kind == "group" and not bool(updated.get("activated")):
        updated.update(
            {
                "activated": True,
                "activated_by": actor.id,
                "activation_msg_id": _required_string(event.msg_id, "msg_id"),
            }
        )
    return updated


def _base_context(event: SessionEvent, actor: Actor) -> dict[str, Any]:
    return {
        "platform": DINGTALK_PLATFORM,
        "conversation_type": event.conversation_type,
        "open_conversation_id": _required_string(
            event.open_conversation_id,
            "open_conversation_id",
        ),
        "last_actor_nick": actor.display_name,
    }


def _actor_from_event(event: SessionEvent) -> Actor:
    return Actor(
        id=_required_string(event.sender_staff_id, "sender_staff_id"),
        display_name=_required_string(event.sender_nick, "sender_nick"),
    )


def _kind_from_conversation_type(conversation_type: int) -> SessionKind:
    if conversation_type == 1:
        return "dm"
    if conversation_type == 2:
        return "group"
    raise ValueError("DingTalk conversation_type must be 1 or 2")


def _kind_from_record(record: SessionRecord) -> SessionKind:
    if record.kind in ("dm", "group"):
        return record.kind
    raise ValueError(f"Stored session has invalid kind: {record.kind}")


def _session_id(kind: SessionKind, conversation_id: str) -> str:
    return f"{DINGTALK_PLATFORM}:{kind}:{_required_string(conversation_id, 'conversation_id')}"


def _principal_id(kind: SessionKind, event: SessionEvent) -> str:
    if kind == "dm":
        return f"user:{_required_string(event.sender_staff_id, 'sender_staff_id')}"
    return f"group:{_required_string(event.open_conversation_id, 'open_conversation_id')}"


def _required_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()
