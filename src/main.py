"""Async application entry point for the DingTalk AI assistant."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from src.infra.log import configure_logging, get_logger

if TYPE_CHECKING:
    from src.adapters.dingtalk import InboundEvent, InboundMessage
    from src.core.agent_loop import AgentRunResult
    from src.core.session import Session
    from src.core.session_manager import SessionRouteResult
    from src.infra.config import AppConfig

logger = get_logger("im_assistant")
ASSISTANT_SYSTEM_PROMPT = "你是企业内 AI 助手。请简洁、准确地回答用户问题。"


class ReplySender(Protocol):
    """Protocol for objects that can reply to DingTalk inbound events."""

    async def reply(self, inbound: InboundEvent, text: str) -> object:
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

                    async def process_event(event: InboundEvent) -> None:
                        await handle_inbound_event(
                            event,
                            outbound=outbound,
                            session_manager=session_manager,
                            agent_loop=agent_loop,
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


async def handle_inbound_event(
    event: InboundEvent,
    *,
    outbound: ReplySender,
    llm_client: TextCompleter | None = None,
    session_manager: SessionRouter | None = None,
    agent_loop: AgentRunner | None = None,
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
    )


async def _on_inbound_message(
    message: InboundMessage,
    *,
    outbound: ReplySender,
    llm_client: TextCompleter | None,
    session: Session | None = None,
    agent_loop: AgentRunner | None = None,
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
        reply_text = result.reply_text
    else:
        if llm_client is None:
            raise ValueError("llm_client is required when agent_loop is not provided")
        reply_text = await llm_client.complete(
            ASSISTANT_SYSTEM_PROMPT,
            [{"role": "user", "content": message.text}],
        )
    await outbound.reply(message, reply_text)


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


def cli(argv: Sequence[str] | None = None) -> None:
    """Run the assistant command-line entry point."""

    args = parse_args(argv)
    asyncio.run(main(start_stream=args.stream))


if __name__ == "__main__":
    cli()
