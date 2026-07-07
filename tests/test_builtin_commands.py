"""Tests for the first built-in deterministic slash commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest

from src.capabilities import Capability, CapabilityRegistry, Granted, NeedsConsent, Requirement
from src.core import (
    Actor,
    BotIdentity,
    InteractionCancellationResult,
    PendingInteractionInfo,
    Principal,
    Session,
    create_builtin_command_registry,
)
from src.infra.oauth import PendingAuth
from src.infra.store import (
    IdentityBindingRecord,
    PendingInteractionRecord,
    SessionRecord,
    SQLiteStore,
)
from src.infra.token_vault import UserToken


@pytest.mark.asyncio
async def test_help_lists_available_commands_and_capabilities(tmp_path) -> None:
    """`/help` should report slash commands and capabilities visible to the actor."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        registry = create_builtin_command_registry(
            store,
            capability_registry_factory=lambda _session: _capability_registry(),
            token_vault=FakeTokenVault(),
        )

        reply = await registry.handle_command(_session(), "/help", object())

    assert "可用指令:" in reply
    assert "/help: 列出当前可用能力和指令" in reply
    assert "/connect: 预热用户授权: /connect <service>" in reply
    assert "可用能力:" in reply
    assert "contact_lookup" in reply
    assert "schedule_summary" in reply


@pytest.mark.asyncio
async def test_reset_clears_history_and_non_operational_context(tmp_path) -> None:
    """`/reset` should remove persisted chat history and clear transient context."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        session = await _stored_session(
            store,
            context={
                "open_conversation_id": "conversation-1",
                "custom_memory": "remove-me",
            },
        )
        await store.add_message(session_id=session.session_id, role="user", content="hello")
        await store.add_message(session_id=session.session_id, role="assistant", content="hi")
        registry = create_builtin_command_registry(store)

        reply = await registry.handle_command(session, "/reset", object())
        stored_messages = await store.list_messages(session.session_id)
        stored_session = await store.get_session(session.session_id)

    assert reply == "已重置当前会话，上下文消息已清空（2 条）。"
    assert stored_messages == []
    assert stored_session is not None
    assert stored_session.state == "Idle"
    assert stored_session.context == {"open_conversation_id": "conversation-1"}


@pytest.mark.asyncio
async def test_whoami_reports_identity_binding_and_authorization(tmp_path) -> None:
    """`/whoami` should show actor identity plus known OBO authorization status."""

    token = UserToken(
        principal_id="user:user-1",
        service="calendar",
        user_access_token="access",
        refresh_token="refresh",
        scopes=("calendar:read",),
        expires_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )
    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        await store.upsert_identity_binding(
            IdentityBindingRecord(
                provider="dingtalk",
                external_user_id="user-1",
                principal_id="user:user-1",
                union_id="union-1",
                display_name="Alice",
            )
        )
        registry = create_builtin_command_registry(
            store,
            capability_registry_factory=lambda _session: _capability_registry(),
            token_vault=FakeTokenVault(tokens={("user:user-1", "calendar"): token}),
        )

        reply = await registry.handle_command(_session(), "/whoami", object())

    assert "当前用户: Alice (user-1)" in reply
    assert "身份绑定: 已绑定（principal=user:user-1; unionId=union-1; name=Alice）" in reply
    assert "- calendar: 已授权（scopes: calendar:read;" in reply


@pytest.mark.asyncio
async def test_connect_calendar_creates_consent_interrupt_and_schedules_timeout(tmp_path) -> None:
    """`/connect <service>` should pre-create the same consent flow used by tools."""

    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    pending = PendingAuth(
        nonce="nonce-1",
        principal_id="user:user-1",
        actor_id="union-1",
        session_id="dingtalk:dm:conversation-1",
        service="calendar",
        scopes=("calendar:read",),
        expires_at=expires_at,
    )
    authorizer = FakeAuthorizer(
        NeedsConsent(
            url="https://assistant.example.com/oauth/start?nonce=nonce-1",
            pending=pending,
            reason="missing",
        )
    )
    interrupt_manager = FakeInterruptManager()
    scheduler = FakeTimeoutScheduler()
    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        registry = create_builtin_command_registry(
            store,
            capability_registry_factory=lambda _session: _capability_registry(),
            authorizer=authorizer,
            interrupt_manager=interrupt_manager,
            timeout_scheduler=scheduler,
        )
        event = FakeEvent(msg_id="msg-connect")

        reply = await registry.handle_command(_session(), "/connect calendar", event)

    assert authorizer.calls == [
        (
            Requirement(service="calendar", scopes=("calendar:read",), on_behalf_of="actor"),
            "user-1",
            "dm",
            "user:user-1",
            "dingtalk:dm:conversation-1",
        )
    ]
    assert interrupt_manager.creates == [
        {
            "session_id": "dingtalk:dm:conversation-1",
            "kind": "consent",
            "correlation_id": "nonce-1",
            "responder": "user-1",
            "expires_at": expires_at,
            "payload": {
                "source": "command",
                "command": "/connect",
                "service": "calendar",
                "scopes": ["calendar:read"],
                "url": "https://assistant.example.com/oauth/start?nonce=nonce-1",
                "reason": "missing",
            },
        }
    ]
    assert scheduler.calls[0][0] is event
    assert scheduler.calls[0][2].correlation_id == "nonce-1"
    assert "请打开链接完成授权：https://assistant.example.com/oauth/start?nonce=nonce-1" in reply


@pytest.mark.asyncio
async def test_connect_reports_existing_authorization_without_interrupt(tmp_path) -> None:
    """`/connect` should not create a consent interrupt when the grant is already valid."""

    authorizer = FakeAuthorizer(
        Granted(
            handle=object(),  # type: ignore[arg-type]
        )
    )
    interrupt_manager = FakeInterruptManager()
    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        registry = create_builtin_command_registry(
            store,
            capability_registry_factory=lambda _session: _capability_registry(),
            authorizer=authorizer,
            interrupt_manager=interrupt_manager,
        )

        reply = await registry.handle_command(_session(), "/connect calendar", object())

    assert reply == "已连接 calendar，无需重新授权。"
    assert interrupt_manager.creates == []


@pytest.mark.asyncio
async def test_disconnect_revokes_token_vault_grant(tmp_path) -> None:
    """`/disconnect <service>` should remove the user's TokenVault grant."""

    token_vault = FakeTokenVault(revoke_result=True)
    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        registry = create_builtin_command_registry(store, token_vault=token_vault)

        reply = await registry.handle_command(_session(), "/disconnect calendar", object())

    assert reply == "已断开 calendar 授权。"
    assert token_vault.revocations == [("user:user-1", "calendar")]


