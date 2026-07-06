"""Tests for DingTalk OAuth pending state and callback handling."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from src.infra.config import (
    AppConfig,
    CapabilitiesConfig,
    DingTalkConfig,
    LLMConfig,
    LoggingConfig,
    OAuthConfig,
    SessionConfig,
    StorageConfig,
    TokenVaultConfig,
)
from src.infra.dingtalk_client import DingTalkAPIError
from src.infra.oauth import (
    OAUTH_SUCCESS_MESSAGE,
    USER_ACCESS_TOKEN_PATH,
    DingTalkOAuthClient,
    OAuthCallbackResult,
    PendingAuthExpired,
    PendingAuthNotFound,
    PendingAuthStore,
    build_authorization_url,
    create_oauth_app,
)


@pytest.mark.asyncio
async def test_pending_auth_store_consumes_nonce_once_and_expires() -> None:
    """Pending OAuth state should be short-lived and single-use."""

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def clock() -> datetime:
        return now

    store = PendingAuthStore(ttl_seconds=60, now_factory=clock)
    pending = await store.create(
        nonce="nonce-1",
        principal="principal-1",
        session="session-1",
        service="calendar",
        scopes=("calendar:read", "calendar:read"),
    )

    assert pending.expires_at == now + timedelta(seconds=60)
    assert pending.scopes == ("calendar:read",)
    assert await store.get("nonce-1") == pending
    assert await store.consume("nonce-1") == pending
    with pytest.raises(PendingAuthNotFound):
        await store.consume("nonce-1")

    expiring = await store.create(
        nonce="nonce-2",
        principal="principal-1",
        session="session-1",
        service="calendar",
        scopes=("calendar:read",),
    )
    now = expiring.expires_at
    assert await store.get("nonce-2") is None
    with pytest.raises(PendingAuthNotFound):
        await store.consume("nonce-2")

    await store.create(
        nonce="nonce-3",
        principal="principal-1",
        session="session-1",
        service="calendar",
        scopes=("calendar:read",),
        expires_at=now + timedelta(seconds=1),
    )
    now = now + timedelta(seconds=1)
    with pytest.raises(PendingAuthExpired):
        await store.consume("nonce-3")


@pytest.mark.asyncio
async def test_start_endpoint_redirects_to_dingtalk_authorization_url() -> None:
    """The start endpoint should redirect only when the nonce is pending."""

    store = PendingAuthStore()
    pending = await store.create(
        nonce="nonce-1",
        principal="principal-1",
        session="session-1",
        service="calendar",
        scopes=("calendar:read",),
    )
    expected_url = build_authorization_url(
        _dingtalk_config(),
        _oauth_config(),
        pending,
        authorization_url="https://login.example.com/oauth2/auth",
    )
    app = create_oauth_app(
        _app_config(),
        store,
        authorization_url="https://login.example.com/oauth2/auth",
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/oauth/start?nonce=nonce-1", allow_redirects=False)
        missing = await client.get("/oauth/start?nonce=missing", allow_redirects=False)

    assert response.status == 302
    parsed = urlparse(response.headers["Location"])
    expected = urlparse(expected_url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        expected.scheme,
        expected.netloc,
        expected.path,
    )
    query = parse_qs(parsed.query)
    assert query == {
        "client_id": ["app-key"],
        "response_type": ["code"],
        "scope": ["openid"],
        "state": ["nonce-1"],
        "redirect_uri": ["https://assistant.example.com/oauth/callback"],
        "prompt": ["consent"],
    }
    assert missing.status == 404


@pytest.mark.asyncio
async def test_callback_consumes_state_and_exchanges_code_for_user_token() -> None:
    """The callback should validate state and exchange the code with DingTalk."""

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    requests: list[httpx.Request] = []
    authorized: list[OAuthCallbackResult] = []
    store = PendingAuthStore()
    pending = await store.create(
        nonce="nonce-1",
        principal="principal-1",
        session="session-1",
        service="calendar",
        scopes=("calendar:read",),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == USER_ACCESS_TOKEN_PATH
        assert json.loads(request.content) == {
            "clientId": "app-key",
            "clientSecret": "app-secret",
            "code": "auth-code",
            "grantType": "authorization_code",
        }
        return httpx.Response(
            200,
            json={
                "accessToken": "user-access-token",
                "refreshToken": "user-refresh-token",
                "expireIn": 7200,
            },
        )

    async def on_authorized(result: OAuthCallbackResult) -> None:
        authorized.append(result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        oauth_client = DingTalkOAuthClient(
            _dingtalk_config(),
            http_client=http_client,
            now_factory=lambda: now,
        )
        app = create_oauth_app(
            _app_config(),
            store,
            oauth_client=oauth_client,
            on_authorized=on_authorized,
        )

        async with TestClient(TestServer(app)) as client:
            response = await client.get("/oauth/callback?code=auth-code&state=nonce-1")
            response_text = await response.text()
            replay = await client.get("/oauth/callback?code=auth-code&state=nonce-1")

    assert response.status == 200
    assert response_text == OAUTH_SUCCESS_MESSAGE
    assert replay.status == 404
    assert len(requests) == 1
    assert authorized[0].pending == pending
    assert authorized[0].token.access_token == "user-access-token"
    assert authorized[0].token.refresh_token == "user-refresh-token"
    assert authorized[0].token.expire_in == 7200
    assert authorized[0].token.expires_at == now + timedelta(seconds=7200)
    assert await store.get("nonce-1") is None


@pytest.mark.asyncio
async def test_callback_rejects_expired_state_without_exchanging_code() -> None:
    """Expired OAuth state must be rejected before any token request is made."""

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    request_count = 0

    def clock() -> datetime:
        return now

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(500)

    store = PendingAuthStore(now_factory=clock)
    await store.create(
        nonce="nonce-1",
        principal="principal-1",
        session="session-1",
        service="calendar",
        scopes=("calendar:read",),
        expires_at=now + timedelta(seconds=1),
    )
    now = now + timedelta(seconds=1)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        oauth_client = DingTalkOAuthClient(_dingtalk_config(), http_client=http_client)
        app = create_oauth_app(_app_config(), store, oauth_client=oauth_client)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/oauth/callback?code=auth-code&state=nonce-1")

    assert response.status == 410
    assert request_count == 0


@pytest.mark.asyncio
async def test_user_token_exchange_rejects_malformed_payload() -> None:
    """DingTalk user-token responses must include all delegated-token fields."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accessToken": "missing-refresh", "expireIn": 7200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        oauth_client = DingTalkOAuthClient(_dingtalk_config(), http_client=http_client)
        with pytest.raises(DingTalkAPIError, match="user token response"):
            await oauth_client.exchange_authorization_code("auth-code")


def _app_config() -> AppConfig:
    return AppConfig(
        dingtalk=_dingtalk_config(),
        llm=LLMConfig(model="claude-test", anthropic_api_key="anthropic-key"),
        session=SessionConfig(confirm_timeout_sec=1800),
        storage=StorageConfig(database_path=Path("assistant.db")),
        token_vault=TokenVaultConfig(fernet_key=Fernet.generate_key().decode("utf-8")),
        capabilities=CapabilitiesConfig(channel_enabled_capabilities={}),
        logging=LoggingConfig(level="INFO"),
        oauth=_oauth_config(),
    )


def _dingtalk_config() -> DingTalkConfig:
    return DingTalkConfig(
        app_key="app-key",
        app_secret="app-secret",
        robot_code="robot-code",
        api_base="https://api.example.com",
        legacy_api_base="https://oapi.example.com",
    )


def _oauth_config() -> OAuthConfig:
    return OAuthConfig(redirect_uri="https://assistant.example.com/oauth/callback")
