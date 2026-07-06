"""Tests for per-session inbox dispatch."""

from __future__ import annotations

import asyncio

import pytest

from src.adapters.dingtalk import InboundMessage
from src.core import SessionInboxDispatcher


@pytest.mark.asyncio
async def test_session_inbox_processes_same_session_serially() -> None:
    """Events for one Session should be handled one at a time in FIFO order."""

    running = 0
    max_running = 0
    events_seen: list[str] = []

    async def handle(event: InboundMessage) -> None:
        nonlocal max_running, running
        running += 1
        max_running = max(max_running, running)
        events_seen.append(f"start:{event.msg_id}")
        await asyncio.sleep(0)
        events_seen.append(f"end:{event.msg_id}")
        running -= 1

    dispatcher = SessionInboxDispatcher(handle)

    await dispatcher.enqueue(_event(msg_id="msg-1"))
    await dispatcher.enqueue(_event(msg_id="msg-2"))
    await dispatcher.enqueue(_event(msg_id="msg-3"))
    await dispatcher.drain()
    await dispatcher.close()

    assert max_running == 1
    assert events_seen == [
        "start:msg-1",
        "end:msg-1",
        "start:msg-2",
        "end:msg-2",
        "start:msg-3",
        "end:msg-3",
    ]


@pytest.mark.asyncio
async def test_session_inbox_processes_different_sessions_in_parallel() -> None:
    """Events for different Sessions should not block each other."""

    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_handlers = asyncio.Event()

    async def handle(event: InboundMessage) -> None:
        if event.conversation_id == "conversation-1":
            first_started.set()
        if event.conversation_id == "conversation-2":
            second_started.set()
        await release_handlers.wait()

    dispatcher = SessionInboxDispatcher(handle)

    await dispatcher.enqueue(_event(conversation_id="conversation-1", msg_id="msg-1"))
    await dispatcher.enqueue(_event(conversation_id="conversation-2", msg_id="msg-2"))

    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.wait_for(second_started.wait(), timeout=1)
    release_handlers.set()
    await dispatcher.drain()
    await dispatcher.close()


def _event(
    *,
    conversation_id: str = "conversation-1",
    msg_id: str,
) -> InboundMessage:
    return InboundMessage(
        text="hello",
        sender_staff_id="user-1",
        sender_nick="Alice",
        conversation_type=1,
        conversation_id=conversation_id,
        open_conversation_id=conversation_id,
        session_webhook="https://webhook.example.com/session",
        msg_id=msg_id,
    )
