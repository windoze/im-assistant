"""Tests for T35 audit logging integration points."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from src.capabilities import Authorizer, Granted, Requirement
from src.core import (
    Actor,
    BotIdentity,
    Command,
    CommandArgsSpec,
    CommandContext,
    CommandRegistry,
    Principal,
    Session,
    SessionInterruptManager,
)
from src.infra.audit import AuditLogger
from src.infra.config import OAuthConfig
from src.infra.oauth import PendingAuthStore
from src.infra.store import SessionRecord, SQLiteStore
from src.infra.token_vault import TokenVaultResolution, UserToken


@pytest.mark.asyncio
async def test_authorizer_audits_granted_obo_token(tmp_path) -> None:
    """OBO authorization grants should leave token-free audit evidence."""

    token = UserToken(
        principal_id="user:user-1",
        service="calendar",
        user_access_token="user-access-token",
        refresh_token="refresh-token",
        scopes=("calendar:read", "calendar:write"),
        expires_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        authorizer = Authorizer(
            token_vault=FakeTokenVault(TokenVaultResolution(token=token)),
            pending_store=PendingAuthStore(),
            dingtalk_client=FakeDingTalkClient(),
            oauth_config=OAuthConfig(redirect_uri="https://assistant.example.com/oauth/callback"),
            audit_logger=AuditLogger(store),
        )

        result = await authorizer.resolve(
            Requirement(service="calendar", scopes=("calendar:read",), on_behalf_of="actor"),
            session.actor,
            session.kind,
            principal_id=session.principal.id,
            session_id=session.session_id,
        )
        audit_logs = await store.list_audit_logs()

    assert isinstance(result, Granted)
    assert len(audit_logs) == 1
    audit = audit_logs[0]
    assert audit.event_type == "obo_authorization"
    assert audit.actor_id == "user-1"
    assert audit.principal_id == "user:user-1"
    assert audit.session_id == "dingtalk:dm:conversation-1"
    assert audit.scope == "calendar:read"
    assert audit.action == "granted"
    assert audit.metadata == {
        "decision": "granted",
        "mode": "dm",
        "on_behalf_of": "actor",
        "refreshed": False,
        "scopes": ["calendar:read"],
        "service": "calendar",
    }


@pytest.mark.asyncio
async def test_interrupt_manager_audits_confirm_and_cancel_decisions(tmp_path) -> None:
    """Confirm approvals and cancellations should be queryable as audit decisions."""

    current_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def now_factory() -> datetime:
        return current_time

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        manager = SessionInterruptManager(
            store,
            now_factory=now_factory,
            audit_logger=AuditLogger(store),
        )
        await manager.create(
            session,
            kind="confirm",
            correlation_id="confirm-approved",
            responder="user-1",
            expires_at=current_time + timedelta(minutes=5),
            payload={
                "capability": "send_notification",
                "action": "发送钉钉通知",
                "details": {"target": "user:user-1", "content": "开会提醒"},
            },
        )
        await manager.resolve("confirm-approved", {"approved": True}, responder="user-1")
        await manager.create(
            session,
            kind="confirm",
            correlation_id="confirm-cancelled",
            responder="user-1",
            expires_at=current_time + timedelta(minutes=5),
            payload={
                "capability": "send_notification",
                "action": "发送钉钉通知",
                "details": {"target": "user:user-1", "content": "不要发送"},
            },
        )
        await manager.cancel(
            "confirm-cancelled",
            "user_cancelled",
            {"approved": False},
            responder="user-1",
        )
        audit_logs = await store.list_audit_logs()

    assert [(audit.event_type, audit.action) for audit in audit_logs] == [
        ("interaction_decision", "confirm.resolved"),
        ("interaction_decision", "confirm.cancelled"),
    ]
    assert audit_logs[0].actor_id == "user-1"
    assert audit_logs[0].principal_id == "user:user-1"
    assert audit_logs[0].scope == "发送钉钉通知"
    assert audit_logs[0].metadata["resolution"] == {"approved": True}
    assert audit_logs[0].metadata["details"] == {
        "target": "user:user-1",
        "content": "开会提醒",
    }
    assert audit_logs[1].metadata["reason"] == "user_cancelled"
    assert audit_logs[1].metadata["resolution"] == {"approved": False}


@pytest.mark.asyncio
async def test_command_registry_audits_command_execution(tmp_path) -> None:
    """Slash command dispatch should record who ran which deterministic action."""

    async def handler(context: CommandContext) -> str:
        return f"mode={context.args[0]}"

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        registry = CommandRegistry(
            store,
            commands=[
                Command(
                    "/mode",
                    handler,
                    args_spec=CommandArgsSpec(min_args=1, max_args=1),
                    description="Set response mode",
                )
            ],
            audit_logger=AuditLogger(store),
        )

        reply = await registry.handle_command(session, "/mode concise", object())
        audit_logs = await store.list_audit_logs()

    assert reply == "mode=concise"
    assert len(audit_logs) == 1
    audit = audit_logs[0]
    assert audit.event_type == "command_execution"
    assert audit.actor_id == "user-1"
    assert audit.principal_id == "user:user-1"
    assert audit.session_id == "dingtalk:dm:conversation-1"
    assert audit.scope == "command:mode"
    assert audit.action == "/mode"
    assert audit.metadata == {
        "args": ["concise"],
        "args_text": "concise",
        "available_in": ["dm", "group"],
        "command": "/mode",
        "outcome": "executed",
        "requires_role": "user",
        "session_kind": "dm",
    }


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


@dataclass(frozen=True, slots=True)
class FakeDingTalkClient:
    """DingTalk client double for Authorizer tests."""

    async def refresh_user_access_token(self, refresh_token: str) -> object:
        raise AssertionError(f"refresh should not be called in this test: {refresh_token}")


class FakeTokenVault:
    """TokenVault test double returning a scripted resolution."""

    def __init__(self, resolution: TokenVaultResolution) -> None:
        self._resolution = resolution

    async def get_valid(
        self,
        principal: str,
        service: str,
        **_kwargs: object,
    ) -> TokenVaultResolution:
        assert principal == "user:user-1"
        assert service == "calendar"
        return self._resolution
