"""Tests for the Anthropic-backed LLM client wrapper."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import pytest
from anthropic import AnthropicError

from src.infra.config import LLMConfig
from src.infra.llm import LLMClient, LLMError


def _config() -> LLMConfig:
    return LLMConfig(model="claude-test-model", anthropic_api_key="anthropic-key")


@pytest.mark.asyncio
async def test_complete_calls_anthropic_messages_api_and_returns_text() -> None:
    """Completion should send configured model/settings and extract text blocks."""

    fake_client = FakeAnthropicClient(
        FakeMessageResponse([TextBlock("text", " hello "), ToolBlock()])
    )

    llm_client = LLMClient(_config(), anthropic_client=fake_client, max_tokens=42)
    result = await llm_client.complete(
        "system prompt",
        [{"role": "user", "content": "question"}],
    )

    assert result == "hello"
    assert fake_client.messages.calls == [
        {
            "model": "claude-test-model",
            "max_tokens": 42,
            "system": "system prompt",
            "messages": [{"role": "user", "content": "question"}],
        }
    ]


@pytest.mark.asyncio
async def test_create_message_sends_tools_and_preserves_tool_use_blocks() -> None:
    """Tool-capable calls should pass tools and keep Claude tool_use content."""

    fake_client = FakeAnthropicClient(
        FakeMessageResponse(
            [
                ToolUseBlock(
                    type="tool_use",
                    id="toolu-1",
                    name="echo",
                    input={"text": "hello"},
                )
            ]
        )
    )
    llm_client = LLMClient(_config(), anthropic_client=fake_client, max_tokens=42)

    response = await llm_client.create_message(
        "system prompt",
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-old",
                        "name": "echo",
                        "input": {"text": "old"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-old",
                        "content": "echo:old",
                    }
                ],
            },
        ],
        tools=[
            {
                "name": "echo",
                "description": "Echo text",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            }
        ],
    )

    assert response.text == ""
    assert response.tool_uses == (
        {"type": "tool_use", "id": "toolu-1", "name": "echo", "input": {"text": "hello"}},
    )
    assert fake_client.messages.calls == [
        {
            "model": "claude-test-model",
            "max_tokens": 42,
            "system": "system prompt",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-old",
                            "name": "echo",
                            "input": {"text": "old"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-old",
                            "content": "echo:old",
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
                    },
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_create_message_normalizes_tool_schema_to_plain_json_containers() -> None:
    """Tool schemas may originate from immutable capability metadata but must serialize."""

    fake_client = FakeAnthropicClient(FakeMessageResponse([TextBlock("text", "ok")]))
    llm_client = LLMClient(_config(), anthropic_client=fake_client)

    await llm_client.create_message(
        "system prompt",
        [{"role": "user", "content": "question"}],
        tools=[
            {
                "name": "default_schema_tool",
                "description": "Uses the default capability schema",
                "input_schema": MappingProxyType(
                    {
                        "type": "object",
                        "properties": MappingProxyType({}),
                        "additionalProperties": True,
                    }
                ),
            }
        ],
    )

    tools = fake_client.messages.calls[0]["tools"]
    assert tools == [
        {
            "name": "default_schema_tool",
            "description": "Uses the default capability schema",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
        }
    ]
    json.dumps(tools)


@pytest.mark.asyncio
async def test_client_factory_receives_api_key_and_timeout_and_is_closed() -> None:
    """Owned SDK clients should be created from config and closed by the wrapper."""

    fake_client = FakeAnthropicClient({"content": [{"type": "text", "text": "ok"}]})
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeAnthropicClient:
        factory_calls.append(dict(kwargs))
        return fake_client

    async with LLMClient(_config(), client_factory=factory, timeout=12.5) as llm_client:
        assert await llm_client.complete("system", [{"role": "user", "content": "hi"}]) == "ok"

    assert factory_calls == [{"api_key": "anthropic-key", "timeout": 12.5}]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_complete_rejects_empty_messages() -> None:
    """The wrapper should fail fast before sending malformed prompts."""

    llm_client = LLMClient(
        _config(), anthropic_client=FakeAnthropicClient(_text_response("unused"))
    )

    with pytest.raises(ValueError, match="messages must contain"):
        await llm_client.complete("system prompt", [])


@pytest.mark.asyncio
async def test_complete_raises_when_response_has_no_text() -> None:
    """A response without text blocks should not be treated as a valid reply."""

    llm_client = LLMClient(_config(), anthropic_client=FakeAnthropicClient({"content": []}))

    with pytest.raises(LLMError, match="did not include text"):
        await llm_client.complete("system prompt", [{"role": "user", "content": "question"}])


@pytest.mark.asyncio
async def test_complete_wraps_anthropic_errors() -> None:
    """SDK failures should be surfaced as the local LLM error type."""

    fake_client = FakeAnthropicClient(AnthropicError("rate limited"))
    llm_client = LLMClient(_config(), anthropic_client=fake_client)

    with pytest.raises(LLMError, match="Claude completion failed"):
        await llm_client.complete("system prompt", [{"role": "user", "content": "question"}])

    assert fake_client.messages.calls == [
        {
            "model": "claude-test-model",
            "max_tokens": 1024,
            "system": "system prompt",
            "messages": [{"role": "user", "content": "question"}],
        }
    ]


def _text_response(text: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}]}


@dataclass(frozen=True, slots=True)
class TextBlock:
    type: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolBlock:
    type: str = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FakeMessageResponse:
    content: list[object]


class FakeMessagesResource:
    """Fake Anthropic messages resource that records create-call payloads."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


class FakeAnthropicClient:
    """Fake async Anthropic client used to avoid external network calls."""

    def __init__(self, response: Any) -> None:
        self.messages = FakeMessagesResource(response)
        self.closed = False

    async def close(self) -> None:
        self.closed = True
