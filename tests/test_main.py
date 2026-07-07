"""Smoke tests for the initial application entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from src.adapters.dingtalk import InboundEvent, InboundMessage, UnsupportedInboundMessage
from src.core import (
    COMMANDS_NOT_CONFIGURED_REPLY,
    GROUP_WELCOME_REPLY,
    Actor,
    AgentRunResult,
    BotIdentity,
    InteractionCancellationResult,
    PendingInteractionInfo,
    Principal,
    Session,
    SessionRouteResult,
)
from src.infra.store import PendingInteractionRecord, SessionRecord, SQLiteStore
from src.main import (
    ASSISTANT_SYSTEM_PROMPT,
    InteractionTimeoutScheduler,
    handle_inbound_event,
    main,
    schedule_persisted_interaction_timeouts,
)


def test_main_logs_startup(caplog) -> None:
    """The entry point should start cleanly and emit a startup log."""
    with caplog.at_level(logging.INFO):
        asyncio.run(main())

    assert "DingTalk AI assistant starting" in caplog.text


@pytest.mark.asyncio
async def test_handle_inbound_event_replies_to_triggered_text_message() -> None:
    outbound = FakeOutbound()
    llm_client = FakeLLMClient("LLM reply")
    event = _text_event()

    await handle_inbound_event(event, outbound=outbound, llm_client=llm_client)

    assert llm_client.calls == [
        (
            ASSISTANT_SYSTEM_PROMPT,
            [{"role": "user", "content": "hello"}],
        )
    ]
    assert outbound.replies == [(event, "LLM reply")]


@pytest.mark.asyncio
async def test_handle_inbound_event_routes_session_and_sends_group_welcome() -> None:
    outbound = FakeOutbound()
    llm_client = FakeLLMClient("LLM reply")
    event = _text_event(
        conversation_type=2,
        conversation_id="group-conversation",
        open_conversation_id="open-group-1",
    )
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=Session(
                session_id="dingtalk:group:group-conversation",
                conversation_id="group-conversation",
                kind="group",
                bot=BotIdentity(id="robot-code"),
                principal=Principal(kind="group", id="group:open-group-1"),
                actor=Actor(id="user-1", display_name="Alice"),
            ),
            created=True,
            should_send_welcome=True,
        )
    )

    await handle_inbound_event(
        event,
        outbound=outbound,
        llm_client=llm_client,
        session_manager=session_manager,
    )

    assert session_manager.events == [event]
    assert llm_client.calls == [
        (
            ASSISTANT_SYSTEM_PROMPT,
            [{"role": "user", "content": "hello"}],
        )
    ]
    assert outbound.replies == [(event, GROUP_WELCOME_REPLY), (event, "LLM reply")]


@pytest.mark.asyncio
async def test_handle_inbound_event_uses_agent_loop_for_routed_text_message() -> None:
    outbound = FakeOutbound()
    event = _text_event(
        conversation_type=2,
        conversation_id="group-conversation",
        open_conversation_id="open-group-1",
    )
    routed_session = Session(
        session_id="dingtalk:group:group-conversation",
        conversation_id="group-conversation",
        kind="group",
        bot=BotIdentity(id="robot-code"),
        principal=Principal(kind="group", id="group:open-group-1"),
        actor=Actor(id="user-1", display_name="Alice"),
    )
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=routed_session,
            created=False,
            should_send_welcome=False,
        )
    )
    agent_loop = FakeAgentLoop("loop reply")

    await handle_inbound_event(
        event,
        outbound=outbound,
        session_manager=session_manager,
        agent_loop=agent_loop,
    )

    assert agent_loop.calls == [(routed_session, "hello", "user-1", "msg-1")]
    assert outbound.replies == [(event, "loop reply")]


@pytest.mark.asyncio
async def test_handle_inbound_event_routes_slash_command_to_handler() -> None:
    outbound = FakeOutbound()
    event = _text_event(text=" /help")
    routed_session = _session()
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=routed_session,
            created=False,
            should_send_welcome=False,
        )
    )
    agent_loop = FakeAgentLoop("unused")
    command_handler = FakeCommandHandler("command reply")

    await handle_inbound_event(
        event,
        outbound=outbound,
        session_manager=session_manager,
        agent_loop=agent_loop,
        command_handler=command_handler,
    )

    assert command_handler.calls == [(routed_session, "/help", event)]
    assert agent_loop.calls == []
    assert outbound.replies == [(event, "command reply")]


@pytest.mark.asyncio
async def test_handle_inbound_event_routes_group_mention_slash_command_to_handler() -> None:
    outbound = FakeOutbound()
    event = _text_event(
        text="@助手 /help",
        conversation_type=2,
        conversation_id="group-conversation",
        open_conversation_id="open-group-1",
    )
    routed_session = _session(kind="group")
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=routed_session,
            created=False,
            should_send_welcome=False,
        )
    )
    agent_loop = FakeAgentLoop("unused")
    command_handler = FakeCommandHandler("command reply")

    await handle_inbound_event(
        event,
        outbound=outbound,
        session_manager=session_manager,
        agent_loop=agent_loop,
        command_handler=command_handler,
    )

    assert command_handler.calls == [(routed_session, "/help", event)]
    assert agent_loop.calls == []
    assert outbound.replies == [(event, "command reply")]


@pytest.mark.asyncio
async def test_handle_inbound_event_keeps_unconfigured_slash_command_out_of_llm() -> None:
    outbound = FakeOutbound()
    event = _text_event(text="/help")
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=_session(),
            created=False,
            should_send_welcome=False,
        )
    )
    agent_loop = FakeAgentLoop("unused")

    await handle_inbound_event(
        event,
        outbound=outbound,
        session_manager=session_manager,
        agent_loop=agent_loop,
    )

    assert agent_loop.calls == []
    assert outbound.replies == [(event, COMMANDS_NOT_CONFIGURED_REPLY)]


@pytest.mark.asyncio
async def test_handle_inbound_event_cancels_pending_interaction_before_new_message() -> None:
    outbound = FakeOutbound()
    event = _text_event(text="新的问题", msg_id="msg-2")
    awaiting_session = _session(state="AwaitingInteraction")
    restored_session = _session(state="Idle")
    cancellation = InteractionCancellationResult(
        correlation_id="confirm-1",
        kind="confirm",
        reason="superseded_by_new_message",
        notice_text="已取消:未确认，[发送钉钉通知] 未执行。",
        session=restored_session,
    )
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=awaiting_session,
            created=False,
            should_send_welcome=False,
        )
    )
    agent_loop = FakeAgentLoop("新消息回复", cancellation_result=cancellation)

    await handle_inbound_event(
        event,
        outbound=outbound,
        session_manager=session_manager,
        agent_loop=agent_loop,
    )

    assert agent_loop.cancellations == [
        (awaiting_session, "superseded_by_new_message", "user-1", "msg-2")
    ]
    assert agent_loop.calls == [(restored_session, "新的问题", "user-1", "msg-2")]
    assert outbound.replies == [
        (event, "已取消:未确认，[发送钉钉通知] 未执行。"),
        (event, "新消息回复"),
    ]


@pytest.mark.asyncio
async def test_pending_interaction_route_has_priority_over_slash_command() -> None:
    outbound = FakeOutbound()
    event = _text_event(text="/help", msg_id="msg-2")
    awaiting_session = _session(state="AwaitingInteraction")
    restored_session = _session(state="Idle")
    cancellation = InteractionCancellationResult(
        correlation_id="confirm-1",
        kind="confirm",
        reason="superseded_by_new_message",
        notice_text="已取消:未确认，[发送钉钉通知] 未执行。",
        session=restored_session,
    )
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=awaiting_session,
            created=False,
            should_send_welcome=False,
        )
    )
    agent_loop = FakeAgentLoop("unused", cancellation_result=cancellation)
    command_handler = FakeCommandHandler("command reply")

    await handle_inbound_event(
        event,
        outbound=outbound,
        session_manager=session_manager,
        agent_loop=agent_loop,
        command_handler=command_handler,
    )

    assert agent_loop.cancellations == [
        (awaiting_session, "superseded_by_new_message", "user-1", "msg-2")
    ]
    assert command_handler.calls == [(restored_session, "/help", event)]
    assert agent_loop.calls == []
    assert outbound.replies == [
        (event, "已取消:未确认，[发送钉钉通知] 未执行。"),
        (event, "command reply"),
    ]


@pytest.mark.asyncio
async def test_cancel_command_handles_pending_interaction_without_generic_supersede() -> None:
    outbound = FakeOutbound()
    event = _text_event(text="/cancel", msg_id="msg-cancel")
    awaiting_session = _session(state="AwaitingInteraction")
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=awaiting_session,
            created=False,
            should_send_welcome=False,
        )
    )
    agent_loop = FakeAgentLoop("unused")
    command_handler = FakeCommandHandler("已取消:用户主动取消，[发送钉钉通知] 未执行。")

    await handle_inbound_event(
        event,
        outbound=outbound,
        session_manager=session_manager,
        agent_loop=agent_loop,
        command_handler=command_handler,
    )

    assert agent_loop.cancellations == []
    assert command_handler.calls == [(awaiting_session, "/cancel", event)]
    assert outbound.replies == [(event, "已取消:用户主动取消，[发送钉钉通知] 未执行。")]


@pytest.mark.asyncio
async def test_group_mention_cancel_command_handles_pending_interaction() -> None:
    outbound = FakeOutbound()
    event = _text_event(
        text="@助手 /cancel",
        msg_id="msg-cancel",
        conversation_type=2,
        conversation_id="group-conversation",
        open_conversation_id="open-group-1",
    )
    awaiting_session = _session(kind="group", state="AwaitingInteraction")
    session_manager = FakeSessionManager(
        SessionRouteResult(
            session=awaiting_session,
            created=False,
            should_send_welcome=False,
        )
    )
    agent_loop = FakeAgentLoop("unused")
    command_handler = FakeCommandHandler("已取消:用户主动取消，[发送钉钉通知] 未执行。")

    await handle_inbound_event(
        event,
        outbound=outbound,
        session_manager=session_manager,
        agent_loop=agent_loop,
        command_handler=command_handler,
    )

    assert agent_loop.cancellations == []
    assert command_handler.calls == [(awaiting_session, "/cancel", event)]
    assert outbound.replies == [(event, "已取消:用户主动取消，[发送钉钉通知] 未执行。")]


@pytest.mark.asyncio
async def test_interaction_timeout_scheduler_sends_system_notice() -> None:
    outbound = FakeOutbound()
    event = _text_event()
    session = _session(state="AwaitingInteraction")
    cancellation = InteractionCancellationResult(
        correlation_id="confirm-timeout",
        kind="confirm",
        reason="timeout",
        notice_text="已取消:确认超时，[发送钉钉通知] 未执行。",
        session=_session(state="Idle"),
    )
    agent_loop = FakeAgentLoop("unused", cancellation_result=cancellation)
    scheduler = InteractionTimeoutScheduler(agent_loop, outbound)

    scheduler.schedule(
        event,
        session,
        PendingInteractionInfo(
            correlation_id="confirm-timeout",
            kind="confirm",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        ),
    )
    for _ in range(10):
        if outbound.replies:
            break
        await asyncio.sleep(0)
    await scheduler.aclose()

    assert agent_loop.cancellations_by_id == [("confirm-timeout", "timeout", None, None)]
    assert outbound.replies == [(event, "已取消:确认超时，[发送钉钉通知] 未执行。")]


@pytest.mark.asyncio
async def test_persisted_pending_interaction_timeout_is_scheduled_after_restart(tmp_path) -> None:
    """Pending interactions restored from SQLite should still timeout and notify users."""

    outbound = FakeOutbound()
    cancellation = InteractionCancellationResult(
        correlation_id="confirm-recovered",
        kind="confirm",
        reason="timeout",
        notice_text="已取消:确认超时，[发送钉钉通知] 未执行。",
        session=_session(state="Idle"),
    )
    agent_loop = FakeAgentLoop("unused", cancellation_result=cancellation)
    scheduler = InteractionTimeoutScheduler(agent_loop, outbound)

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        await store.upsert_session(
            SessionRecord(
                session_id="dingtalk:group:conversation-1",
                conversation_id="conversation-1",
                kind="group",
                bot_id="robot-code",
                principal_id="group:open-conversation-1",
                actor_id="user-1",
                state="AwaitingInteraction",
                context={
                    "open_conversation_id": "open-conversation-1",
                    "last_actor_nick": "Alice",
                },
            )
        )
        await store.create_pending_interaction(
            PendingInteractionRecord(
                correlation_id="confirm-recovered",
                session_id="dingtalk:group:conversation-1",
                kind="confirm",
                responder_id="user-1",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
                payload={"action": "发送钉钉通知"},
            )
        )

        await schedule_persisted_interaction_timeouts(store, scheduler)
        for _ in range(10):
            if outbound.replies:
                break
            await asyncio.sleep(0)
        await scheduler.aclose()

    assert agent_loop.cancellations_by_id == [("confirm-recovered", "timeout", None, None)]
    assert len(outbound.replies) == 1
    target, notice_text = outbound.replies[0]
    assert notice_text == "已取消:确认超时，[发送钉钉通知] 未执行。"
    assert target.sender_staff_id == "user-1"
    assert target.conversation_type == 2
    assert target.conversation_id == "conversation-1"
    assert target.open_conversation_id == "open-conversation-1"
    assert target.session_webhook == ""
    assert target.msg_id == "pending:confirm-recovered"


@pytest.mark.asyncio
async def test_handle_inbound_event_replies_to_unsupported_message_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outbound = FakeOutbound()
    llm_client = FakeLLMClient("unused")
    event = UnsupportedInboundMessage(
        message_type="picture",
        sender_staff_id="user-1",
        sender_nick="Alice",
        conversation_type=2,
        conversation_id="conversation-1",
        open_conversation_id="open-conversation-1",
        session_webhook="https://webhook.example.com/session",
        msg_id="msg-1",
    )

    with caplog.at_level(logging.INFO):
        await handle_inbound_event(event, outbound=outbound, llm_client=llm_client)

    assert llm_client.calls == []
    assert outbound.replies == [(event, "暂只支持文本")]
    assert any(record.message == "dingtalk_unsupported_message_type" for record in caplog.records)


def _text_event(
    *,
    text: str = "hello",
    msg_id: str = "msg-1",
    conversation_type: int = 1,
    conversation_id: str = "conversation-1",
    open_conversation_id: str = "conversation-1",
) -> InboundMessage:
    return InboundMessage(
        text=text,
        sender_staff_id="user-1",
        sender_nick="Alice",
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        open_conversation_id=open_conversation_id,
        session_webhook="https://webhook.example.com/session",
        msg_id=msg_id,
    )


def _session(*, state: str = "Idle", kind: Literal["dm", "group"] = "dm") -> Session:
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
        state=state,
    )


class FakeOutbound:
    replies: list[tuple[object, str]]

    def __init__(self) -> None:
        self.replies = []

    async def reply(self, inbound: object, text: str) -> object:
        self.replies.append((inbound, text))
        return None


class FakeLLMClient:
    calls: list[tuple[str, list[dict[str, str]]]]

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls = []

    async def complete(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        self.calls.append((system, [dict(message) for message in messages]))
        return self._reply


class FakeAgentLoop:
    calls: list[tuple[Session, str, str | None, str | None]]

    def __init__(
        self,
        reply: str,
        *,
        cancellation_result: InteractionCancellationResult | None = None,
    ) -> None:
        self._reply = reply
        self._cancellation_result = cancellation_result
        self.calls = []
        self.cancellations: list[tuple[Session, str, str | None, str | None]] = []
        self.cancellations_by_id: list[tuple[str, str, str | None, str | None]] = []

    async def run(
        self,
        session: Session,
        user_text: str,
        *,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> AgentRunResult:
        self.calls.append((session, user_text, actor_id, provider_message_id))
        return AgentRunResult(reply_text=self._reply)

    async def cancel_pending_interaction_for_session(
        self,
        session: Session,
        *,
        reason: str,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> InteractionCancellationResult | None:
        self.cancellations.append((session, reason, actor_id, provider_message_id))
        return self._cancellation_result

    async def cancel_pending_interaction(
        self,
        correlation_id: str,
        *,
        reason: str,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> InteractionCancellationResult | None:
        self.cancellations_by_id.append((correlation_id, reason, actor_id, provider_message_id))
        return self._cancellation_result


class FakeCommandHandler:
    calls: list[tuple[Session | None, str, object]]

    def __init__(self, reply: str | None) -> None:
        self._reply = reply
        self.calls = []

    async def handle_command(
        self,
        session: Session | None,
        command_text: str,
        event: object,
    ) -> str | None:
        self.calls.append((session, command_text, event))
        return self._reply


class FakeSessionManager:
    events: list[InboundEvent]

    def __init__(self, result: SessionRouteResult) -> None:
        self._result = result
        self.events = []

    async def get_or_create_for_event(self, event: InboundEvent) -> SessionRouteResult:
        self.events.append(event)
        return self._result
