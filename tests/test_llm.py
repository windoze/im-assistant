"""Tests for the Anthropic-backed LLM client wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

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
class FakeMessageResponse:
    content: list[object]


class FakeMessagesResource:
    """Fake Anthropic messages resource that records create-call payloads."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return self._response


class FakeAnthropicClient:
    """Fake async Anthropic client used to avoid external network calls."""

    def __init__(self, response: Any) -> None:
        self.messages = FakeMessagesResource(response)
        self.closed = False

    async def close(self) -> None:
        self.closed = True
