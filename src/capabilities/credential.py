"""Credential context exposed to capability handlers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

CredentialKind = Literal["application", "user"]


class CredentialError(RuntimeError):
    """Raised when a capability requests a credential that is not available."""


@dataclass(frozen=True, slots=True)
class CredentialHandle:
    """One resolved credential for a capability requirement."""

    service: str
    kind: CredentialKind
    scopes: tuple[str, ...] = ()
    principal_id: str | None = None
    actor_id: str | None = None
    user_access_token: str | None = field(default=None, repr=False)
    refreshed: bool = False

    def __post_init__(self) -> None:
        """Normalize credential metadata and validate the credential shape."""

        object.__setattr__(self, "service", _non_empty_string(self.service, "service"))
        if self.kind not in ("application", "user"):
            raise ValueError("kind must be 'application' or 'user'")
        object.__setattr__(self, "scopes", _string_tuple(self.scopes, "scopes"))
        if self.principal_id is not None:
            object.__setattr__(
                self,
                "principal_id",
                _non_empty_string(self.principal_id, "principal_id"),
            )
        if self.actor_id is not None:
            object.__setattr__(self, "actor_id", _non_empty_string(self.actor_id, "actor_id"))
        if self.kind == "user":
            object.__setattr__(
                self,
                "user_access_token",
                _non_empty_string(self.user_access_token, "user_access_token"),
            )
        elif self.user_access_token is not None:
            raise ValueError("application credentials cannot carry a user access token")

    @classmethod
    def application(
        cls,
        *,
        service: str,
        scopes: Iterable[str] = (),
        principal_id: str | None = None,
        actor_id: str | None = None,
    ) -> CredentialHandle:
        """Create an application-level credential marker for a service."""

        return cls(
            service=service,
            kind="application",
            scopes=tuple(scopes),
            principal_id=principal_id,
            actor_id=actor_id,
        )

    @classmethod
    def user_token(
        cls,
        *,
        service: str,
        user_access_token: str,
        scopes: Iterable[str] = (),
        principal_id: str | None = None,
        actor_id: str | None = None,
        refreshed: bool = False,
    ) -> CredentialHandle:
        """Create a user-level OBO credential for a service."""

        return cls(
            service=service,
            kind="user",
            scopes=tuple(scopes),
            principal_id=principal_id,
            actor_id=actor_id,
            user_access_token=user_access_token,
            refreshed=refreshed,
        )


@dataclass(frozen=True, slots=True)
class CredentialUserContext:
    """User identity and OBO token access exposed as `ctx.user`."""

    id: str
    staff_id: str
    principal_id: str
    union_id: str | None = None
    _handles: Mapping[str, CredentialHandle] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Normalize identity fields and freeze credential handles."""

        object.__setattr__(self, "id", _non_empty_string(self.id, "user.id"))
        object.__setattr__(self, "staff_id", _non_empty_string(self.staff_id, "user.staff_id"))
        object.__setattr__(
            self,
            "principal_id",
            _non_empty_string(self.principal_id, "user.principal_id"),
        )
        if self.union_id is not None:
            object.__setattr__(self, "union_id", _non_empty_string(self.union_id, "user.union_id"))
        object.__setattr__(self, "_handles", _handle_mapping(self._handles))

    def handle_for(self, service: str) -> CredentialHandle:
        """Return the resolved credential handle for one service."""

        service_name = _non_empty_string(service, "service")
        handle = self._handles.get(service_name)
        if handle is None:
            raise CredentialError(f"No credential is available for service: {service_name}")
        return handle

    def token_for(self, service: str) -> str:
        """Return the user-level OBO access token for one service."""

        handle = self.handle_for(service)
        if handle.kind != "user" or handle.user_access_token is None:
            raise CredentialError(f"Service does not have a user-level token: {handle.service}")
        return handle.user_access_token


@dataclass(frozen=True, slots=True)
class CredentialGroupContext:
    """Group identity exposed as `ctx.group` for group sessions."""

    id: str
    open_conversation_id: str

    def __post_init__(self) -> None:
        """Normalize group identifiers."""

        object.__setattr__(self, "id", _non_empty_string(self.id, "group.id"))
        object.__setattr__(
            self,
            "open_conversation_id",
            _non_empty_string(self.open_conversation_id, "group.open_conversation_id"),
        )


@dataclass(frozen=True, slots=True)
class CredentialContext:
    """Credential facade attached to one capability execution."""

    user: CredentialUserContext
    group: CredentialGroupContext | None = None
    handles: Mapping[str, CredentialHandle] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze handles and ensure `ctx.user` sees the same mapping."""

        handles = _handle_mapping(self.handles)
        object.__setattr__(self, "handles", handles)
        object.__setattr__(
            self,
            "user",
            CredentialUserContext(
                id=self.user.id,
                staff_id=self.user.staff_id,
                principal_id=self.user.principal_id,
                union_id=self.user.union_id,
                _handles=handles,
            ),
        )

    @classmethod
    def for_session(
        cls,
        session: Any,
        *,
        handles: Iterable[CredentialHandle] = (),
        actor_union_id: str | None = None,
    ) -> CredentialContext:
        """Build a credential context from a Session-like object."""

        handle_map = {handle.service: handle for handle in handles}
        actor = session.actor
        principal = session.principal
        context = getattr(session, "context", {})
        union_id = actor_union_id or _optional_context_string(context, "actor_union_id")
        user = CredentialUserContext(
            id=_non_empty_string(getattr(actor, "id", None), "session.actor.id"),
            staff_id=_non_empty_string(getattr(actor, "id", None), "session.actor.id"),
            principal_id=_non_empty_string(getattr(principal, "id", None), "session.principal.id"),
            union_id=union_id,
            _handles=handle_map,
        )
        group = None
        if getattr(session, "kind", None) == "group":
            group = CredentialGroupContext(
                id=_non_empty_string(getattr(principal, "id", None), "session.principal.id"),
                open_conversation_id=(
                    _optional_context_string(context, "open_conversation_id")
                    or _non_empty_string(
                        getattr(session, "conversation_id", None),
                        "session.conversation_id",
                    )
                ),
            )
        return cls(user=user, group=group, handles=handle_map)

    def handle_for(self, service: str) -> CredentialHandle:
        """Return one resolved credential handle by service name."""

        return self.user.handle_for(service)

    def require_user_token(self, service: str) -> str:
        """Return a user-level OBO access token or raise a handler-facing error."""

        return self.user.token_for(service)


def _handle_mapping(handles: Mapping[str, CredentialHandle]) -> Mapping[str, CredentialHandle]:
    normalized: dict[str, CredentialHandle] = {}
    for service, handle in handles.items():
        service_name = _non_empty_string(service, "credential.service")
        if not isinstance(handle, CredentialHandle):
            raise ValueError("credential handles must be CredentialHandle instances")
        if handle.service != service_name:
            raise ValueError("credential handle service must match its mapping key")
        normalized[service_name] = handle
    return MappingProxyType(normalized)


def _optional_context_string(context: object, key: str) -> str | None:
    if not isinstance(context, Mapping):
        return None
    value = context.get(key)
    if value is None:
        return None
    return _non_empty_string(value, key)


def _string_tuple(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be an iterable of strings")
    return tuple(dict.fromkeys(_non_empty_string(value, field_name) for value in values))


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


__all__ = [
    "CredentialContext",
    "CredentialError",
    "CredentialGroupContext",
    "CredentialHandle",
    "CredentialKind",
    "CredentialUserContext",
]
