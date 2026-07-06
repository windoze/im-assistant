"""Session domain objects for the assistant runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SessionKind = Literal["dm", "group"]
SessionState = Literal["Idle", "RunningAgent", "AwaitingInteraction"]
SessionLifecycle = Literal["active", "archived"]
PrincipalKind = Literal["user", "group"]


@dataclass(frozen=True, slots=True)
class BotIdentity:
    """IM bot identity used for inbound and outbound conversation traffic."""

    id: str


@dataclass(frozen=True, slots=True)
class Principal:
    """Stable owner of one DM or group Session."""

    kind: PrincipalKind
    id: str


@dataclass(frozen=True, slots=True)
class Actor:
    """User who triggered the current inbound message."""

    id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class Session:
    """Runtime view of one persistent DM or group conversation."""

    session_id: str
    conversation_id: str
    kind: SessionKind
    bot: BotIdentity
    principal: Principal
    actor: Actor
    context: Mapping[str, Any] = field(default_factory=dict)
    state: SessionState = "Idle"
    lifecycle: SessionLifecycle = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None
