"""Tests for capability authorization and credential context."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from src.capabilities import (
    Authorizer,
    CredentialContext,
    CredentialError,
    CredentialHandle,
    Denied,
    Granted,
    NeedsConsent,
    Requirement,
    build_oauth_start_url,
)
from src.core import Actor, BotIdentity, Principal, Session
from src.infra.config import OAuthConfig
from src.infra.oauth import PendingAuthStore
from src.infra.token_vault import TokenVaultResolution, UserToken


@pytest.mark.asyncio
async def test_authorizer_grants_valid_obo_token(caplog: pytest.LogCaptureFixture) -> None:
    """A valid TokenVault grant should become a user credential handle."""

    token = UserToken(
        principal_id="user:user-1",
        service="calendar",
        user_access_token="user-access-token",
        refresh_token="refresh-token",
        scopes=("calendar:read", "calendar:write"),
        expires_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    vault = FakeTokenVault(TokenVaultResolution(token=token))
    authorizer = Authorizer(
        token_vault=vault,
        pending_store=PendingAuthStore(),
        dingtalk_client=FakeDingTalkClient(),
        oauth_config=_oauth_config(),
    )

    with caplog.at_level(logging.INFO, logger="im_assistant.metrics"):
        result = await authorizer.resolve(
            Requirement(service="calendar", scopes=("calendar:read",), on_behalf_of="actor"),
            SimpleActor(id="user-1"),
            "dm",
            principal_id="user:user-1",
            session_id="session-1",
        )

    assert isinstance(result, Granted)
    assert result.handle.kind == "user"
    assert result.handle.service == "calendar"
    assert result.handle.user_access_token == "user-access-token"
    assert result.handle.scopes == ("calendar:read",)
    assert vault.calls == [("user:user-1", "calendar")]
    assert any(
        record.message == "runtime_metric"
        and record.metric_name == "obo_authorizations_total"
        and record.metric_labels["decision"] == "granted"
        and record.metric_labels["service"] == "calendar"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_authorizer_returns_needs_consent_and_creates_pending_auth() -> None:
    """Missing OBO grants should create a pending OAuth nonce and consent URL."""

    pending_store = PendingAuthStore()
    authorizer = Authorizer(
        token_vault=FakeTokenVault(TokenVaultResolution(reauthorization_reason="missing")),
        pending_store=pending_store,
        dingtalk_client=FakeDingTalkClient(),
        oauth_config=_oauth_config(),
        actor_identity_resolver=lambda _actor: "union-1",
        nonce_factory=lambda: "nonce-1",
    )

    result = await authorizer.resolve(
        Requirement(service="calendar", scopes=("calendar:read",), on_behalf_of="actor"),
        SimpleActor(id="user-1"),
        "dm",
        principal_id="user:user-1",
        session_id="dingtalk:dm:conversation-1",
    )

    assert isinstance(result, NeedsConsent)
    assert result.reason == "missing"
    assert result.url == "https://assistant.example.com/oauth/start?nonce=nonce-1"
    assert result.url == build_oauth_start_url(_oauth_config(), result.pending)
    assert result.pending.principal_id == "user:user-1"
    assert result.pending.actor_id == "union-1"
    assert result.pending.session_id == "dingtalk:dm:conversation-1"
    assert result.pending.service == "calendar"
    assert result.pending.scopes == ("calendar:read",)
    assert await pending_store.get("nonce-1") == result.pending


@pytest.mark.asyncio
async def test_authorizer_denies_obo_requirement_in_group_mode() -> None:
    """OBO requirements are denied outside DMs before TokenVault lookup."""

    vault = FakeTokenVault(TokenVaultResolution(reauthorization_reason="missing"))
    authorizer = Authorizer(
        token_vault=vault,
        pending_store=PendingAuthStore(),
        dingtalk_client=FakeDingTalkClient(),
        oauth_config=_oauth_config(),
    )

    result = await authorizer.resolve(
        Requirement(service="calendar", scopes=("calendar:read",), on_behalf_of="actor"),
        SimpleActor(id="user-1"),
        "group",
        principal_id="group:open-conversation-1",
        session_id="session-1",
    )

    assert isinstance(result, Denied)
    assert result.reason == "OBO requirements can only be granted in DM sessions"
    assert vault.calls == []


def test_credential_context_exposes_user_and_group_facades() -> None:
    """Capability handlers should get `ctx.user.*` and `ctx.group.*` helpers."""

    session = Session(
        session_id="dingtalk:group:conversation-1",
        conversation_id="conversation-1",
        kind="group",
        bot=BotIdentity(id="robot-code"),
        principal=Principal(kind="group", id="group:open-conversation-1"),
        actor=Actor(id="user-1", display_name="Alice"),
        context={"open_conversation_id": "open-conversation-1", "actor_union_id": "union-1"},
    )
    credentials = CredentialContext.for_session(
        session,
        handles=[
            CredentialHandle.user_token(
                service="calendar",
                user_access_token="user-access-token",
                scopes=("calendar:read",),
                principal_id="user:user-1",
                actor_id="user-1",
            )
        ],
    )

    assert credentials.user.id == "user-1"
    assert credentials.user.staff_id == "user-1"
    assert credentials.user.union_id == "union-1"
    assert credentials.user.token_for("calendar") == "user-access-token"
    assert credentials.require_user_token("calendar") == "user-access-token"
    assert credentials.group is not None
    assert credentials.group.id == "group:open-conversation-1"
    assert credentials.group.open_conversation_id == "open-conversation-1"
    with pytest.raises(CredentialError, match="No credential"):
        credentials.require_user_token("drive")


def _oauth_config() -> OAuthConfig:
    return OAuthConfig(redirect_uri="https://assistant.example.com/oauth/callback")


@dataclass(frozen=True, slots=True)
class SimpleActor:
    """Minimal actor test double."""

    id: str


class FakeDingTalkClient:
    """DingTalk client double for Authorizer tests."""

    async def refresh_user_access_token(self, refresh_token: str) -> object:
        raise AssertionError(f"refresh should not be called in this test: {refresh_token}")


class FakeTokenVault:
    """TokenVault test double returning a scripted resolution."""

    def __init__(self, resolution: TokenVaultResolution) -> None:
        self._resolution = resolution
        self.calls: list[tuple[str, str]] = []

    async def get_valid(
        self, principal: str, service: str, **_kwargs: object
    ) -> TokenVaultResolution:
        self.calls.append((principal, service))
        return self._resolution
