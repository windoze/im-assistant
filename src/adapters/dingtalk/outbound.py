"""Outbound DingTalk replies for normalized inbound chatbot messages."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from src.adapters.dingtalk.message import InboundMessage, UnsupportedInboundMessage
from src.infra.dingtalk_client import DingTalkClient, parse_dingtalk_response
from src.infra.log import get_logger

logger = get_logger(__name__)

ReplyTarget = InboundMessage | UnsupportedInboundMessage
ReplyTransport = Literal["session_webhook", "openapi_oto", "openapi_group"]
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ReplyResult:
    """Transport and DingTalk response payload for one outbound reply."""

    transport: ReplyTransport
    payload: Any


class DingTalkOutbound:
    """Send replies back to the DingTalk conversation that produced an inbound event."""

    def __init__(
        self,
        client: DingTalkClient,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._http_client = http_client or httpx.AsyncClient(
            timeout=DEFAULT_WEBHOOK_TIMEOUT_SECONDS
        )
        self._owns_http_client = http_client is None
        self._clock = clock

    async def __aenter__(self) -> DingTalkOutbound:
        """Return this outbound sender when used as an async context manager."""

        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close an internally owned HTTP client on context-manager exit."""

        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally owned HTTP client, if this sender created one."""

        if self._owns_http_client:
            await self._http_client.aclose()

    async def reply(self, inbound: ReplyTarget, text: str) -> ReplyResult:
        """Reply to a DingTalk inbound event using webhook first, then OpenAPI."""

        return await reply(
            inbound,
            text,
            client=self._client,
            http_client=self._http_client,
            clock=self._clock,
        )


async def reply(
    inbound: ReplyTarget,
    text: str,
    *,
    client: DingTalkClient,
    http_client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.time,
) -> ReplyResult:
    """Send one text reply to the inbound event's source conversation."""

    if _has_unexpired_session_webhook(inbound, clock=clock):
        payload = await _send_session_webhook(inbound, text, http_client=http_client)
        return ReplyResult(transport="session_webhook", payload=payload)

    if inbound.conversation_type == 1:
        payload = await client.send_oto([inbound.sender_staff_id], text)
        return ReplyResult(transport="openapi_oto", payload=payload)

    if inbound.conversation_type == 2:
        payload = await client.send_group(inbound.open_conversation_id, text)
        return ReplyResult(transport="openapi_group", payload=payload)

    raise ValueError("DingTalk conversation_type must be 1 or 2")


def _has_unexpired_session_webhook(
    inbound: ReplyTarget,
    *,
    clock: Callable[[], float],
) -> bool:
    expires_at = inbound.session_webhook_expired_time
    if inbound.session_webhook.strip() == "" or expires_at is None:
        return False
    return _timestamp_seconds(expires_at) > clock()


async def _send_session_webhook(
    inbound: ReplyTarget,
    text: str,
    *,
    http_client: httpx.AsyncClient | None,
) -> Any:
    request_body = {"msgtype": "text", "text": {"content": _non_empty_text(text)}}
    if http_client is None:
        async with httpx.AsyncClient(timeout=DEFAULT_WEBHOOK_TIMEOUT_SECONDS) as temporary_client:
            return await _post_session_webhook(temporary_client, inbound, request_body)
    return await _post_session_webhook(http_client, inbound, request_body)


async def _post_session_webhook(
    http_client: httpx.AsyncClient,
    inbound: ReplyTarget,
    request_body: dict[str, object],
) -> Any:
    try:
        response = await http_client.post(inbound.session_webhook, json=request_body)
    except httpx.HTTPError:
        logger.exception(
            "dingtalk_session_webhook_request_failed",
            extra={
                "msg_id": inbound.msg_id,
                "conversation_id": inbound.conversation_id,
            },
        )
        raise

    return parse_dingtalk_response(
        response,
        method="POST",
        path="sessionWebhook",
    )


def _timestamp_seconds(value: int) -> float:
    if value >= 10_000_000_000:
        return value / 1000
    return float(value)


def _non_empty_text(value: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError("reply text must be a non-empty string")
    return value
