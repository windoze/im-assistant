"""Async application entry point for the DingTalk AI assistant."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from src.infra.log import configure_logging, get_logger

if TYPE_CHECKING:
    from src.adapters.dingtalk import InboundEvent, InboundMessage
    from src.core.agent_loop import (
        AgentRunResult,
        InteractionCancellationReason,
        InteractionCancellationResult,
        PendingInteractionInfo,
    )
    from src.core.session import Session
    from src.core.session_manager import SessionRouteResult
    from src.infra.config import AppConfig
    from src.infra.store import PendingInteractionRecord, SessionRecord

logger = get_logger("im_assistant")
ASSISTANT_SYSTEM_PROMPT = "你是企业内 AI 助手。请简洁、准确地回答用户问题。"


class ReplySender(Protocol):
    """Protocol for objects that can reply to DingTalk inbound events."""

    async def reply(self, inbound: object, text: str) -> object:
        """Send a reply to the source conversation for an inbound event."""


class TextCompleter(Protocol):
    """Protocol for objects that can complete one assistant text turn."""

    async def complete(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        """Return a text completion for the supplied system prompt and chat messages."""


class SessionRouter(Protocol):
    """Protocol for objects that route inbound events to persistent Sessions."""

    async def get_or_create_for_event(self, event: InboundEvent) -> SessionRouteResult:
        """Return the persistent Session route for one inbound event."""


class AgentRunner(Protocol):
    """Protocol for the persistent multi-turn agent loop."""

    async def run(
        self,
        session: Session,
        user_text: str,
        *,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> AgentRunResult:
        """Run one agent turn for a routed Session."""

    async def cancel_pending_interaction_for_session(
        self,
        session: Session,
        *,
        reason: InteractionCancellationReason,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> InteractionCancellationResult | None:
        """Cancel a pending interaction and return the system notice to send."""

    async def cancel_pending_interaction(
        self,
        correlation_id: str,
        *,
        reason: InteractionCancellationReason,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> InteractionCancellationResult | None:
        """Cancel a pending interaction by correlation id."""


class PendingInteractionStore(Protocol):
    """Store surface needed to recover timeout scheduling after process restart."""

    async def list_pending_interactions(self) -> Sequence[PendingInteractionRecord]:
        """Return active pending interactions."""

    async def get_session(self, session_id: str) -> SessionRecord | None:
        """Return a persisted Session by id."""


@dataclass(frozen=True, slots=True)
class ScheduledInteractionInfo:
    """Timeout metadata for recovered pending interactions."""

    correlation_id: str
    kind: str
    expires_at: datetime


class InteractionTimeoutScheduler:
    """Schedule timeout cancellations for pending Session interactions."""

    def __init__(self, canceller: AgentRunner, outbound: ReplySender) -> None:
        self._canceller = canceller
        self._outbound = outbound
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def schedule(
        self,
        event: object,
        session: Session,
        pending: PendingInteractionInfo | ScheduledInteractionInfo | None,
    ) -> None:
        """Schedule a timeout notice for a pending interaction."""

        if pending is None:
            return
        existing = self._tasks.pop(pending.correlation_id, None)
        if existing is not None:
            existing.cancel()
        delay = max((_to_utc(pending.expires_at) - datetime.now(UTC)).total_seconds(), 0.0)
        self._tasks[pending.correlation_id] = asyncio.create_task(
            self._run_timeout(delay, event, session, pending),
            name=f"interaction-timeout-{pending.correlation_id}",
        )

    async def aclose(self) -> None:
        """Cancel all pending timeout tasks."""

        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_timeout(
        self,
        delay: float,
        event: object,
        session: Session,
        pending: PendingInteractionInfo | ScheduledInteractionInfo,
    ) -> None:
        try:
            await asyncio.sleep(delay)
            cancellation = await self._canceller.cancel_pending_interaction(
                pending.correlation_id,
                reason="timeout",
            )
            if cancellation is not None:
                await self._outbound.reply(event, cancellation.notice_text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "interaction_timeout_cancellation_failed",
                extra={
                    "correlation_id": pending.correlation_id,
                    "session_id": session.session_id,
                },
            )
        finally:
            self._tasks.pop(pending.correlation_id, None)


async def schedule_persisted_interaction_timeouts(
    store: PendingInteractionStore,
    timeout_scheduler: InteractionTimeoutScheduler,
) -> None:
    """Schedule timeout cancellation for pending interactions restored from SQLite."""

    for pending in await store.list_pending_interactions():
        session_record = await store.get_session(pending.session_id)
        if session_record is None:
            logger.warning(
                "pending_interaction_session_missing",
                extra={
                    "correlation_id": pending.correlation_id,
                    "session_id": pending.session_id,
                },
            )
            continue
        timeout_scheduler.schedule(
            _reply_target_from_pending(session_record, pending),
            _session_from_record(session_record, responder_id=pending.responder_id),
            ScheduledInteractionInfo(
                correlation_id=pending.correlation_id,
                kind=pending.kind,
                expires_at=pending.expires_at,
            ),
        )


async def main(*, start_stream: bool = False, config: AppConfig | None = None) -> None:
    """Start the assistant runtime."""

    configure_logging()
    logger.info("DingTalk AI assistant starting")

    if not start_stream:
        return

    from src.adapters.dingtalk import DingTalkOutbound, DingTalkStreamAdapter
    from src.capabilities import Authorizer, load_capability_registry
    from src.core import (
        AgentLoop,
        InteractionCallbackRouter,
        SessionInboxDispatcher,
        SessionManager,
    )
    from src.infra.config import load_config
    from src.infra.dingtalk_client import DingTalkClient
    from src.infra.llm import LLMClient
    from src.infra.oauth import PendingAuthStore
    from src.infra.store import SQLiteStore
    from src.infra.token_vault import TokenVault

    app_config = config or load_config()
    configure_logging(app_config.logging.level, force=True)

    async with SQLiteStore(app_config.storage.database_path) as store:
        await store.initialize()
        session_manager = SessionManager(store, bot_id=app_config.dingtalk.robot_code)
        token_vault = TokenVault.from_config(store, app_config.token_vault)
        pending_auth_store = PendingAuthStore()

        def capability_registry_for_session(session: Session):
            user_id = session.actor.id if session.kind == "dm" else None
            return load_capability_registry(user_id=user_id)

        async with DingTalkClient(app_config.dingtalk) as dingtalk_client:

            async def actor_union_id(actor: object) -> str:
                user = await dingtalk_client.user_by_id(
                    _required_string(getattr(actor, "id", None), "actor.id")
                )
                union_id = getattr(user, "union_id", None)
                if union_id is None and isinstance(getattr(user, "raw", None), Mapping):
                    union_id = user.raw.get("unionId")
                return _required_string(union_id, "actor.union_id")

            authorizer = Authorizer(
                token_vault=token_vault,
                pending_store=pending_auth_store,
                dingtalk_client=dingtalk_client,
                oauth_config=app_config.oauth,
                actor_identity_resolver=actor_union_id,
            )

            async with DingTalkOutbound(dingtalk_client) as outbound:
                async with LLMClient(app_config.llm) as llm_client:
                    agent_loop = AgentLoop(
                        store,
                        llm_client,
                        system_prompt=ASSISTANT_SYSTEM_PROMPT,
                        capability_registry_factory=capability_registry_for_session,
                        channel_enabled_capabilities=(
                            app_config.capabilities.channel_enabled_capabilities
                        ),
                        capability_services={
                            "dingtalk_client": dingtalk_client,
                            "llm_client": llm_client,
                            "dingtalk_document_defaults": {
                                "parent_object_type": (
                                    app_config.dingtalk.document.parent_object_type
                                ),
                                "parent_object_id": app_config.dingtalk.document.parent_object_id,
                            },
                        },
                        authorizer=authorizer,
                        confirm_card_sender=dingtalk_client,
                        confirm_timeout_seconds=app_config.session.confirm_timeout_sec,
                    )
                    callback_router = InteractionCallbackRouter(agent_loop)
                    timeout_scheduler = InteractionTimeoutScheduler(agent_loop, outbound)
                    await schedule_persisted_interaction_timeouts(store, timeout_scheduler)

                    async def process_event(event: InboundEvent) -> None:
                        await handle_inbound_event(
                            event,
                            outbound=outbound,
                            session_manager=session_manager,
                            agent_loop=agent_loop,
                            interaction_timeout_scheduler=timeout_scheduler,
                        )

                    inbox_dispatcher = SessionInboxDispatcher(process_event)

                    async def on_event(event: InboundEvent) -> None:
                        await inbox_dispatcher.enqueue(event)

                    try:
                        await DingTalkStreamAdapter(
                            app_config.dingtalk,
                            on_event,
                            on_card_callback=callback_router.handle_card_callback,
                        ).start()
                    finally:
                        await inbox_dispatcher.close()
                        await timeout_scheduler.aclose()


async def handle_inbound_event(
    event: InboundEvent,
    *,
    outbound: ReplySender,
    llm_client: TextCompleter | None = None,
    session_manager: SessionRouter | None = None,
    agent_loop: AgentRunner | None = None,
    interaction_timeout_scheduler: InteractionTimeoutScheduler | None = None,
) -> None:
    """Apply trigger rules and route a normalized DingTalk inbound event."""

    from src.adapters.dingtalk import (
        UNSUPPORTED_MESSAGE_REPLY,
        UnsupportedInboundMessage,
        is_triggered,
    )

    if not is_triggered(event):
        logger.debug(
            "dingtalk_inbound_message_ignored",
            extra={"msg_id": event.msg_id, "conversation_type": event.conversation_type},
        )
        return

    session: Session | None = None
    if session_manager is not None:
        from src.core import GROUP_WELCOME_REPLY

        session_route = await session_manager.get_or_create_for_event(event)
        session = session_route.session
        logger.debug(
            "dingtalk_session_routed",
            extra={
                "msg_id": event.msg_id,
                "session_id": session.session_id,
                "kind": session.kind,
                "actor_id": session.actor.id,
                "created": session_route.created,
            },
        )
        if session_route.should_send_welcome:
            await outbound.reply(event, GROUP_WELCOME_REPLY)
        if session.state == "AwaitingInteraction" and agent_loop is not None:
            cancellation = await agent_loop.cancel_pending_interaction_for_session(
                session,
                reason="superseded_by_new_message",
                actor_id=event.sender_staff_id,
                provider_message_id=event.msg_id,
            )
            if cancellation is not None:
                await outbound.reply(event, cancellation.notice_text)
                session = cancellation.session

    if isinstance(event, UnsupportedInboundMessage):
        logger.info(
            "dingtalk_unsupported_message_type",
            extra={"msg_id": event.msg_id, "message_type": event.message_type},
        )
        await outbound.reply(event, UNSUPPORTED_MESSAGE_REPLY)
        return

    await _on_inbound_message(
        event,
        outbound=outbound,
        llm_client=llm_client,
        session=session,
        agent_loop=agent_loop,
        interaction_timeout_scheduler=interaction_timeout_scheduler,
    )


async def _on_inbound_message(
    message: InboundMessage,
    *,
    outbound: ReplySender,
    llm_client: TextCompleter | None,
    session: Session | None = None,
    agent_loop: AgentRunner | None = None,
    interaction_timeout_scheduler: InteractionTimeoutScheduler | None = None,
) -> None:
    """Complete one LLM turn and reply to the DingTalk conversation."""

    logger.debug(
        "dingtalk_inbound_message_accepted",
        extra={
            "msg_id": message.msg_id,
            "session_id": None if session is None else session.session_id,
        },
    )
    if agent_loop is not None:
        if session is None:
            raise ValueError("agent_loop requires a routed Session")
        result = await agent_loop.run(
            session,
            message.text,
            actor_id=message.sender_staff_id,
            provider_message_id=message.msg_id,
        )
        if result.status == "awaiting_interaction" and interaction_timeout_scheduler is not None:
            interaction_timeout_scheduler.schedule(message, session, result.pending_interaction)
        reply_text = result.reply_text
    else:
        if llm_client is None:
            raise ValueError("llm_client is required when agent_loop is not provided")
        reply_text = await llm_client.complete(
            ASSISTANT_SYSTEM_PROMPT,
            [{"role": "user", "content": message.text}],
        )
    await outbound.reply(message, reply_text)


def _reply_target_from_pending(
    session: SessionRecord,
    pending: PendingInteractionRecord,
) -> object:
    from src.adapters.dingtalk import DingTalkReplyTarget

    return DingTalkReplyTarget(
        sender_staff_id=_required_string(pending.responder_id, "pending.responder_id"),
        conversation_type=_conversation_type_from_kind(session.kind),
        conversation_id=_required_string(session.conversation_id, "session.conversation_id"),
        open_conversation_id=_open_conversation_id_from_session(session),
        msg_id=f"pending:{_required_string(pending.correlation_id, 'pending.correlation_id')}",
    )


def _session_from_record(session: SessionRecord, *, responder_id: str) -> Session:
    from src.core import Actor, BotIdentity, Principal, Session

    kind = session.kind
    return Session(
        session_id=_required_string(session.session_id, "session.session_id"),
        conversation_id=_required_string(session.conversation_id, "session.conversation_id"),
        kind=kind,
        bot=BotIdentity(id=_required_string(session.bot_id, "session.bot_id")),
        principal=Principal(
            kind="group" if kind == "group" else "user",
            id=_required_string(session.principal_id, "session.principal_id"),
        ),
        actor=Actor(
            id=_required_string(responder_id, "pending.responder_id"),
            display_name=_display_name_from_context(session.context, responder_id),
        ),
        context=session.context,
        state=session.state,
        lifecycle=session.lifecycle,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _conversation_type_from_kind(kind: str) -> int:
    if kind == "dm":
        return 1
    if kind == "group":
        return 2
    raise ValueError(f"Stored session kind is invalid: {kind}")


def _open_conversation_id_from_session(session: SessionRecord) -> str:
    value = session.context.get("open_conversation_id")
    if isinstance(value, str) and value.strip() != "":
        return value.strip()
    return _required_string(session.conversation_id, "session.conversation_id")


def _display_name_from_context(context: Mapping[str, object], fallback: str) -> str:
    value = context.get("last_actor_nick")
    if isinstance(value, str) and value.strip() != "":
        return value.strip()
    return _required_string(fallback, "pending.responder_id")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for the assistant entry point."""

    parser = argparse.ArgumentParser(description="Run the DingTalk AI assistant.")
    parser.add_argument(
        "--stream",
        action="store_true",
        help="connect DingTalk Stream and log normalized inbound chatbot messages",
    )
    return parser.parse_args(argv)


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def cli(argv: Sequence[str] | None = None) -> None:
    """Run the assistant command-line entry point."""

    args = parse_args(argv)
    asyncio.run(main(start_stream=args.stream))


if __name__ == "__main__":
    cli()
