"""Tests for the deterministic slash-command registry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

import pytest

from src.capabilities import Capability, CapabilityRegistry
from src.core import (
    COMMAND_ARGS_INVALID_REPLY,
    COMMAND_FORBIDDEN_REPLY,
    COMMAND_UNAVAILABLE_REPLY,
    Actor,
    AgentLoop,
    BotIdentity,
    Command,
    CommandArgsSpec,
    CommandContext,
    CommandRegistry,
    Principal,
    Session,
)
from src.infra.store import SessionRecord, SQLiteStore


def test_registry_lists_commands_independently_from_capabilities() -> None:
    """Slash commands and Claude tools should stay in separate registries."""

    async def tool_handler() -> str:
        return "tool"

    command = Command("/ping", _reply("pong"), description="Ping command")
    command_registry = CommandRegistry(commands=[command])
    capability_registry = CapabilityRegistry(
        [
            Capability(
                name="ping",
                origin="system",
                available_in=["global"],
                handler=tool_handler,
            )
        ]
    )

    assert command_registry.list_commands() == (command,)
    assert command_registry.get("/ping") == command
    assert capability_registry.names() == ["ping"]
    assert capability_registry.get("ping") is not None


@pytest.mark.asyncio
async def test_registry_executes_authorized_user_command_with_parsed_args() -> None:
    """A registered command should receive parsed arguments and return a direct reply."""

    calls: list[tuple[tuple[str, ...], str]] = []

    async def handler(context: CommandContext) -> str:
        calls.append((context.args, context.args_text))
        return ",".join(context.args)

    registry = CommandRegistry(
        commands=[
            Command(
                "/say",
                handler,
                args_spec=CommandArgsSpec(min_args=2, max_args=2),
            )
        ]
    )

    reply = await registry.handle_command(_session(), '/say "hello world" Alice', object())

    assert reply == "hello world,Alice"
    assert calls == [(("hello world", "Alice"), '"hello world" Alice')]


@pytest.mark.asyncio
async def test_registry_rejects_invalid_arguments_before_handler() -> None:
    """Argument spec failures should be deterministic command replies."""

    called = False

    async def handler(context: CommandContext) -> str:
        nonlocal called
        called = True
        return "should not run"

    registry = CommandRegistry(
        commands=[Command("/connect", handler, args_spec=CommandArgsSpec(min_args=1, max_args=1))]
    )

    reply = await registry.handle_command(_session(), "/connect", object())

    assert reply == COMMAND_ARGS_INVALID_REPLY.format(reason="至少需要 1 个参数")
    assert called is False


@pytest.mark.asyncio
async def test_registry_denies_command_outside_available_mode() -> None:
    """Command mode restrictions should be checked before handler execution."""

    called = False

    async def handler(context: CommandContext) -> str:
        nonlocal called
        called = True
        return "should not run"

    registry = CommandRegistry(commands=[Command("/dm-only", handler, available_in=("dm",))])

    reply = await registry.handle_command(_session(kind="group"), "/dm-only", object())

    assert reply == COMMAND_UNAVAILABLE_REPLY.format(name="/dm-only")
    assert called is False


@pytest.mark.asyncio
async def test_registry_denies_over_privileged_command_for_regular_actor() -> None:
    """requires_role must use the current actor and reject insufficient roles."""

    called = False

    async def handler(context: CommandContext) -> str:
        nonlocal called
        called = True
        return "should not run"

    registry = CommandRegistry(
        commands=[Command("/enable", handler, requires_role="channel_admin")]
    )

    reply = await registry.handle_command(_session(kind="group"), "/enable docs", object())

    assert reply == COMMAND_FORBIDDEN_REPLY.format(name="/enable", role="channel_admin")
    assert called is False


@pytest.mark.asyncio
async def test_registry_allows_channel_admin_actor_from_session_context() -> None:
    """Session actor context should grant channel-admin commands deterministically."""

    registry = CommandRegistry(
        commands=[
            Command(
                "/enable",
                _reply("enabled"),
                requires_role="channel_admin",
            )
        ]
    )

    reply = await registry.handle_command(
        _session(kind="group", context={"channel_admin_ids": ["user-1"]}),
        "/enable docs",
        object(),
    )

    assert reply == "enabled"


@pytest.mark.asyncio
async def test_registry_allows_org_admin_to_satisfy_channel_admin() -> None:
    """Role checks should treat org_admin as higher privilege than channel_admin."""

    registry = CommandRegistry(
        commands=[Command("/enable", _reply("enabled"), requires_role="channel_admin")]
    )

    reply = await registry.handle_command(
        _session(kind="group", context={"org_admin_ids": ["user-1"]}),
        "/enable docs",
        object(),
    )

    assert reply == "enabled"


@pytest.mark.asyncio
async def test_inject_message_is_visible_to_next_agent_turn(tmp_path) -> None:
    """Injected command context should be loaded as history by the next agent turn."""

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        session = await _stored_session(store)
        registry = CommandRegistry(
            store,
            commands=[
                Command(
                    "/mode",
                    _injecting_reply("用户已切换到简洁模式。"),
                    args_spec=CommandArgsSpec(min_args=1, max_args=1),
                )
            ],
        )

        command_reply = await registry.handle_command(session, "/mode concise", object())
        llm_client = FakeCompleter("ok")
        agent_loop = AgentLoop(store, llm_client, system_prompt="system prompt")
        await agent_loop.run(
            session, "现在怎么回复？", actor_id="user-1", provider_message_id="msg-2"
        )
        stored_messages = await store.list_messages(session.session_id)

    assert command_reply == "模式已更新"
    assert llm_client.calls == [
        (
            "system prompt",
            [
                {"role": "user", "content": "用户已切换到简洁模式。"},
                {"role": "user", "content": "现在怎么回复？"},
            ],
        )
    ]
    assert stored_messages[0].metadata == {
        "source": "command_injection",
        "command": "/mode",
    }


class FakeCompleter:
    """Minimal LLM fake for agent-loop history assertions."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[str, list[Mapping[str, object]]]] = []

    async def complete(self, system: str, messages: Sequence[Mapping[str, object]]) -> str:
        self.calls.append((system, list(messages)))
        return self._reply


def _reply(text: str):
    async def handler(context: CommandContext) -> str:
        return text

    return handler


def _injecting_reply(injected_text: str):
    async def handler(context: CommandContext) -> str:
        await context.inject_message(injected_text)
        return "模式已更新"

    return handler


async def _stored_session(store: SQLiteStore) -> Session:
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


def _session(
    *,
    kind: Literal["dm", "group"] = "dm",
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
    )