@pytest.mark.asyncio
async def test_cancel_cancels_current_pending_interaction(tmp_path) -> None:
    """`/cancel` should cancel the active pending interaction through the agent loop."""

    cancellation = InteractionCancellationResult(
        correlation_id="confirm-1",
        kind="confirm",
        reason="command_cancelled",
        notice_text="已取消:用户主动取消，[发送钉钉通知] 未执行。",
        session=_session(),
    )
    canceller = FakeCanceller(cancellation)
    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        session = await _stored_session(store, state="AwaitingInteraction")
        await store.create_pending_interaction(
            PendingInteractionRecord(
                correlation_id="confirm-1",
                session_id=session.session_id,
                kind="confirm",
                responder_id="user-1",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                payload={"action": "发送钉钉通知"},
            )
        )
        registry = create_builtin_command_registry(
            store,
            interaction_canceller=canceller,
        )
        event = FakeEvent(msg_id="msg-cancel")

        reply = await registry.handle_command(session, "/cancel", event)

    assert reply == "已取消:用户主动取消，[发送钉钉通知] 未执行。"
    assert canceller.calls == [
        (session, "command_cancelled", "user-1", "msg-cancel"),
    ]


def _capability_registry() -> CapabilityRegistry:
    async def handler() -> str:
        return "ok"

    return CapabilityRegistry(
        [
            Capability(
                name="contact_lookup",
                origin="system",
                available_in=["global"],
                handler=handler,
                description="查询通讯录",
            ),
            Capability(
                name="schedule_summary",
                origin="system",
                available_in=["dm"],
                requires=[
                    Requirement(
                        service="calendar",
                        scopes=("calendar:read",),
                        on_behalf_of="actor",
                    )
                ],
                handler=handler,
                description="总结今日日程",
            ),
        ]
    )


