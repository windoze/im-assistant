"""Tests for persistent Session routing."""

from __future__ import annotations

import pytest

from src.adapters.dingtalk import InboundMessage
from src.core import SessionManager
from src.infra.store import SQLiteStore


@pytest.mark.asyncio
async def test_session_manager_creates_and_reuses_dm_session(tmp_path) -> None:
    """The same DM conversation should map to one persistent Session."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        manager = SessionManager(store, bot_id="robot-code")

        first = await manager.get_or_create_for_event(
            _event(conversation_type=1, conversation_id="dm-conversation", sender_staff_id="user-1")
        )
        second = await manager.get_or_create_for_event(
            _event(
                conversation_type=1,
                conversation_id="dm-conversation",
                sender_staff_id="user-1",
                msg_id="msg-2",
            )
        )

        stored = await store.get_session_by_conversation_id("dm-conversation")

    assert first.created is True
    assert first.should_send_welcome is False
    assert first.session.kind == "dm"
    assert first.session.bot.id == "robot-code"
    assert first.session.principal.kind == "user"
    assert first.session.principal.id == "user:user-1"
    assert second.created is False
    assert second.session.session_id == first.session.session_id
    assert stored is not None
    assert stored.session_id == first.session.session_id


@pytest.mark.asyncio
async def test_session_manager_shares_group_session_and_updates_actor(tmp_path) -> None:
    """A group should keep one Session while actor follows each triggering sender."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        manager = SessionManager(store, bot_id="robot-code")

        first = await manager.get_or_create_for_event(
            _event(
                conversation_type=2,
                conversation_id="group-conversation",
                open_conversation_id="open-group-1",
                sender_staff_id="user-1",
                sender_nick="Alice",
                msg_id="msg-1",
            )
        )
        second = await manager.get_or_create_for_event(
            _event(
                conversation_type=2,
                conversation_id="group-conversation",
                open_conversation_id="open-group-1",
                sender_staff_id="user-2",
                sender_nick="Bob",
                msg_id="msg-2",
            )
        )

        stored = await store.get_session_by_conversation_id("group-conversation")

    assert first.created is True
    assert first.should_send_welcome is True
    assert first.session.kind == "group"
    assert first.session.principal.kind == "group"
    assert first.session.principal.id == "group:open-group-1"
    assert first.session.context["activated"] is True
    assert first.session.context["activated_by"] == "user-1"
    assert first.session.context["activation_msg_id"] == "msg-1"
    assert second.created is False
    assert second.should_send_welcome is False
    assert second.session.session_id == first.session.session_id
    assert second.session.actor.id == "user-2"
    assert second.session.actor.display_name == "Bob"
    assert stored is not None
    assert stored.actor_id == "user-2"
    assert stored.context["last_actor_nick"] == "Bob"


def _event(
    *,
    conversation_type: int,
    conversation_id: str,
    sender_staff_id: str = "user-1",
    sender_nick: str = "Alice",
    open_conversation_id: str | None = None,
    msg_id: str = "msg-1",
) -> InboundMessage:
    return InboundMessage(
        text="hello",
        sender_staff_id=sender_staff_id,
        sender_nick=sender_nick,
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        open_conversation_id=open_conversation_id or conversation_id,
        session_webhook="https://webhook.example.com/session",
        msg_id=msg_id,
    )
