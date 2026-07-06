"""Capability model shared by system, base, and user capability tiers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

CapabilityOrigin = Literal["system", "base", "user"]
CapabilityAvailability = Literal["global", "group", "dm"]
CapabilityHandler = Callable[..., Awaitable[Any] | Any]

VALID_ORIGINS: frozenset[str] = frozenset({"system", "base", "user"})
VALID_AVAILABILITY: frozenset[str] = frozenset({"global", "group", "dm"})


@dataclass(frozen=True, slots=True)
class Requirement:
    """Declarative authorization requirement for an external resource."""

    service: str
    scopes: Sequence[str] = field(default_factory=tuple)
    on_behalf_of: str | None = None

    def __post_init__(self) -> None:
        """Normalize immutable fields and reject malformed requirement metadata."""

        object.__setattr__(self, "service", _non_empty_string(self.service, "service"))
        object.__setattr__(self, "scopes", _string_tuple(self.scopes, "scopes"))
        if self.on_behalf_of is not None:
            object.__setattr__(
                self,
                "on_behalf_of",
                _non_empty_string(self.on_behalf_of, "on_behalf_of"),
            )


@dataclass(frozen=True, slots=True)
class Capability:
    """Tool or skill definition exposed to the assistant runtime."""

    name: str
    origin: CapabilityOrigin
    available_in: Sequence[CapabilityAvailability]
    requires: Sequence[Requirement] = field(default_factory=tuple)
    sensitivity: str = "low"
    handler: CapabilityHandler | None = None
    owner_id: str | None = None

    def __post_init__(self) -> None:
        """Normalize metadata so registry consumers can compare capabilities safely."""

        object.__setattr__(self, "name", _non_empty_string(self.name, "name"))
        object.__setattr__(self, "origin", _literal_value(self.origin, VALID_ORIGINS, "origin"))
        object.__setattr__(
            self,
            "available_in",
            _literal_tuple(self.available_in, VALID_AVAILABILITY, "available_in"),
        )
        object.__setattr__(self, "requires", _requirement_tuple(self.requires))
        object.__setattr__(self, "sensitivity", _non_empty_string(self.sensitivity, "sensitivity"))
        if self.handler is not None and not callable(self.handler):
            raise ValueError("handler must be callable when provided")
        if self.owner_id is not None:
            object.__setattr__(self, "owner_id", _non_empty_string(self.owner_id, "owner_id"))

    @property
    def requires_user_authority(self) -> bool:
        """Return whether the capability needs an on-behalf-of user credential."""

        return any(requirement.on_behalf_of is not None for requirement in self.requires)


def _non_empty_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _literal_value(value: str, allowed_values: frozenset[str], field_name: str) -> str:
    normalized = _non_empty_string(value, field_name)
    if normalized not in allowed_values:
        allowed = ", ".join(sorted(allowed_values))
        raise ValueError(f"{field_name} must be one of: {allowed}")
    return normalized


def _string_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    return tuple(_non_empty_string(value, field_name) for value in values)


def _literal_tuple(
    values: Sequence[str],
    allowed_values: frozenset[str],
    field_name: str,
) -> tuple[str, ...]:
    normalized = _string_tuple(values, field_name)
    if not normalized:
        raise ValueError(f"{field_name} must contain at least one value")
    invalid = sorted({value for value in normalized if value not in allowed_values})
    if invalid:
        allowed = ", ".join(sorted(allowed_values))
        raise ValueError(f"{field_name} values must be one of: {allowed}; got {invalid}")
    return normalized


def _requirement_tuple(values: Sequence[Requirement]) -> tuple[Requirement, ...]:
    if isinstance(values, Requirement) or not isinstance(values, Sequence):
        raise ValueError("requires must be a sequence of Requirement values")
    for value in values:
        if not isinstance(value, Requirement):
            raise ValueError("requires must contain only Requirement values")
    return tuple(values)


__all__ = [
    "Capability",
    "CapabilityAvailability",
    "CapabilityHandler",
    "CapabilityOrigin",
    "Requirement",
]
