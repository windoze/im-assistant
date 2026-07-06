"""Async SQLite storage for sessions, messages, identity bindings, audit, and tokens."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from src.infra.log import get_logger

logger = get_logger(__name__)

SessionKind = Literal["dm", "group"]
MessageRole = Literal["system", "user", "assistant", "tool"]

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('dm', 'group')),
    bot_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    actor_id TEXT,
    state TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_conversation_id
    ON sessions(conversation_id);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    actor_id TEXT,
    provider_message_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session_id_message_id
    ON messages(session_id, message_id);

CREATE TABLE IF NOT EXISTS identity_bindings (
    provider TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    union_id TEXT,
    staff_id TEXT,
    display_name TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, external_user_id)
);

CREATE INDEX IF NOT EXISTS idx_identity_bindings_principal_id
    ON identity_bindings(principal_id);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor_id TEXT,
    principal_id TEXT,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    scope TEXT,
    action TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_session_id
    ON audit_log(session_id);

CREATE INDEX IF NOT EXISTS idx_audit_log_actor_id
    ON audit_log(actor_id);

CREATE TABLE IF NOT EXISTS token_vault (
    principal_id TEXT NOT NULL,
    service TEXT NOT NULL,
    access_token_ciphertext TEXT NOT NULL,
    refresh_token_ciphertext TEXT,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, service)
);

CREATE INDEX IF NOT EXISTS idx_token_vault_expires_at
    ON token_vault(expires_at);
"""


