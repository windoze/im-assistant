"""Async Claude client wrapper used by the assistant runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class LLMMessageResponse:
    """Normalized Claude Messages API response content."""

    content: tuple[dict[str, Any], ...]

    @property
    def text(self) -> str:
        """Return concatenated text blocks from this response."""

        return _text_from_content_blocks(self.content)

    @property
    def tool_uses(self) -> tuple[dict[str, Any], ...]:
        """Return Claude tool-use blocks from this response."""

        return tuple(block for block in self.content if block.get("type") == "tool_use")


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

    async def complete(self, system: str, messages: Sequence[Mapping[str, Any]]) -> str:
        """Return Claude's text response for a system prompt and message list."""

        response = await self.create_message(system, messages)
        text = response.text
        if text == "":
            logger.error("llm_completion_missing_text")
            raise LLMError("Claude response did not include text content")
        return text

    async def create_message(
        self,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> LLMMessageResponse:
        """Create one Claude message and preserve text or tool-use content blocks."""

        system_prompt = _non_empty_string(system, "system")
        normalized_messages = _normalize_messages(messages)
        normalized_tools = _normalize_tools(tools)
        request: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._max_tokens,
            "system": system_prompt,
            "messages": normalized_messages,
        }
        if normalized_tools:
            request["tools"] = normalized_tools
        try:
            response = await self._client.messages.create(**request)
        except AnthropicError as exc:
            logger.exception("llm_completion_failed", extra={"model": self._config.model})
            raise LLMError("Claude completion failed") from exc

        return LLMMessageResponse(content=_normalize_response_content(response))


def _normalize_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if (
        isinstance(messages, (str, bytes))
        or not isinstance(messages, Sequence)
        or len(messages) == 0
    ):
        raise ValueError("messages must contain at least one chat message")

    normalized: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise ValueError("each message must be a mapping")
        role = _non_empty_string(message.get("role"), "message.role")
        if role not in ALLOWED_MESSAGE_ROLES:
            raise ValueError("message.role must be 'user' or 'assistant'")
        normalized.append(
            {
                "role": role,
                "content": _normalize_message_content(message.get("content")),
            }
        )
    return normalized


def _normalize_message_content(content: object) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return _non_empty_string(content, "message.content")
    if isinstance(content, bytes) or not isinstance(content, Sequence) or len(content) == 0:
        raise ValueError("message.content must be a non-empty string or content block list")
    return [_normalize_content_block(block) for block in content]


def _normalize_content_block(block: object) -> dict[str, Any]:
    if not isinstance(block, Mapping):
        raise ValueError("message.content blocks must be mappings")
    block_type = _non_empty_string(block.get("type"), "content.type")
    if block_type == "text":
        return {"type": "text", "text": _non_empty_string(block.get("text"), "content.text")}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": _non_empty_string(block.get("id"), "content.id"),
            "name": _non_empty_string(block.get("name"), "content.name"),
            "input": _tool_input_mapping(block.get("input", {})),
        }
    if block_type == "tool_result":
        normalized = {
            "type": "tool_result",
            "tool_use_id": _non_empty_string(
                block.get("tool_use_id"),
                "content.tool_use_id",
            ),
            "content": _tool_result_content(block.get("content", "")),
        }
        if "is_error" in block:
            is_error = block["is_error"]
            if not isinstance(is_error, bool):
                raise ValueError("content.is_error must be a boolean when provided")
            normalized["is_error"] = is_error
        return normalized
    raise ValueError("content.type must be 'text', 'tool_use', or 'tool_result'")


def _normalize_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(tools, (str, bytes, Mapping)) or not isinstance(tools, Sequence):
        raise ValueError("tools must be a sequence of tool mappings")

    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise ValueError("each tool must be a mapping")
        normalized.append(
            {
                "name": _non_empty_string(tool.get("name"), "tool.name"),
                "description": _non_empty_string(tool.get("description"), "tool.description"),
                "input_schema": _tool_schema_mapping(tool.get("input_schema")),
            }
        )
    return normalized


def _extract_text_content(response: Any) -> str:
    text = _text_from_content_blocks(_normalize_response_content(response))
    if text == "":
        logger.error("llm_completion_missing_text")
        raise LLMError("Claude response did not include text content")
    return text


def _normalize_response_content(response: Any) -> tuple[dict[str, Any], ...]:
    content = _response_content(response)
    if isinstance(content, (str, bytes)) or not isinstance(content, Sequence):
        raise LLMError("Claude response content must be a sequence")

    return tuple(_response_block_to_dict(block) for block in content)


def _text_from_content_blocks(content: Sequence[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for block in content:
        block_type = _block_value(block, "type")
        text = _block_value(block, "text")
        if block_type == "text" and isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()


def _response_block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, Mapping):
        return dict(block)
    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)

    normalized: dict[str, Any] = {}
    for key in ("type", "text", "id", "name", "input"):
        value = _block_value(block, key)
        if value is not None:
            normalized[key] = value
    if normalized:
        return normalized
    raise LLMError("Claude response content block is not readable")


def _tool_input_mapping(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("content.input must be a mapping")
    return dict(value)


def _tool_result_content(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("content.content must be a string")
    return value


def _tool_schema_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("tool.input_schema must be a mapping")
    schema = dict(value)
    if schema.get("type") != "object":
        raise ValueError("tool.input_schema.type must be 'object'")
    return schema


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
