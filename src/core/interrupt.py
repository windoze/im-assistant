"""Session interrupt primitives for out-of-band consent and confirmation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from src.core.session import Session, SessionState
from src.infra.store import PendingInteractionRecord, SessionRecord

InterruptKind = Literal["confirm", "consent"]
InterruptResolutionStatus = Literal["resolved", "cancelled"]
DEFAULT_INTERRUPT_TTL_SECONDS = 1800


class SessionInterruptError(RuntimeError):
    """Raised when a Session interrupt cannot be created or resolved."""


class SessionInterruptNotFound(SessionInterruptError):
    """Raised when no pending interrupt exists for a correlation id."""


class SessionInterruptResponderMismatch(SessionInterruptError):
    """Raised when an actor tries to resolve an interrupt owned by someone else."""


class SessionInterruptExpired(SessionInterruptError):
    """Raised when an interrupt is resolved after its expiry time."""


@dataclass(frozen=True, slots=True)
class InterruptResolution:
    """Terminal decision produced by resolving a Session interrupt."""

    correlation_id: str
    kind: InterruptKind
    status: InterruptResolutionStatus
    responder: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    resolved_at: datetime | None = None

    def to_record_payload(self) -> dict[str, Any]:
        """Return a JSON-safe payload for persistence."""

        payload = {
            "kind": self.kind,
            "status": self.status,
            "responder": self.responder,
            "payload": _plain_json_object(self.payload, "resolution.payload"),
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.resolved_at is not None:
            payload["resolved_at"] = _to_utc(self.resolved_at).isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class SessionInterrupt:
    """Out-of-band interaction that suspends a Session until the responder resolves it."""

    kind: InterruptKind
    payload: Mapping[str, Any]
    correlation_id: str
    responder: str
    expires_at: datetime

    def __post_init__(self) -> None:
        """Normalize immutable fields and validate JSON-compatible payloads."""

        object.__setattr__(self, "kind", _interrupt_kind(self.kind))
        object.__setattr__(self, "payload", _plain_json_object(self.payload, "payload"))
        object.__setattr__(
            self, "correlation_id", _non_empty_string(self.correlation_id, "correlation_id")
        )
        object.__setattr__(self, "responder", _non_empty_string(self.responder, "responder"))
        object.__setattr__(self, "expires_at", _to_utc(self.expires_at))

    def resolve(
        self,
        reply: Mapping[str, Any] | None = None,
        *,
        responder: str,
        now: datetime | None = None,
    ) -> InterruptResolution:
        """Resolve this interrupt for the expected responder before expiry."""

        normalized_responder = _non_empty_string(responder, "responder")
        if normalized_responder != self.responder:
            raise SessionInterruptResponderMismatch(
                f"Interrupt {self.correlation_id} can only be resolved by {self.responder}"
            )
        resolved_at = _to_utc(now or datetime.now(UTC))
        if resolved_at > self.expires_at:
            raise SessionInterruptExpired(f"Interrupt expired: {self.correlation_id}")
        return InterruptResolution(
            correlation_id=self.correlation_id,
            kind=self.kind,
            status="resolved",
            responder=normalized_responder,
            payload=_plain_json_object(reply or {}, "reply"),
            resolved_at=resolved_at,
        )

    def cancel(
        self,
        reason: str,
        reply: Mapping[str, Any] | None = None,
        *,
        responder: str,
        require_responder: bool = True,
        allow_expired: bool = False,
        now: datetime | None = None,
    ) -> InterruptResolution:
        """Cancel this interrupt, optionally bypassing responder or expiry checks."""

        normalized_responder = _non_empty_string(responder, "responder")
        if require_responder and normalized_responder != self.responder:
            raise SessionInterruptResponderMismatch(
                f"Interrupt {self.correlation_id} can only be resolved by {self.responder}"
            )
        resolved_at = _to_utc(now or datetime.now(UTC))
        if resolved_at > self.expires_at and not allow_expired:
            raise SessionInterruptExpired(f"Interrupt expired: {self.correlation_id}")
        return InterruptResolution(
            correlation_id=self.correlation_id,
            kind=self.kind,
            status="cancelled",
            responder=normalized_responder,
            payload=_plain_json_object(reply or {}, "reply"),
            reason=_non_empty_string(reason, "reason"),
            resolved_at=resolved_at,
        )

    def to_context(self) -> dict[str, Any]:
        """Return the Session context representation of this pending interrupt."""

        return {
            "kind": self.kind,
            "correlation_id": self.correlation_id,
            "responder": self.responder,
            "expires_at": self.expires_at.isoformat(),
            "payload": dict(self.payload),
        }


class SessionInterruptPersistence(Protocol):
    """Store operations required to persist and resume Session interrupts."""

    async def create_pending_interaction(
        self,
        record: PendingInteractionRecord,
    ) -> PendingInteractionRecord:
        """Persist one pending interaction."""

    async def get_pending_interaction(
        self,
        correlation_id: str,
    ) -> PendingInteractionRecord | None:
        """Return one pending interaction by correlation id."""

    async def get_pending_interaction_for_session(
        self,
        session_id: str,
    ) -> PendingInteractionRecord | None:
        """Return the active pending interaction for a Session."""

    async def resolve_pending_interaction(
        self,
        correlation_id: str,
        *,
        status: InterruptResolutionStatus,
        resolution: Mapping[str, Any],
        resolved_at: datetime | None = None,
    ) -> PendingInteractionRecord:
        """Persist a terminal resolution."""

    async def get_session(self, session_id: str) -> SessionRecord | None:
        """Return one persisted Session."""

    async def upsert_session(self, record: SessionRecord) -> SessionRecord:
        """Persist a Session state change."""


class SessionInterruptManager:
    """Coordinate interrupt persistence with Session state transitions."""

    def __init__(
        self,
        store: SessionInterruptPersistence,
        *,
        now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._now_factory = now_factory

    async def create(
        self,
        session: Session,
        *,
        kind: InterruptKind,
        payload: Mapping[str, Any],
        correlation_id: str,
        responder: str | None = None,
        expires_at: datetime | None = None,
        ttl_seconds: int = DEFAULT_INTERRUPT_TTL_SECONDS,
    ) -> SessionInterrupt:
        """Persist an interrupt and move its Session to AwaitingInteraction."""

        interrupt = SessionInterrupt(
            kind=kind,
            payload=payload,
            correlation_id=correlation_id,
            responder=responder or session.actor.id,
            expires_at=(
                _to_utc(expires_at)
                if expires_at is not None
                else _to_utc(self._now_factory()) + timedelta(seconds=_positive_int(ttl_seconds))
            ),
        )
        await self._store.create_pending_interaction(
            PendingInteractionRecord(
                correlation_id=interrupt.correlation_id,
                session_id=session.session_id,
                kind=interrupt.kind,
                responder_id=interrupt.responder,
                expires_at=interrupt.expires_at,
                payload=interrupt.payload,
            )
        )
        await self._store.upsert_session(
            _session_record_with_state(
                session,
                "AwaitingInteraction",
                context=_context_with_interrupt(session, interrupt),
            )
        )
        return interrupt

    async def get(self, correlation_id: str) -> SessionInterrupt | None:
        """Load a non-terminal interrupt by correlation id."""

        record = await self._store.get_pending_interaction(correlation_id)
        if record is None or record.status != "pending":
            return None
        return _interrupt_from_record(record)

    async def pending_for_session(self, session_id: str) -> SessionInterrupt | None:
        """Load the currently pending interrupt for one Session."""

        record = await self._store.get_pending_interaction_for_session(session_id)
        if record is None:
            return None
        return _interrupt_from_record(record)

    async def resolve(
        self,
        correlation_id: str,
        reply: Mapping[str, Any] | None = None,
        *,
        responder: str,
    ) -> InterruptResolution:
        """Resolve a pending interrupt and restore the Session to Idle."""

        record = await self._store.get_pending_interaction(correlation_id)
        if record is None or record.status != "pending":
            raise SessionInterruptNotFound(f"No active interrupt: {correlation_id}")
        interrupt = _interrupt_from_record(record)
        resolution = interrupt.resolve(
            reply,
            responder=responder,
            now=_to_utc(self._now_factory()),
        )
        await self._store.resolve_pending_interaction(
            interrupt.correlation_id,
            status=resolution.status,
            resolution=resolution.to_record_payload(),
            resolved_at=resolution.resolved_at,
        )
        session_record = await self._store.get_session(record.session_id)
        if session_record is None:
            raise SessionInterruptError(
                f"Interrupted Session no longer exists: {record.session_id}"
            )
        await self._store.upsert_session(
            _session_record_from_record(
                session_record,
                "Idle",
                context=_context_without_interrupt(session_record.context),
            )
        )
        return resolution

    async def cancel(
        self,
        correlation_id: str,
        reason: str,
        reply: Mapping[str, Any] | None = None,
        *,
        responder: str,
        require_responder: bool = True,
        allow_expired: bool = False,
    ) -> InterruptResolution:
        """Cancel a pending interrupt and restore the Session to Idle."""

        record = await self._store.get_pending_interaction(correlation_id)
        if record is None or record.status != "pending":
            raise SessionInterruptNotFound(f"No active interrupt: {correlation_id}")
        interrupt = _interrupt_from_record(record)
        resolution = interrupt.cancel(
            reason,
            reply,
            responder=responder,
            require_responder=require_responder,
            allow_expired=allow_expired,
            now=_to_utc(self._now_factory()),
        )
        await self._store.resolve_pending_interaction(
            interrupt.correlation_id,
            status=resolution.status,
            resolution=resolution.to_record_payload(),
            resolved_at=resolution.resolved_at,
        )
        session_record = await self._store.get_session(record.session_id)
        if session_record is None:
            raise SessionInterruptError(
                f"Interrupted Session no longer exists: {record.session_id}"
            )
        await self._store.upsert_session(
            _session_record_from_record(
                session_record,
                "Idle",
                context=_context_without_interrupt(session_record.context),
            )
        )
        return resolution


def _interrupt_from_record(record: PendingInteractionRecord) -> SessionInterrupt:
    return SessionInterrupt(
        kind=_interrupt_kind(record.kind),
        payload=record.payload,
        correlation_id=record.correlation_id,
        responder=record.responder_id,
        expires_at=record.expires_at,
    )


def _context_with_interrupt(session: Session, interrupt: SessionInterrupt) -> dict[str, Any]:
    context = dict(session.context)
    context["pending_interaction"] = interrupt.to_context()
    return context


def _context_without_interrupt(context: Mapping[str, Any]) -> dict[str, Any]:
    updated = dict(context)
    updated.pop("pending_interaction", None)
    return updated


def _session_record_with_state(
    session: Session,
    state: SessionState,
    *,
    context: Mapping[str, Any],
) -> SessionRecord:
    return SessionRecord(
        session_id=session.session_id,
        conversation_id=session.conversation_id,
        kind=session.kind,
        bot_id=session.bot.id,
        principal_id=session.principal.id,
        actor_id=session.actor.id,
        state=state,
        lifecycle=session.lifecycle,
        context=context,
        created_at=session.created_at,
    )


def _session_record_from_record(
    record: SessionRecord,
    state: SessionState,
    *,
    context: Mapping[str, Any],
) -> SessionRecord:
    return SessionRecord(
        session_id=record.session_id,
        conversation_id=record.conversation_id,
        kind=record.kind,
        bot_id=record.bot_id,
        principal_id=record.principal_id,
        actor_id=record.actor_id,
        state=state,
        lifecycle=record.lifecycle,
        context=context,
        created_at=record.created_at,
    )


def _interrupt_kind(kind: str) -> InterruptKind:
    normalized = _non_empty_string(kind, "kind")
    if normalized == "confirm":
        return "confirm"
    if normalized == "consent":
        return "consent"
    raise ValueError("kind must be 'confirm' or 'consent'")


def _plain_json_object(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return {
        _non_empty_string(key, f"{field_name}.key"): _plain_json_value(
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
    raise ValueError(f"{field_name} must be JSON-compatible")


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive_int(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("ttl_seconds must be a positive integer")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "DEFAULT_INTERRUPT_TTL_SECONDS",
    "InterruptKind",
    "InterruptResolution",
    "InterruptResolutionStatus",
    "SessionInterrupt",
    "SessionInterruptError",
    "SessionInterruptExpired",
    "SessionInterruptManager",
    "SessionInterruptNotFound",
    "SessionInterruptResponderMismatch",
]
