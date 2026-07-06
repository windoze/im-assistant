"""Async Claude client wrapper used by the assistant runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from anthropic import AnthropicError, AsyncAnthropic

from src.infra.config import LLMConfig
from src.infra.log import get_logger

logger = get_logger(__name__)

DEFAULT_LLM_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_TOKENS = 1024
ALLOWED_MESSAGE_ROLES = frozenset({"user", "assistant"})


class LLMError(RuntimeError):
    """Raised when Claude completion fails or returns unusable content."""


class MessagesResource(Protocol):
    """Protocol for the Anthropic messages resource used by `LLMClient`."""

    async def create(self, **kwargs: Any) -> Any:
        """Create one Anthropic message response."""


class AnthropicClient(Protocol):
    """Protocol for the subset of `AsyncAnthropic` needed by this module."""

    messages: MessagesResource

    async def close(self) -> None:
        """Close the underlying Anthropic HTTP client."""


AnthropicClientFactory = Callable[..., AnthropicClient]


class LLMClient:
    """Complete assistant prompts through Anthropic's async Messages API."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        anthropic_client: AnthropicClient | None = None,
        client_factory: AnthropicClientFactory = AsyncAnthropic,
        timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._config = config
        self._client = anthropic_client or client_factory(
            api_key=config.anthropic_api_key,
            timeout=timeout,
        )
        self._owns_client = anthropic_client is None
        self._max_tokens = _positive_int(max_tokens, "max_tokens")

    async def __aenter__(self) -> LLMClient:
        """Return this client when used as an async context manager."""

        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close an internally created Anthropic client on context-manager exit."""

        await self.aclose()

    async def aclose(self) -> None:
        """Close the Anthropic client if this wrapper created it."""

        if self._owns_client:
            await self._client.close()

    async def complete(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        """Return Claude's text response for a system prompt and message list."""

        system_prompt = _non_empty_string(system, "system")
        normalized_messages = _normalize_messages(messages)
        try:
            response = await self._client.messages.create(
                model=self._config.model,
                max_tokens=self._max_tokens,
                system=system_prompt,
                messages=normalized_messages,
            )
        except AnthropicError as exc:
            logger.exception("llm_completion_failed", extra={"model": self._config.model})
            raise LLMError("Claude completion failed") from exc

        return _extract_text_content(response)


def _normalize_messages(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    if isinstance(messages, (str, bytes)) or len(messages) == 0:
        raise ValueError("messages must contain at least one chat message")

    normalized: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise ValueError("each message must be a mapping")
        role = _non_empty_string(message.get("role"), "message.role")
        if role not in ALLOWED_MESSAGE_ROLES:
            raise ValueError("message.role must be 'user' or 'assistant'")
        normalized.append(
            {
                "role": role,
                "content": _non_empty_string(message.get("content"), "message.content"),
            }
        )
    return normalized


def _extract_text_content(response: Any) -> str:
    content = _response_content(response)
    if isinstance(content, (str, bytes)) or not isinstance(content, Sequence):
        raise LLMError("Claude response content must be a sequence")

    parts: list[str] = []
    for block in content:
        block_type = _block_value(block, "type")
        text = _block_value(block, "text")
        if block_type == "text" and isinstance(text, str):
            parts.append(text)

    text = "".join(parts).strip()
    if text == "":
        logger.error("llm_completion_missing_text")
        raise LLMError("Claude response did not include text content")
    return text


def _response_content(response: Any) -> Any:
    if isinstance(response, Mapping):
        return response.get("content")
    return getattr(response, "content", None)


def _block_value(block: Any, key: str) -> Any:
    if isinstance(block, Mapping):
        return block.get(key)
    return getattr(block, key, None)


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value