class StoreError(RuntimeError):
    """Raised when stored data cannot be mapped to the expected schema."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Persistent session state for one DingTalk DM or group conversation."""

    session_id: str
    conversation_id: str
    kind: SessionKind
    bot_id: str
    principal_id: str
    actor_id: str | None = None
    state: str = "Idle"
    lifecycle: str = "active"
    context: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """One persisted chat-history item for a session."""

    message_id: int
    session_id: str
    role: MessageRole
    content: str
    actor_id: str | None = None
    provider_message_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IdentityBindingRecord:
    """Binding between a platform user identifier and an internal principal."""

    provider: str
    external_user_id: str
    principal_id: str
    union_id: str | None = None
    staff_id: str | None = None
    display_name: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuditLogRecord:
    """Append-only audit event for sensitive actions and authorization decisions."""

    audit_id: int
    event_type: str
    actor_id: str | None = None
    principal_id: str | None = None
    session_id: str | None = None
    scope: str | None = None
    action: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TokenVaultRecord:
    """Encrypted delegated-token material for one principal and external service."""

    principal_id: str
    service: str
    access_token_ciphertext: str
    refresh_token_ciphertext: str | None = None
    scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SQLiteStore:
    """Async repository wrapper around the assistant SQLite database."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> SQLiteStore:
        """Open the SQLite connection when used as an async context manager."""

        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the SQLite connection on context-manager exit."""

        await self.aclose()

    async def open(self) -> SQLiteStore:
        """Open the SQLite connection if it is not already open."""

        if self._db is not None:
            return self

        if str(self._database_path) != ":memory:":
            self._database_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self._database_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        return self

    async def aclose(self) -> None:
        """Close the SQLite connection if it is open."""

        if self._db is None:
            return

        await self._db.close()
        self._db = None

    async def initialize(self) -> None:
        """Create or migrate all known tables idempotently."""

        db = await self._connection()
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        logger.info("sqlite_store_initialized", extra={"database_path": str(self._database_path)})

    async def upsert_session(self, record: SessionRecord) -> SessionRecord:
        """Create or update a session row and return the stored record."""

        now = _utc_now()
        created_at = _format_datetime(record.created_at or now)
        updated_at = _format_datetime(now)
        db = await self._connection()
        await db.execute(
            """
            INSERT INTO sessions (
                session_id,
                conversation_id,
                kind,
                bot_id,
                principal_id,
                actor_id,
                state,
                lifecycle,
                context_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                kind = excluded.kind,
                bot_id = excluded.bot_id,
                principal_id = excluded.principal_id,
                actor_id = excluded.actor_id,
                state = excluded.state,
                lifecycle = excluded.lifecycle,
                context_json = excluded.context_json,
                updated_at = excluded.updated_at
            """,
            (
                _non_empty_string(record.session_id, "session_id"),
                _non_empty_string(record.conversation_id, "conversation_id"),
                record.kind,
                _non_empty_string(record.bot_id, "bot_id"),
                _non_empty_string(record.principal_id, "principal_id"),
                _optional_string(record.actor_id),
                _non_empty_string(record.state, "state"),
                _non_empty_string(record.lifecycle, "lifecycle"),
                _encode_json_object(record.context),
                created_at,
                updated_at,
            ),
        )
        await db.commit()
        stored = await self.get_session(record.session_id)
        if stored is None:
            raise StoreError(f"Session was not stored: {record.session_id}")
        return stored

    async def get_session(self, session_id: str) -> SessionRecord | None:
        """Return one session by its stable session id."""

        row = await self._fetchone(
            "SELECT * FROM sessions WHERE session_id = ?",
            (_non_empty_string(session_id, "session_id"),),
        )
        return None if row is None else _row_to_session(row)

    async def get_session_by_conversation_id(self, conversation_id: str) -> SessionRecord | None:
        """Return one session by the DingTalk conversation id."""

        row = await self._fetchone(
            "SELECT * FROM sessions WHERE conversation_id = ?",
            (_non_empty_string(conversation_id, "conversation_id"),),
        )
        return None if row is None else _row_to_session(row)

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and cascade its message history."""

        db = await self._connection()
        cursor = await db.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (_non_empty_string(session_id, "session_id"),),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def add_message(
        self,
        *,
        session_id: str,
        role: MessageRole,
        content: str,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> MessageRecord:
        """Append one chat-history message to a session."""

        db = await self._connection()
        timestamp = _format_datetime(created_at or _utc_now())
        cursor = await db.execute(
            """
            INSERT INTO messages (
                session_id,
                role,
                content,
                actor_id,
                provider_message_id,
                metadata_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _non_empty_string(session_id, "session_id"),
                role,
                _non_empty_string(content, "content"),
                _optional_string(actor_id),
                _optional_string(provider_message_id),
                _encode_json_object(metadata or {}),
                timestamp,
            ),
        )
        await db.commit()
        message_id = cursor.lastrowid
        if message_id is None:
            raise StoreError("Message insert did not return a row id")
        return MessageRecord(
            message_id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            actor_id=actor_id,
            provider_message_id=provider_message_id,
            metadata=metadata or {},
            created_at=_parse_datetime(timestamp),
        )

    async def list_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[MessageRecord]:
        """List a session's messages in insertion order."""

        params: tuple[object, ...]
        query = "SELECT * FROM messages WHERE session_id = ? ORDER BY message_id"
        params = (_non_empty_string(session_id, "session_id"),)
        if limit is not None:
            query += " LIMIT ?"
            params = (*params, _positive_int(limit, "limit"))

        rows = await self._fetchall(query, params)
        return [_row_to_message(row) for row in rows]

    async def delete_messages_for_session(self, session_id: str) -> int:
        """Delete all chat-history messages for one session."""

        db = await self._connection()
        cursor = await db.execute(
            "DELETE FROM messages WHERE session_id = ?",
            (_non_empty_string(session_id, "session_id"),),
        )
        await db.commit()
        return cursor.rowcount

    async def upsert_identity_binding(
        self,
        record: IdentityBindingRecord,
    ) -> IdentityBindingRecord:
        """Create or update one platform-to-principal identity binding."""

        now = _utc_now()
        created_at = _format_datetime(record.created_at or now)
        updated_at = _format_datetime(now)
        db = await self._connection()
        await db.execute(
            """
            INSERT INTO identity_bindings (
                provider,
                external_user_id,
                principal_id,
                union_id,
                staff_id,
                display_name,
                metadata_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, external_user_id) DO UPDATE SET
                principal_id = excluded.principal_id,
                union_id = excluded.union_id,
                staff_id = excluded.staff_id,
                display_name = excluded.display_name,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                _non_empty_string(record.provider, "provider"),
                _non_empty_string(record.external_user_id, "external_user_id"),
                _non_empty_string(record.principal_id, "principal_id"),
                _optional_string(record.union_id),
                _optional_string(record.staff_id),
                _optional_string(record.display_name),
                _encode_json_object(record.metadata),
                created_at,
                updated_at,
            ),
        )
        await db.commit()
        stored = await self.get_identity_binding(record.provider, record.external_user_id)
        if stored is None:
            raise StoreError(
                f"Identity binding was not stored: {record.provider}/{record.external_user_id}"
            )
        return stored

    async def get_identity_binding(
        self,
        provider: str,
        external_user_id: str,
    ) -> IdentityBindingRecord | None:
        """Return one platform-to-principal identity binding."""

        row = await self._fetchone(
            """
            SELECT *
            FROM identity_bindings
            WHERE provider = ? AND external_user_id = ?
            """,
            (
                _non_empty_string(provider, "provider"),
                _non_empty_string(external_user_id, "external_user_id"),
            ),
        )
        return None if row is None else _row_to_identity_binding(row)

    async def delete_identity_binding(self, provider: str, external_user_id: str) -> bool:
        """Delete one platform-to-principal identity binding."""

        db = await self._connection()
        cursor = await db.execute(
            "DELETE FROM identity_bindings WHERE provider = ? AND external_user_id = ?",
            (
                _non_empty_string(provider, "provider"),
                _non_empty_string(external_user_id, "external_user_id"),
            ),
        )
        await db.commit()
        return cursor.rowcount > 0

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
        """Append one immutable audit-log event."""

        db = await self._connection()
        timestamp = _format_datetime(created_at or _utc_now())
        cursor = await db.execute(
            """
            INSERT INTO audit_log (
                event_type,
                actor_id,
                principal_id,
                session_id,
                scope,
                action,
                metadata_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _non_empty_string(event_type, "event_type"),
                _optional_string(actor_id),
                _optional_string(principal_id),
                _optional_string(session_id),
                _optional_string(scope),
                _optional_string(action),
                _encode_json_object(metadata or {}),
                timestamp,
            ),
        )
        await db.commit()
        audit_id = cursor.lastrowid
        if audit_id is None:
            raise StoreError("Audit-log insert did not return a row id")
        return AuditLogRecord(
            audit_id=audit_id,
            event_type=event_type,
            actor_id=actor_id,
            principal_id=principal_id,
            session_id=session_id,
            scope=scope,
            action=action,
            metadata=metadata or {},
            created_at=_parse_datetime(timestamp),
        )

    async def list_audit_logs(self, *, limit: int = 100) -> list[AuditLogRecord]:
        """List recent audit events in insertion order."""

        rows = await self._fetchall(
            "SELECT * FROM audit_log ORDER BY audit_id LIMIT ?",
            (_positive_int(limit, "limit"),),
        )
        return [_row_to_audit_log(row) for row in rows]

    async def upsert_token(self, record: TokenVaultRecord) -> TokenVaultRecord:
        """Create or update encrypted token material for one service."""

        now = _utc_now()
        created_at = _format_datetime(record.created_at or now)
        updated_at = _format_datetime(now)
        expires_at = None if record.expires_at is None else _format_datetime(record.expires_at)
        db = await self._connection()
        await db.execute(
            """
            INSERT INTO token_vault (
                principal_id,
                service,
                access_token_ciphertext,
                refresh_token_ciphertext,
                scopes_json,
                expires_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(principal_id, service) DO UPDATE SET
                access_token_ciphertext = excluded.access_token_ciphertext,
                refresh_token_ciphertext = excluded.refresh_token_ciphertext,
                scopes_json = excluded.scopes_json,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (
                _non_empty_string(record.principal_id, "principal_id"),
                _non_empty_string(record.service, "service"),
                _non_empty_string(record.access_token_ciphertext, "access_token_ciphertext"),
                _optional_string(record.refresh_token_ciphertext),
                _encode_json_array(_normalize_scopes(record.scopes)),
                expires_at,
                created_at,
                updated_at,
            ),
        )
        await db.commit()
        stored = await self.get_token(record.principal_id, record.service)
        if stored is None:
            raise StoreError(f"Token was not stored: {record.principal_id}/{record.service}")
        return stored

    async def get_token(self, principal_id: str, service: str) -> TokenVaultRecord | None:
        """Return encrypted token material for one principal and service."""

        row = await self._fetchone(
            "SELECT * FROM token_vault WHERE principal_id = ? AND service = ?",
            (
                _non_empty_string(principal_id, "principal_id"),
                _non_empty_string(service, "service"),
            ),
        )
        return None if row is None else _row_to_token(row)

    async def delete_token(self, principal_id: str, service: str) -> bool:
        """Delete encrypted token material for one principal and service."""

        db = await self._connection()
        cursor = await db.execute(
            "DELETE FROM token_vault WHERE principal_id = ? AND service = ?",
            (
                _non_empty_string(principal_id, "principal_id"),
                _non_empty_string(service, "service"),
            ),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.open()
        if self._db is None:
            raise StoreError("SQLite connection was not opened")
        return self._db

    async def _fetchone(self, query: str, params: Sequence[object]) -> aiosqlite.Row | None:
        db = await self._connection()
        async with db.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, query: str, params: Sequence[object]) -> list[aiosqlite.Row]:
        db = await self._connection()
        async with db.execute(query, params) as cursor:
            return await cursor.fetchall()


async def initialize_database(database_path: str | Path) -> None:
    """Open a store, apply idempotent migrations, then close it."""

    async with SQLiteStore(database_path) as store:
        await store.initialize()


def _row_to_session(row: aiosqlite.Row) -> SessionRecord:
    return SessionRecord(
        session_id=row["session_id"],
        conversation_id=row["conversation_id"],
        kind=row["kind"],
        bot_id=row["bot_id"],
        principal_id=row["principal_id"],
        actor_id=row["actor_id"],
        state=row["state"],
        lifecycle=row["lifecycle"],
        context=_decode_json_object(row["context_json"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _row_to_message(row: aiosqlite.Row) -> MessageRecord:
    return MessageRecord(
        message_id=row["message_id"],
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        actor_id=row["actor_id"],
        provider_message_id=row["provider_message_id"],
        metadata=_decode_json_object(row["metadata_json"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _row_to_identity_binding(row: aiosqlite.Row) -> IdentityBindingRecord:
    return IdentityBindingRecord(
        provider=row["provider"],
        external_user_id=row["external_user_id"],
        principal_id=row["principal_id"],
        union_id=row["union_id"],
        staff_id=row["staff_id"],
        display_name=row["display_name"],
        metadata=_decode_json_object(row["metadata_json"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _row_to_audit_log(row: aiosqlite.Row) -> AuditLogRecord:
    return AuditLogRecord(
        audit_id=row["audit_id"],
        event_type=row["event_type"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        session_id=row["session_id"],
        scope=row["scope"],
        action=row["action"],
        metadata=_decode_json_object(row["metadata_json"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _row_to_token(row: aiosqlite.Row) -> TokenVaultRecord:
    return TokenVaultRecord(
        principal_id=row["principal_id"],
        service=row["service"],
        access_token_ciphertext=row["access_token_ciphertext"],
        refresh_token_ciphertext=row["refresh_token_ciphertext"],
        scopes=tuple(_decode_json_string_array(row["scopes_json"])),
        expires_at=None if row["expires_at"] is None else _parse_datetime(row["expires_at"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _encode_json_object(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encode_json_array(value: Sequence[str]) -> str:
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def _decode_json_object(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise StoreError("Stored JSON value is not an object")
    return decoded


def _decode_json_string_array(value: str) -> list[str]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise StoreError("Stored JSON value is not a string array")
    return decoded


def _normalize_scopes(scopes: Sequence[str]) -> list[str]:
    return [_non_empty_string(scope, "scope") for scope in scopes]


def _non_empty_string(value: str, field_name: str) -> str:
    if value.strip() == "":
        raise ValueError(f"`{field_name}` must be a non-empty string")
    return value.strip()


def _optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"`{field_name}` must be a positive integer")
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
