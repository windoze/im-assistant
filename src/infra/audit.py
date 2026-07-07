"""Typed audit logging for authorization, interaction, and command decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from src.infra.store import AuditLogRecord

OBOAuditDecision = Literal["granted", "needs_consent", "denied"]
InteractionAuditStatus = Literal["resolved", "cancelled"]
CommandAuditOutcome = Literal[
    "executed",
    "unknown_command",
    "missing_session",
    "unavailable",
    "forbidden",
    "invalid_args",
    "failed",
]


class AuditStore(Protocol):
    """Store surface used by the audit logger."""

    async def append_audit_log(
        self,
        *,
        event_type: str,
        actor_id: str | None = None,
        principal_id: str | None = None,
        session_id: str | None = None,
        scope: str | None = None,
        action: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> AuditLogRecord:
        """Append one immutable audit event."""


@dataclass(frozen=True, slots=True)
class AuditLogger:
    """Write normalized audit records into the shared SQLite audit table."""

    store: AuditStore

    async def record_obo_authorization(
        self,
        *,
        actor_id: str,
        principal_id: str,
        session_id: str,
        service: str,
        scopes: Sequence[str],
        mode: str,
        decision: OBOAuditDecision,
        on_behalf_of: str | None,
        reason: str | None = None,
        refreshed: bool = False,
        pending_nonce: str | None = None,
        actor_identity: str | None = None,
    ) -> AuditLogRecord:
        """Record an OBO authorization decision without storing token material."""

        metadata: dict[str, Any] = {
            "service": _non_empty_string(service, "service"),
            "scopes": _string_list(scopes, "scopes"),
            "mode": _non_empty_string(mode, "mode"),
            "decision": decision,
            "on_behalf_of": on_behalf_of,
            "refreshed": refreshed,
        }
        if reason is not None:
            metadata["reason"] = _non_empty_string(reason, "reason")
        if pending_nonce is not None:
            metadata["pending_nonce"] = _non_empty_string(pending_nonce, "pending_nonce")
        if actor_identity is not None:
            metadata["actor_identity"] = _non_empty_string(actor_identity, "actor_identity")

        return await self.store.append_audit_log(
            event_type="obo_authorization",
            actor_id=_non_empty_string(actor_id, "actor_id"),
            principal_id=_non_empty_string(principal_id, "principal_id"),
            session_id=_non_empty_string(session_id, "session_id"),
            scope=_scope_label(service, scopes),
            action=decision,
            metadata=_json_object(metadata, "metadata"),
        )

    async def record_interaction_decision(
        self,
        *,
        correlation_id: str,
        kind: str,
        status: InteractionAuditStatus,
        responder_id: str,
        principal_id: str,
        session_id: str,
        payload: Mapping[str, Any],
        resolution_payload: Mapping[str, Any],
        reason: str | None = None,
    ) -> AuditLogRecord:
        """Record a terminal consent/confirm decision."""

        normalized_kind = _non_empty_string(kind, "kind")
        metadata: dict[str, Any] = {
            "correlation_id": _non_empty_string(correlation_id, "correlation_id"),
            "kind": normalized_kind,
            "status": status,
            **_interaction_subject(payload),
            "resolution": _resolution_metadata(resolution_payload),
        }
        if reason is not None:
            metadata["reason"] = _non_empty_string(reason, "reason")

        return await self.store.append_audit_log(
            event_type="interaction_decision",
            actor_id=_non_empty_string(responder_id, "responder_id"),
            principal_id=_non_empty_string(principal_id, "principal_id"),
            session_id=_non_empty_string(session_id, "session_id"),
            scope=_interaction_scope(payload),
            action=f"{normalized_kind}.{status}",
            metadata=_json_object(metadata, "metadata"),
        )

    async def record_command_execution(
        self,
        *,
        actor_id: str | None,
        principal_id: str | None,
        session_id: str | None,
        command_name: str,
        args: Sequence[str],
        args_text: str,
        session_kind: str | None,
        requires_role: str | None,
        available_in: Sequence[str],
        outcome: CommandAuditOutcome,
        reason: str | None = None,
    ) -> AuditLogRecord:
        """Record one deterministic slash-command dispatch outcome."""

        normalized_command = _non_empty_string(command_name, "command_name")
        metadata: dict[str, Any] = {
            "command": normalized_command,
            "args": _string_list(args, "args"),
            "args_text": args_text,
            "session_kind": session_kind,
            "requires_role": requires_role,
            "available_in": _string_list(available_in, "available_in"),
            "outcome": outcome,
        }
        if reason is not None:
            metadata["reason"] = _non_empty_string(reason, "reason")

        return await self.store.append_audit_log(
            event_type="command_execution",
            actor_id=_optional_string(actor_id),
            principal_id=_optional_string(principal_id),
            session_id=_optional_string(session_id),
            scope=_command_scope(normalized_command, args),
            action=normalized_command,
            metadata=_json_object(metadata, "metadata"),
        )


def _interaction_subject(payload: Mapping[str, Any]) -> dict[str, Any]:
    subject: dict[str, Any] = {}
    for key in ("source", "command", "capability", "tool_use_id", "service", "action"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() != "":
            subject["interaction_action" if key == "action" else key] = value.strip()
    scopes = payload.get("scopes")
    if isinstance(scopes, Sequence) and not isinstance(scopes, (str, bytes, bytearray)):
        subject["scopes"] = _string_list(scopes, "payload.scopes")
    details = payload.get("details")
    if isinstance(details, Mapping):
        subject["details"] = _json_object(details, "payload.details")
    return subject


def _resolution_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("approved", "authorized", "cancelled", "reason", "actor_id", "provider_message_id"):
        if key in payload:
            metadata[key] = _json_value(payload[key], f"resolution.{key}")
    return metadata


def _interaction_scope(payload: Mapping[str, Any]) -> str | None:
    service = payload.get("service")
    scopes = payload.get("scopes")
    if isinstance(service, str) and service.strip() != "":
        if isinstance(scopes, Sequence) and not isinstance(scopes, (str, bytes, bytearray)):
            return _scope_label(service, scopes)
        return service.strip()
    action = payload.get("action")
    if isinstance(action, str) and action.strip() != "":
        return action.strip()
    capability = payload.get("capability")
    if isinstance(capability, str) and capability.strip() != "":
        return capability.strip()
    return None


def _scope_label(service: str, scopes: Sequence[object]) -> str:
    normalized_scopes = _string_list(scopes, "scopes")
    if normalized_scopes:
        return ",".join(normalized_scopes)
    return _non_empty_string(service, "service")


def _command_scope(command_name: str, args: Sequence[str]) -> str:
    normalized_command = _non_empty_string(command_name, "command_name")
    normalized_args = _string_list(args, "args")
    if normalized_command in {"/connect", "/disconnect"}:
        return f"service:{normalized_args[0]}" if normalized_args else "service"
    if normalized_command == "/cancel":
        return "pending_interaction"
    if normalized_command == "/reset":
        return "session_history"
    if normalized_command == "/whoami":
        return "identity"
    if normalized_command == "/help":
        return "registry"
    return f"command:{normalized_command.removeprefix('/')}"


def _json_object(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    return {
        _non_empty_string(key, f"{field_name}.key"): _json_value(
            nested_value,
            f"{field_name}.{key}",
        )
        for key, nested_value in value.items()
    }


def _json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return _json_object(value, field_name)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item, field_name) for item in value]
    raise TypeError(f"{field_name} must be JSON-compatible")


def _string_list(values: Sequence[object], field_name: str) -> list[str]:
    return [_non_empty_string(value, field_name) for value in values]


def _optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


__all__ = [
    "AuditLogger",
    "AuditStore",
    "CommandAuditOutcome",
    "InteractionAuditStatus",
    "OBOAuditDecision",
]
