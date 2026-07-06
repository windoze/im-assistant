"""Tests for DingTalk outbound reply transport selection."""

from __future__ import annotations

import json

import httpx
import pytest

from src.adapters.dingtalk.message import InboundMessage
from src.adapters.dingtalk.outbound import reply
from src.infra.config import DingTalkConfig
from src.infra.dingtalk_client import DingTalkClient


def _config() -> DingTalkConfig:
    return DingTalkConfig(
        app_key="app-key",
        app_secret="app-secret",
        robot_code="robot-code",
        api_base="https://api.example.com",
        legacy_api_base="https://oapi.example.com",
    )


def _inbound(
    *,
    conversation_type: int = 2,
    open_conversation_id: str = "open-conversation-1",
    session_webhook_expired_time: int | None = 2_000,
) -> InboundMessage:
    return InboundMessage(
        text="hello",
        sender_staff_id="user-1",
        sender_nick="Alice",
        conversation_type=conversation_type,
        conversation_id="conversation-1",
        open_conversation_id=open_conversation_id,
        session_webhook="https://webhook.example.com/session",
        msg_id="msg-1",
        session_webhook_expired_time=session_webhook_expired_time,
    )


@pytest.mark.asyncio
async def test_reply_prefers_unexpired_session_webhook() -> None:
    webhook_requests: list[httpx.Request] = []

    def webhook_handler(request: httpx.Request) -> httpx.Response:
        webhook_requests.append(request)
        assert str(request.url) == "https://webhook.example.com/session"
        assert json.loads(request.content) == {
            "msgtype": "text",
            "text": {"content": "fixed reply"},
        }
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(webhook_handler)) as webhook_client,
        httpx.AsyncClient(transport=httpx.MockTransport(_unexpected_request)) as api_http_client,
    ):
        client = DingTalkClient(_config(), http_client=api_http_client)

        result = await reply(
            _inbound(),
            "fixed reply",
            client=client,
            http_client=webhook_client,
            clock=lambda: 1_000,
        )

    assert result.transport == "session_webhook"
    assert result.payload == {"errcode": 0, "errmsg": "ok"}
    assert len(webhook_requests) == 1


@pytest.mark.asyncio
async def test_reply_falls_back_to_openapi_oto_when_webhook_expired() -> None:
    paths: list[str] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.url.path == "/v1.0/robot/oToMessages/batchSend"
        body = json.loads(request.content)
        assert body["userIds"] == ["user-1"]
        assert json.loads(body["msgParam"]) == {"content": "dm reply"}
        return httpx.Response(200, json={"processQueryKey": "query-key"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(api_handler)) as api_http_client,
        httpx.AsyncClient(transport=httpx.MockTransport(_unexpected_request)) as webhook_client,
    ):
        client = DingTalkClient(_config(), http_client=api_http_client)

        result = await reply(
            _inbound(conversation_type=1, open_conversation_id="conversation-1"),
            "dm reply",
            client=client,
            http_client=webhook_client,
            clock=lambda: 2_001,
        )

    assert result.transport == "openapi_oto"
    assert result.payload == {"processQueryKey": "query-key"}
    assert paths == ["/v1.0/oauth2/accessToken", "/v1.0/robot/oToMessages/batchSend"]


@pytest.mark.asyncio
async def test_reply_falls_back_to_openapi_group_when_webhook_missing_expiry() -> None:
    paths: list[str] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.url.path == "/v1.0/robot/groupMessages/send"
        body = json.loads(request.content)
        assert body["openConversationId"] == "open-conversation-1"
        assert json.loads(body["msgParam"]) == {"content": "group reply"}
        return httpx.Response(200, json={"messageId": "message-1"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(api_handler)) as api_http_client,
        httpx.AsyncClient(transport=httpx.MockTransport(_unexpected_request)) as webhook_client,
    ):
        client = DingTalkClient(_config(), http_client=api_http_client)

        result = await reply(
            _inbound(session_webhook_expired_time=None),
            "group reply",
            client=client,
            http_client=webhook_client,
            clock=lambda: 1_000,
        )

    assert result.transport == "openapi_group"
    assert result.payload == {"messageId": "message-1"}
    assert paths == ["/v1.0/oauth2/accessToken", "/v1.0/robot/groupMessages/send"]


def _unexpected_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")
