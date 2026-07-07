"""Tests for SessionInterrupt persistence and resume behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core import (
    Actor,
    BotIdentity,
    Principal,
    Session,
    SessionInterruptExpired,
    SessionInterruptManager,
    SessionInterruptResponderMismatch,
)
from src.infra.store import SessionRecord, SQLiteStore


@pytest.mark.asyncio
async def test_interrupt_manager_persists_interrupt_and_suspends_session(tmp_path) -> None:
    """Creating an interrupt should persist pending state and survive a store reopen."""

    database_path = tmp_path / "assistant.db"
    expires_at = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)

    async with SQLiteStore(database_path) as store:
        session = await _stored_session(store)
        manager = SessionInterruptManager(store)

        interrupt = await manager.create(
            session,
            kind="confirm",
            correlation_id="confirm-1",
            responder="user-1",
            expires_at=expires_at,
            payload={"action": "发送邮件", "details": {"to": "alice@example.com"}},
        )
        stored_session = await store.get_session(session.session_id)
        stored_pending = await store.get_pending_interaction("confirm-1")

    assert interrupt.kind == "confirm"
    assert interrupt.payload == {
        "action": "发送邮件",
        "details": {"to": "alice@example.com"},
    }
    assert stored_pending is not None
    assert stored_pending.session_id == "dingtalk:dm:conversation-1"
    assert stored_pending.status == "pending"
    assert stored_pending.expires_at == expires_at
    assert stored_session is not None
    assert stored_session.state == "AwaitingInteraction"
    assert stored_session.context["pending_interaction"] == {
        "kind": "confirm",
        "correlation_id": "confirm-1",
        "responder": "user-1",
        "expires_at": "2026-01-01T12:30:00+00:00",
        "payload": {
            "action": "发送邮件",
            "details": {"to": "alice@example.com"},
        },
    }

    async with SQLiteStore(database_path) as reopened:
        await reopened.initialize()
        restored = await SessionInterruptManager(reopened).pending_for_session(
            "dingtalk:dm:conversation-1"
        )

    assert restored == interrupt


@pytest.mark.asyncio
async def test_interrupt_manager_resolves_for_expected_responder_and_restores_idle(
    tmp_path,
) -> None:
    """Only the expected responder should resolve an interrupt and resume the Session."""

    current_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def now_factory() -> datetime:
        return current_time

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        manager = SessionInterruptManager(store, now_factory=now_factory)
        await manager.create(
            session,
            kind="consent",
            correlation_id="consent-1",
            expires_at=current_time + timedelta(minutes=10),
            payload={"url": "https://assistant.example.com/oauth/start?nonce=consent-1"},
        )

        with pytest.raises(SessionInterruptResponderMismatch):
            await manager.resolve("consent-1", {"authorized": True}, responder="user-2")

        still_pending = await store.get_pending_interaction("consent-1")
        still_suspended = await store.get_session(session.session_id)
        current_time += timedelta(minutes=1)
        resolution = await manager.resolve(
            "consent-1",
            {"authorized": True},
            responder="user-1",
        )
        resolved = await store.get_pending_interaction("consent-1")
        restored_session = await store.get_session(session.session_id)

    assert still_pending is not None
    assert still_pending.status == "pending"
    assert still_suspended is not None
    assert still_suspended.state == "AwaitingInteraction"
    assert resolution.status == "resolved"
    assert resolution.payload == {"authorized": True}
    assert resolution.resolved_at == datetime(2026, 1, 1, 12, 1, tzinfo=UTC)
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.resolution == {
        "kind": "consent",
        "status": "resolved",
        "responder": "user-1",
        "payload": {"authorized": True},
        "resolved_at": "2026-01-01T12:01:00+00:00",
    }
    assert restored_session is not None
    assert restored_session.state == "Idle"
    assert "pending_interaction" not in restored_session.context


@pytest.mark.asyncio
async def test_interrupt_manager_cancels_and_restores_idle(tmp_path) -> None:
    """Cancel decisions should be persisted without executing the pending interaction."""

    current_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def now_factory() -> datetime:
        return current_time

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        manager = SessionInterruptManager(store, now_factory=now_factory)
        await manager.create(
            session,
            kind="confirm",
            correlation_id="confirm-cancel",
            expires_at=current_time + timedelta(minutes=10),
            payload={"action": "发送通知"},
        )

        resolution = await manager.cancel(
            "confirm-cancel",
            "user_cancelled",
            {"approved": False},
            responder="user-1",
        )
        cancelled = await store.get_pending_interaction("confirm-cancel")
        restored_session = await store.get_session(session.session_id)

    assert resolution.status == "cancelled"
    assert resolution.reason == "user_cancelled"
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.resolution == {
        "kind": "confirm",
        "status": "cancelled",
        "responder": "user-1",
        "payload": {"approved": False},
        "reason": "user_cancelled",
        "resolved_at": "2026-01-01T12:00:00+00:00",
    }
    assert restored_session is not None
    assert restored_session.state == "Idle"


@pytest.mark.asyncio
async def test_interrupt_resolution_rejects_expired_reply(tmp_path) -> None:
    """A late reply must not resolve or resume an expired interrupt."""

    current_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def now_factory() -> datetime:
        return current_time

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        manager = SessionInterruptManager(store, now_factory=now_factory)
        await manager.create(
            session,
            kind="confirm",
            correlation_id="confirm-expired",
            expires_at=current_time + timedelta(seconds=1),
            payload={"action": "发送邮件"},
        )

        current_time += timedelta(seconds=2)
        with pytest.raises(SessionInterruptExpired):
            await manager.resolve("confirm-expired", {"approved": True}, responder="user-1")

        pending = await store.get_pending_interaction("confirm-expired")
        stored_session = await store.get_session(session.session_id)

    assert pending is not None
    assert pending.status == "pending"
    assert stored_session is not None
    assert stored_session.state == "AwaitingInteraction"


async def _stored_session(store: SQLiteStore) -> Session:
    await store.initialize()
    record = await store.upsert_session(
        SessionRecord(
            session_id="dingtalk:dm:conversation-1",
            conversation_id="conversation-1",
            kind="dm",
            bot_id="robot-code",
            principal_id="user:user-1",
            actor_id="user-1",
            context={"platform": "dingtalk"},
        )
    )
    return Session(
        session_id=record.session_id,
        conversation_id=record.conversation_id,
        kind=record.kind,
        bot=BotIdentity(id=record.bot_id),
        principal=Principal(kind="user", id=record.principal_id),
        actor=Actor(id="user-1", display_name="Alice"),
        context=record.context,
        state=record.state,
        lifecycle=record.lifecycle,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
