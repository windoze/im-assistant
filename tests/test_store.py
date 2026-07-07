"""Tests for the async SQLite storage layer."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import aiosqlite
import pytest

from src.infra.store import (
    IdentityBindingRecord,
    PendingInteractionRecord,
    SessionRecord,
    SQLiteStore,
    TokenVaultRecord,
)


@pytest.mark.asyncio
async def test_initialize_creates_required_tables_idempotently(tmp_path) -> None:
    """Store initialization should create every T10 table and be safe to repeat."""

    database_path = tmp_path / "assistant.db"

    async with SQLiteStore(database_path) as store:
        await store.initialize()
        await store.initialize()

    async with aiosqlite.connect(database_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ) as cursor:
            table_names = {row[0] for row in await cursor.fetchall()}

    assert {
        "sessions",
        "messages",
        "pending_interactions",
        "identity_bindings",
        "audit_log",
        "token_vault",
    }.issubset(table_names)


@pytest.mark.asyncio
async def test_store_supports_basic_crud_for_all_t10_tables(tmp_path) -> None:
    """The repository should persist and read representative rows for every table."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()

        session = await store.upsert_session(
            SessionRecord(
                session_id="session-1",
                conversation_id="conversation-1",
                kind="dm",
                bot_id="bot-1",
                principal_id="principal-1",
                actor_id="actor-1",
                context={"topic": "intro"},
            )
        )
        assert session.created_at is not None
        assert session.context == {"topic": "intro"}
        assert await store.get_session("session-1") == session
        assert await store.get_session_by_conversation_id("conversation-1") == session

        updated_session = await store.upsert_session(
            replace(
                session,
                actor_id="actor-2",
                state="RunningAgent",
                context={"turn": 2},
            )
        )
        assert updated_session.actor_id == "actor-2"
        assert updated_session.state == "RunningAgent"
        assert updated_session.context == {"turn": 2}
        assert updated_session.created_at == session.created_at

        user_message = await store.add_message(
            session_id="session-1",
            role="user",
            content="hello",
            actor_id="actor-2",
            provider_message_id="msg-1",
            metadata={"source": "dingtalk"},
        )
        assistant_message = await store.add_message(
            session_id="session-1",
            role="assistant",
            content="hi",
        )
        assert await store.list_messages("session-1") == [user_message, assistant_message]
        assert await store.list_messages("session-1", limit=1) == [user_message]
        assert await store.list_recent_messages("session-1", limit=1) == [assistant_message]
        assert await store.list_recent_messages("session-1", limit=2) == [
            user_message,
            assistant_message,
        ]

        pending = await store.create_pending_interaction(
            PendingInteractionRecord(
                correlation_id="confirm-1",
                session_id="session-1",
                kind="confirm",
                responder_id="actor-2",
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                payload={"action": "approve"},
            )
        )
        assert pending.status == "pending"
        assert await store.get_pending_interaction("confirm-1") == pending
        assert await store.get_pending_interaction_for_session("session-1") == pending
        resolved_pending = await store.resolve_pending_interaction(
            "confirm-1",
            status="resolved",
            resolution={"approved": True},
            resolved_at=datetime(2030, 1, 1, 0, 1, tzinfo=UTC),
        )
        assert resolved_pending.status == "resolved"
        assert resolved_pending.resolution == {"approved": True}
        assert resolved_pending.resolved_at == datetime(2030, 1, 1, 0, 1, tzinfo=UTC)
        assert await store.get_pending_interaction_for_session("session-1") is None

        binding = await store.upsert_identity_binding(
            IdentityBindingRecord(
                provider="dingtalk",
                external_user_id="staff-1",
                principal_id="principal-1",
                union_id="union-1",
                display_name="Alice",
                metadata={"tenant": "corp-1"},
            )
        )
        assert await store.get_identity_binding("dingtalk", "staff-1") == binding
        updated_binding = await store.upsert_identity_binding(
            replace(binding, principal_id="principal-2", display_name="Alice Chen")
        )
        assert updated_binding.principal_id == "principal-2"
        assert updated_binding.display_name == "Alice Chen"
        assert updated_binding.created_at == binding.created_at

        audit = await store.append_audit_log(
            event_type="tool_call",
            actor_id="actor-2",
            principal_id="principal-2",
            session_id="session-1",
            scope="calendar:read",
            action="summarize_calendar",
            metadata={"allowed": True},
        )
        assert audit.audit_id == 1
        assert await store.list_audit_logs() == [audit]

        expires_at = datetime(2030, 1, 1, tzinfo=UTC)
        token = await store.upsert_token(
            TokenVaultRecord(
                principal_id="principal-2",
                service="calendar",
                access_token_ciphertext="encrypted-access",
                refresh_token_ciphertext="encrypted-refresh",
                scopes=("calendar:read", "calendar:write"),
                expires_at=expires_at,
            )
        )
        assert token.expires_at == expires_at
        assert token.scopes == ("calendar:read", "calendar:write")
        assert await store.get_token("principal-2", "calendar") == token

        refreshed_token = await store.upsert_token(
            replace(
                token,
                access_token_ciphertext="new-encrypted-access",
                scopes=("calendar:read",),
            )
        )
        assert refreshed_token.access_token_ciphertext == "new-encrypted-access"
        assert refreshed_token.scopes == ("calendar:read",)

        assert refreshed_token.created_at == token.created_at

        assert await store.delete_token("principal-2", "calendar") is True
        assert await store.get_token("principal-2", "calendar") is None
        assert await store.delete_identity_binding("dingtalk", "staff-1") is True
        assert await store.get_identity_binding("dingtalk", "staff-1") is None
        assert await store.delete_messages_for_session("session-1") == 2
        assert await store.list_messages("session-1") == []

        await store.add_message(session_id="session-1", role="user", content="second")
        assert await store.delete_session("session-1") is True
        assert await store.get_session("session-1") is None
        assert await store.list_messages("session-1") == []


@pytest.mark.asyncio
async def test_store_lists_active_pending_interactions_by_expiry(tmp_path) -> None:
    """Recovered timeout scheduling needs all active pending rows in deterministic order."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        await store.upsert_session(
            SessionRecord(
                session_id="session-1",
                conversation_id="conversation-1",
                kind="dm",
                bot_id="bot-1",
                principal_id="principal-1",
                actor_id="actor-1",
            )
        )
        later = await store.create_pending_interaction(
            PendingInteractionRecord(
                correlation_id="confirm-later",
                session_id="session-1",
                kind="confirm",
                responder_id="actor-1",
                expires_at=datetime(2030, 1, 1, 0, 2, tzinfo=UTC),
                payload={"action": "later"},
            )
        )
        earlier = await store.create_pending_interaction(
            PendingInteractionRecord(
                correlation_id="confirm-earlier",
                session_id="session-1",
                kind="confirm",
                responder_id="actor-1",
                expires_at=datetime(2030, 1, 1, 0, 1, tzinfo=UTC),
                payload={"action": "earlier"},
            )
        )
        active_before_resolution = await store.list_pending_interactions()
        await store.resolve_pending_interaction(
            "confirm-earlier",
            status="cancelled",
            resolution={"cancelled": True},
        )

        active = await store.list_pending_interactions()

    assert active_before_resolution == [earlier, later]
    assert active == [later]