async def _stored_session(
    store: SQLiteStore,
    *,
    state: str = "Idle",
    context: Mapping[str, object] | None = None,
) -> Session:
    record = await store.upsert_session(
        SessionRecord(
            session_id="dingtalk:dm:conversation-1",
            conversation_id="conversation-1",
            kind="dm",
            bot_id="robot-code",
            principal_id="user:user-1",
            actor_id="user-1",
            state=state,
            context=context or {},
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


def _session(
    *,
    kind: Literal["dm", "group"] = "dm",
    state: str = "Idle",
    context: Mapping[str, object] | None = None,
) -> Session:
    return Session(
        session_id=f"dingtalk:{kind}:conversation-1",
        conversation_id="conversation-1",
        kind=kind,
        bot=BotIdentity(id="robot-code"),
        principal=Principal(
            kind="group" if kind == "group" else "user",
            id="group:open-group-1" if kind == "group" else "user:user-1",
        ),
        actor=Actor(id="user-1", display_name="Alice"),
        context=context or {},
        state=state,
    )


@dataclass(frozen=True, slots=True)
class FakeEvent:
    """Command event test double."""

    msg_id: str


class FakeTokenVault:
    """TokenVault double for command tests."""

    def __init__(
        self,
        *,
        tokens: Mapping[tuple[str, str], UserToken] | None = None,
        revoke_result: bool = False,
    ) -> None:
        self._tokens = dict(tokens or {})
        self._revoke_result = revoke_result
        self.revocations: list[tuple[str, str]] = []

    async def get(self, principal: str, service: str) -> UserToken | None:
        return self._tokens.get((principal, service))

    async def revoke(self, principal: str, service: str) -> bool:
        self.revocations.append((principal, service))
        return self._revoke_result


class FakeAuthorizer:
    """Authorizer double returning one scripted resolution."""

    def __init__(self, resolution: object) -> None:
        self._resolution = resolution
        self.calls: list[tuple[Requirement, str, str, str | None, str | None]] = []

    async def resolve(
        self,
        requirement: Requirement,
        actor: object,
        mode: str,
        *,
        principal_id: str | None = None,
        session_id: str | None = None,
    ) -> object:
        self.calls.append(
            (
                requirement,
                actor.id,
                mode,
                principal_id,
                session_id,
            )
        )
        return self._resolution


class FakeInterruptManager:
    """Interrupt manager double recording consent creation."""

    def __init__(self) -> None:
        self.creates: list[dict[str, object]] = []

    async def create(
        self,
        session: Session,
        *,
        kind: str,
        payload: Mapping[str, Any],
        correlation_id: str,
        responder: str | None = None,
        expires_at: datetime | None = None,
        ttl_seconds: int = 1800,
    ) -> object:
        self.creates.append(
            {
                "session_id": session.session_id,
                "kind": kind,
                "correlation_id": correlation_id,
                "responder": responder,
                "expires_at": expires_at,
                "payload": dict(payload),
            }
        )
        return object()


class FakeTimeoutScheduler:
    """Timeout scheduler double recording command-created pending interactions."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, Session, PendingInteractionInfo | None]] = []

    def schedule(
        self,
        event: object,
        session: Session,
        pending: PendingInteractionInfo | None,
    ) -> None:
        self.calls.append((event, session, pending))


class FakeCanceller:
    """Agent-loop cancellation double."""

    def __init__(self, result: InteractionCancellationResult | None) -> None:
        self._result = result
        self.calls: list[tuple[Session, str, str | None, str | None]] = []

    async def cancel_pending_interaction_for_session(
        self,
        session: Session,
        *,
        reason: str,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> InteractionCancellationResult | None:
        self.calls.append((session, reason, actor_id, provider_message_id))
        return self._result
