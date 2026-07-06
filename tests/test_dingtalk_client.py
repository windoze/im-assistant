"""Tests for DingTalk OpenAPI token and request handling."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from src.infra.config import DingTalkConfig
from src.infra.dingtalk_client import AccessToken, DingTalkAPIError, DingTalkClient


def _config() -> DingTalkConfig:
    return DingTalkConfig(
        app_key="app-key",
        app_secret="app-secret",
        robot_code="robot-code",
        api_base="https://api.example.com",
        legacy_api_base="https://oapi.example.com",
    )


@pytest.mark.asyncio
async def test_get_access_token_fetches_and_caches() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "https://api.example.com/v1.0/oauth2/accessToken"
        assert json.loads(request.content) == {
            "appKey": "app-key",
            "appSecret": "app-secret",
        }
        return httpx.Response(200, json={"accessToken": "token-1", "expireIn": 7200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        first = await client.get_access_token()
        second = await client.get_access_token()

    assert first == AccessToken(access_token="token-1", expire_in=7200)
    assert second == first
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_get_access_token_refreshes_five_minutes_before_expiry() -> None:
    current_time = 0.0
    request_count = 0

    def clock() -> float:
        return current_time

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={"accessToken": f"token-{request_count}", "expireIn": 3600},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client, clock=clock)

        first = await client.get_access_token()
        current_time = 3299.0
        still_cached = await client.get_access_token()
        current_time = 3301.0
        refreshed = await client.get_access_token()

    assert first.access_token == "token-1"
    assert still_cached.access_token == "token-1"
    assert refreshed.access_token == "token-2"
    assert request_count == 2


@pytest.mark.asyncio
async def test_concurrent_get_access_token_uses_single_network_request() -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"accessToken": "shared-token", "expireIn": 7200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        tokens = await asyncio.gather(*(client.get_access_token() for _ in range(10)))

    assert {token.access_token for token in tokens} == {"shared-token"}
    assert request_count == 1


@pytest.mark.asyncio
async def test_api_post_uses_application_token_header() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.method == "POST"
        assert request.url.path == "/v1.0/example"
        assert request.headers["x-acs-dingtalk-access-token"] == "app-token"
        assert json.loads(request.content) == {"hello": "world"}
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        payload = await client.api_post("/v1.0/example", {"hello": "world"})

    assert payload == {"ok": True}
    assert paths == ["/v1.0/oauth2/accessToken", "/v1.0/example"]


@pytest.mark.asyncio
async def test_api_get_uses_supplied_user_token_without_fetching_app_token() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.url.path == "/v1.0/users"
        assert request.url.params["name"] == "alice"
        assert request.headers["x-acs-dingtalk-access-token"] == "user-token"
        return httpx.Response(200, json={"result": ["user-1"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        payload = await client.api_get(
            "v1.0/users",
            params={"name": "alice"},
            use_user_token="user-token",
        )

    assert payload == {"result": ["user-1"]}
    assert paths == ["/v1.0/users"]


@pytest.mark.asyncio
async def test_api_error_logs_errcode_and_errmsg(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 88, "errmsg": "bad request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        with caplog.at_level(logging.ERROR), pytest.raises(DingTalkAPIError) as exc_info:
            await client.api_post("/v1.0/fail", {}, use_user_token="user-token")

    assert exc_info.value.errcode == 88
    assert exc_info.value.errmsg == "bad request"
    assert any(record.errcode == 88 and record.errmsg == "bad request" for record in caplog.records)
