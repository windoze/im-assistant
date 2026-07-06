"""Integration tests for the M2 session runtime review surface."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from src.adapters.dingtalk import InboundEvent, InboundMessage
from src.core import AgentLoop, SessionInboxDispatcher, SessionManager
from src.infra.store import SessionRecord, SQLiteStore
from src.main import handle_inbound_event


@pytest.mark.asyncio
async def test_m2_runtime_keeps_concurrent_session_history_isolated(tmp_path) -> None:
    """Concurrent sessions should run in parallel without sharing persisted history."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        session_a = await _seed_dm_session(
            store,
            conversation_id="conversation-a",
            sender_staff_id="user-a",
            history_text="history A",
        )
        session_b = await _seed_dm_session(
            store,
            conversation_id="conversation-b",
            sender_staff_id="user-b",
            history_text="history B",
        )
        outbound = RecordingOutbound()
        completer = ConcurrentCompleter({"current A", "current B"})
        session_manager = SessionManager(store, bot_id="robot-code")
        agent_loop = AgentLoop(store, completer, system_prompt="system prompt", history_limit=4)

        async def process_event(event: InboundEvent) -> None:
            await handle_inbound_event(
                event,
                outbound=outbound,
                session_manager=session_manager,
                agent_loop=agent_loop,
            )

        dispatcher = SessionInboxDispatcher(process_event)
        try:
            await dispatcher.enqueue(
                _event(
                    conversation_id="conversation-a",
                    sender_staff_id="user-a",
                    sender_nick="Alice",
                    text="current A",
                    msg_id="msg-a-current",
                )
            )
            await dispatcher.enqueue(
                _event(
                    conversation_id="conversation-b",
                    sender_staff_id="user-b",
                    sender_nick="Bob",
                    text="current B",
                    msg_id="msg-b-current",
                )
            )

            await asyncio.wait_for(completer.wait_for_all_started(), timeout=1)
            running_a = await store.get_session(session_a)
            running_b = await store.get_session(session_b)
            assert running_a is not None
            assert running_b is not None
            assert running_a.state == "RunningAgent"
            assert running_b.state == "RunningAgent"
        finally:
            completer.release()
            await dispatcher.close()

        assert completer.calls_by_current == {
            "current A": [
                {"role": "user", "content": "history A"},
                {"role": "assistant", "content": "ack history A"},
                {"role": "user", "content": "current A"},
            ],
            "current B": [
                {"role": "user", "content": "history B"},
                {"role": "assistant", "content": "ack history B"},
                {"role": "user", "content": "current B"},
            ],
        }
        assert outbound.replies == [
            ("conversation-a", "reply to current A"),
            ("conversation-b", "reply to current B"),
        ]
        assert [
            (message.role, message.content) for message in await store.list_messages(session_a)
        ] == [
            ("user", "history A"),
            ("assistant", "ack history A"),
            ("user", "current A"),
            ("assistant", "reply to current A"),
        ]
        assert [
            (message.role, message.content) for message in await store.list_messages(session_b)
        ] == [
            ("user", "history B"),
            ("assistant", "ack history B"),
            ("user", "current B"),
            ("assistant", "reply to current B"),
        ]


async def _seed_dm_session(
    store: SQLiteStore,
    *,
    conversation_id: str,
    sender_staff_id: str,
    history_text: str,
) -> str:
    session_id = f"dingtalk:dm:{conversation_id}"
    await store.upsert_session(
        SessionRecord(
            session_id=session_id,
            conversation_id=conversation_id,
            kind="dm",
            bot_id="robot-code",
            principal_id=f"user:{sender_staff_id}",
            actor_id=sender_staff_id,
            context={"platform": "dingtalk"},
        )
    )
    await store.add_message(
        session_id=session_id,
        role="user",
        content=history_text,
        actor_id=sender_staff_id,
    )
    await store.add_message(
        session_id=session_id,
        role="assistant",
        content=f"ack {history_text}",
        actor_id="robot-code",
    )
    return session_id


def _event(
    *,
    conversation_id: str,
    sender_staff_id: str,
    sender_nick: str,
    text: str,
    msg_id: str,
) -> InboundMessage:
    return InboundMessage(
        text=text,
        sender_staff_id=sender_staff_id,
        sender_nick=sender_nick,
        conversation_type=1,
        conversation_id=conversation_id,
        open_conversation_id=conversation_id,
        session_webhook="https://webhook.example.com/session",
        msg_id=msg_id,
    )


class ConcurrentCompleter:
    """Fake LLM client that proves different session turns can overlap."""

    def __init__(self, expected_currents: set[str]) -> None:
        self._started = {current: asyncio.Event() for current in expected_currents}
        self._release = asyncio.Event()
        self.calls_by_current: dict[str, list[dict[str, str]]] = {}

    async def complete(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        current = messages[-1]["content"]
        self.calls_by_current[current] = [dict(message) for message in messages]
        started = self._started.get(current)
        if started is not None:
            started.set()
        await self._release.wait()
        return f"reply to {current}"

    async def wait_for_all_started(self) -> None:
        await asyncio.gather(*(started.wait() for started in self._started.values()))

    def release(self) -> None:
        self._release.set()


class RecordingOutbound:
    """Fake DingTalk outbound adapter that records conversation-scoped replies."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []

    async def reply(self, inbound: InboundEvent, text: str) -> object:
        self.replies.append((inbound.conversation_id, text))
        return None
