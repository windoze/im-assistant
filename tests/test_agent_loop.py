"""Tests for the persistent multi-turn agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from src.core import Actor, AgentLoop, AgentLoopStateError, BotIdentity, Principal, Session
from src.infra.store import SessionRecord, SQLiteStore


@pytest.mark.asyncio
async def test_agent_loop_uses_history_and_persists_completed_turn(tmp_path) -> None:
    """A completed turn should include prior history and append user/assistant messages."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        first_user = await store.add_message(
            session_id=session.session_id,
            role="user",
            content="我叫 Alice",
            actor_id="user-1",
            provider_message_id="msg-1",
        )
        first_assistant = await store.add_message(
            session_id=session.session_id,
            role="assistant",
            content="好的，我记住了。",
            actor_id="robot-code",
        )
        llm_client = FakeCompleter("你叫 Alice。")
        agent_loop = AgentLoop(store, llm_client, system_prompt="system prompt")

        result = await agent_loop.run(
            session,
            "我叫什么？",
            actor_id="user-1",
            provider_message_id="msg-2",
        )

        stored_messages = await store.list_messages(session.session_id)
        stored_session = await store.get_session(session.session_id)

    assert result.reply_text == "你叫 Alice。"
    assert result.status == "completed"
    assert llm_client.calls == [
        (
            "system prompt",
            [
                {"role": "user", "content": "我叫 Alice"},
                {"role": "assistant", "content": "好的，我记住了。"},
                {"role": "user", "content": "我叫什么？"},
            ],
        )
    ]
    assert stored_messages[:2] == [first_user, first_assistant]
    assert [(message.role, message.content) for message in stored_messages[2:]] == [
        ("user", "我叫什么？"),
        ("assistant", "你叫 Alice。"),
    ]
    assert stored_messages[2].provider_message_id == "msg-2"
    assert stored_session is not None
    assert stored_session.state == "Idle"


@pytest.mark.asyncio
async def test_agent_loop_limits_loaded_history_to_recent_messages(tmp_path) -> None:
    """Only bounded recent history should be sent to the LLM."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        for role, content in (
            ("user", "old user"),
            ("assistant", "old assistant"),
            ("user", "recent user"),
            ("assistant", "recent assistant"),
        ):
            await store.add_message(session_id=session.session_id, role=role, content=content)
        llm_client = FakeCompleter("current reply")
        agent_loop = AgentLoop(
            store,
            llm_client,
            system_prompt="system prompt",
            history_limit=2,
        )

        await agent_loop.run(session, "current user")

    assert llm_client.calls == [
        (
            "system prompt",
            [
                {"role": "user", "content": "recent user"},
                {"role": "assistant", "content": "recent assistant"},
                {"role": "user", "content": "current user"},
            ],
        )
    ]


@pytest.mark.asyncio
async def test_agent_loop_sets_running_state_while_completion_is_pending(tmp_path) -> None:
    """The Session should persist RunningAgent during the LLM call and return to Idle."""

    completion_started = asyncio.Event()
    release_completion = asyncio.Event()

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        llm_client = BlockingCompleter(
            reply="done",
            started=completion_started,
            release=release_completion,
        )
        agent_loop = AgentLoop(store, llm_client, system_prompt="system prompt")

        run_task = asyncio.create_task(agent_loop.run(session, "hello"))
        await asyncio.wait_for(completion_started.wait(), timeout=1)
        running_session = await store.get_session(session.session_id)
        release_completion.set()
        result = await asyncio.wait_for(run_task, timeout=1)
        idle_session = await store.get_session(session.session_id)

    assert result.reply_text == "done"
    assert running_session is not None
    assert running_session.state == "RunningAgent"
    assert idle_session is not None
    assert idle_session.state == "Idle"


@pytest.mark.asyncio
async def test_agent_loop_rejects_awaiting_interaction_until_resume_is_implemented(
    tmp_path,
) -> None:
    """AwaitingInteraction is reserved for the later suspend/resume task."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store, state="AwaitingInteraction")
        agent_loop = AgentLoop(store, FakeCompleter("unused"), system_prompt="system prompt")

        with pytest.raises(AgentLoopStateError, match="AwaitingInteraction"):
            await agent_loop.run(session, "hello")

        stored_session = await store.get_session(session.session_id)

    assert stored_session is not None
    assert stored_session.state == "AwaitingInteraction"


async def _stored_session(store: SQLiteStore, *, state: str = "Idle") -> Session:
    await store.initialize()
    record = await store.upsert_session(
        SessionRecord(
            session_id="dingtalk:dm:conversation-1",
            conversation_id="conversation-1",
            kind="dm",
            bot_id="robot-code",
            principal_id="user:user-1",
            actor_id="user-1",
            state=state,
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


class FakeCompleter:
    """Fake LLM client that records completion inputs."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    async def complete(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        self.calls.append((system, [dict(message) for message in messages]))
        return self._reply


class BlockingCompleter(FakeCompleter):
    """Fake LLM client that pauses while the caller inspects Session state."""

    def __init__(self, *, reply: str, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__(reply)
        self._started = started
        self._release = release

    async def complete(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        self.calls.append((system, [dict(message) for message in messages]))
        self._started.set()
        await self._release.wait()
        return self._reply
