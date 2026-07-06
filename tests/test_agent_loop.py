"""Tests for the persistent multi-turn agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from src.capabilities import Capability, CapabilityRegistry, Requirement
from src.core import (
    Actor,
    AgentLoop,
    AgentLoopStateError,
    BotIdentity,
    CapabilityExecutionContext,
    Principal,
    Session,
)
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
async def test_agent_loop_executes_visible_capability_tool_and_continues(tmp_path) -> None:
    """Claude tool_use requests should execute a visible capability and continue."""

    async def echo(context: CapabilityExecutionContext, *, text: str) -> str:
        """Echo text back to Claude."""

        return f"{context.session.session_id}:{text}"

    registry = CapabilityRegistry(
        [
            Capability(
                name="echo",
                origin="system",
                available_in=["global"],
                handler=echo,
                description="Echo text",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
    )
    llm_client = ToolCallingCompleter(
        [
            [
                {
                    "type": "tool_use",
                    "id": "toolu-1",
                    "name": "echo",
                    "input": {"text": "hello"},
                }
            ],
            [{"type": "text", "text": "final reply"}],
        ]
    )

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        agent_loop = AgentLoop(
            store,
            llm_client,
            system_prompt="system prompt",
            capability_registry=registry,
        )

        result = await agent_loop.run(session, "please echo")
        stored_messages = await store.list_messages(session.session_id)

    assert result.reply_text == "final reply"
    assert llm_client.calls == [
        {
            "system": "system prompt",
            "messages": [{"role": "user", "content": "please echo"}],
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo text",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ],
        },
        {
            "system": "system prompt",
            "messages": [
                {"role": "user", "content": "please echo"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "echo",
                            "input": {"text": "hello"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-1",
                            "content": "dingtalk:dm:conversation-1:hello",
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo text",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ],
        },
    ]
    assert [(message.role, message.content) for message in stored_messages] == [
        ("user", "please echo"),
        ("assistant", "final reply"),
    ]


@pytest.mark.asyncio
async def test_agent_loop_injects_capability_services(tmp_path) -> None:
    """Capability handlers should receive runtime services configured on the agent loop."""

    def use_service(context: CapabilityExecutionContext) -> str:
        return f"service={context.require_service('example')}"

    registry = CapabilityRegistry(
        [
            Capability(
                name="use_service",
                origin="system",
                available_in=["global"],
                handler=use_service,
            )
        ]
    )
    llm_client = ToolCallingCompleter(
        [
            [{"type": "tool_use", "id": "toolu-service", "name": "use_service", "input": {}}],
            [{"type": "text", "text": "service reply"}],
        ]
    )

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        agent_loop = AgentLoop(
            store,
            llm_client,
            system_prompt="system prompt",
            capability_registry=registry,
            capability_services={"example": "configured"},
        )

        result = await agent_loop.run(session, "use a service")

    tool_result = llm_client.calls[1]["messages"][-1]["content"][0]
    assert result.reply_text == "service reply"
    assert tool_result["content"] == "service=configured"


@pytest.mark.asyncio
async def test_agent_loop_returns_tool_execution_errors_to_claude(tmp_path) -> None:
    """Handler failures should become tool_result errors rather than crashing the turn."""

    async def explode(_context: CapabilityExecutionContext) -> str:
        raise RuntimeError("boom")

    registry = CapabilityRegistry(
        [
            Capability(
                name="explode",
                origin="system",
                available_in=["global"],
                handler=explode,
            )
        ]
    )
    llm_client = ToolCallingCompleter(
        [
            [{"type": "tool_use", "id": "toolu-err", "name": "explode", "input": {}}],
            [{"type": "text", "text": "handled failure"}],
        ]
    )

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_session(store)
        agent_loop = AgentLoop(
            store,
            llm_client,
            system_prompt="system prompt",
            capability_registry=registry,
        )

        result = await agent_loop.run(session, "please fail")

    tool_result = llm_client.calls[1]["messages"][-1]["content"][0]
    assert result.reply_text == "handled failure"
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu-err"
    assert tool_result["is_error"] is True
    assert "Tool explode failed: boom" in tool_result["content"]


@pytest.mark.asyncio
async def test_agent_loop_exposes_only_can_use_filtered_group_capabilities(tmp_path) -> None:
    """Group-mode tools should be filtered through channel-enabled can_use rules."""

    def noop(_context: CapabilityExecutionContext) -> str:
        return "ok"

    registry = CapabilityRegistry(
        [
            Capability(name="enabled_group", origin="system", available_in=["group"], handler=noop),
            Capability(
                name="disabled_group", origin="system", available_in=["group"], handler=noop
            ),
            Capability(
                name="obo_global",
                origin="system",
                available_in=["global"],
                requires=[Requirement(service="calendar", on_behalf_of="actor")],
                handler=noop,
            ),
        ]
    )
    llm_client = ToolCallingCompleter([[{"type": "text", "text": "group reply"}]])

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        session = await _stored_group_session(store)
        agent_loop = AgentLoop(
            store,
            llm_client,
            system_prompt="system prompt",
            capability_registry=registry,
            channel_enabled_capabilities={"group-open-conversation-id": ("enabled_group",)},
        )

        result = await agent_loop.run(session, "group request")

    assert result.reply_text == "group reply"
    assert [tool["name"] for tool in llm_client.calls[0]["tools"]] == ["enabled_group"]


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


async def _stored_group_session(store: SQLiteStore) -> Session:
    await store.initialize()
    record = await store.upsert_session(
        SessionRecord(
            session_id="dingtalk:group:conversation-1",
            conversation_id="conversation-1",
            kind="group",
            bot_id="robot-code",
            principal_id="group:group-open-conversation-id",
            actor_id="user-1",
            state="Idle",
            context={
                "platform": "dingtalk",
                "open_conversation_id": "group-open-conversation-id",
            },
        )
    )
    return Session(
        session_id=record.session_id,
        conversation_id=record.conversation_id,
        kind=record.kind,
        bot=BotIdentity(id=record.bot_id),
        principal=Principal(kind="group", id=record.principal_id),
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


class ToolCallingCompleter:
    """Fake LLM client that returns scripted Claude content blocks."""

    def __init__(self, responses: Sequence[Sequence[Mapping[str, Any]]]) -> None:
        self._responses = [[dict(block) for block in response] for response in responses]
        self.calls: list[dict[str, Any]] = []

    async def complete(self, system: str, messages: Sequence[Mapping[str, Any]]) -> str:
        raise AssertionError("tool tests should use create_message")

    async def create_message(
        self,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> FakeToolResponse:
        self.calls.append(
            {
                "system": system,
                "messages": _copy_messages(messages),
                "tools": [dict(tool) for tool in tools],
            }
        )
        if not self._responses:
            raise AssertionError("no scripted LLM response remaining")
        return FakeToolResponse(tuple(self._responses.pop(0)))


@dataclass(frozen=True, slots=True)
class FakeToolResponse:
    """Fake normalized response object returned by ToolCallingCompleter."""

    content: tuple[dict[str, Any], ...]

    @property
    def text(self) -> str:
        return "".join(
            block["text"]
            for block in self.content
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        ).strip()


def _copy_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            content = [dict(block) for block in content]
        copied.append({"role": message["role"], "content": content})
    return copied
