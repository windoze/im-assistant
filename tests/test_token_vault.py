"""Tests for encrypted delegated-token storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from src.infra.store import SQLiteStore
from src.infra.token_vault import TokenVault


class RefreshRejected(RuntimeError):
    """Test exception representing an invalid provider refresh token."""


class TransientRefreshError(RuntimeError):
    """Test exception representing a non-revocable refresh failure."""


@dataclass(frozen=True, slots=True)
class RefreshPayload:
    """Provider refresh result shape consumed by TokenVault.get_valid."""

    access_token: str
    refresh_token: str
    expires_at: datetime


@pytest.mark.asyncio
async def test_token_vault_put_get_revoke_and_encrypts_plaintext(tmp_path) -> None:
    """Stored OBO tokens should be encrypted and round-trip through the vault API."""

    fernet_key = Fernet.generate_key().decode("utf-8")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    expires_at = datetime(2026, 1, 1, 12, 10, tzinfo=UTC)

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        vault = TokenVault(store, fernet_key=fernet_key, now_factory=lambda: now)

        stored = await vault.put(
            principal="principal-1",
            service="calendar",
            user_access_token="user-access-token",
            refresh_token="user-refresh-token",
            scopes=("calendar:read", "calendar:read", "calendar:write"),
            expires_at=expires_at,
        )

        assert stored.principal_id == "principal-1"
        assert stored.service == "calendar"
        assert stored.user_access_token == "user-access-token"
        assert stored.refresh_token == "user-refresh-token"
        assert stored.scopes == ("calendar:read", "calendar:write")
        assert stored.expires_at == expires_at
        assert stored.needs_refresh is False

        raw_record = await store.get_token("principal-1", "calendar")
        assert raw_record is not None
        assert raw_record.access_token_ciphertext != "user-access-token"
        assert "user-access-token" not in raw_record.access_token_ciphertext
        assert raw_record.refresh_token_ciphertext is not None
        assert "user-refresh-token" not in raw_record.refresh_token_ciphertext

        fetched = await vault.get("principal-1", "calendar")
        assert fetched == stored

        assert await vault.revoke("principal-1", "calendar") is True
        assert await vault.get("principal-1", "calendar") is None
        assert await vault.revoke("principal-1", "calendar") is False


@pytest.mark.asyncio
async def test_token_vault_marks_near_expiry_tokens_for_refresh(tmp_path) -> None:
    """Tokens expiring inside the refresh skew should be surfaced for refresh."""

    fernet_key = Fernet.generate_key().decode("utf-8")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        vault = TokenVault(store, fernet_key=fernet_key, now_factory=lambda: now)

        await vault.put(
            principal="principal-1",
            service="calendar",
            user_access_token="access",
            refresh_token="refresh",
            scopes=("calendar:read",),
            expires_at=datetime(2026, 1, 1, 12, 4, tzinfo=UTC),
        )

        stored = await vault.get("principal-1", "calendar")
        assert stored is not None
        assert stored.needs_refresh is True


@pytest.mark.asyncio
async def test_token_vault_get_valid_silently_refreshes_stale_token(tmp_path) -> None:
    """Stale grants should be refreshed, persisted, and returned as usable tokens."""

    fernet_key = Fernet.generate_key().decode("utf-8")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    calls: list[str] = []

    async def refresh(refresh_token: str) -> RefreshPayload:
        calls.append(refresh_token)
        return RefreshPayload(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        )

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        vault = TokenVault(store, fernet_key=fernet_key, now_factory=lambda: now)
        await vault.put(
            principal="principal-1",
            service="calendar",
            user_access_token="old-access",
            refresh_token="old-refresh",
            scopes=("calendar:read",),
            expires_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        )

        resolution = await vault.get_valid(
            "principal-1",
            "calendar",
            refresh=refresh,
            refresh_rejected_exceptions=(RefreshRejected,),
        )

        assert resolution.token is not None
        assert resolution.token.user_access_token == "new-access"
        assert resolution.token.refresh_token == "new-refresh"
        assert resolution.token.scopes == ("calendar:read",)
        assert resolution.token.needs_refresh is False
        assert resolution.refreshed is True
        assert resolution.needs_reauthorization is False
        assert calls == ["old-refresh"]

        stored = await vault.get("principal-1", "calendar")
        assert stored == resolution.token


@pytest.mark.asyncio
async def test_token_vault_get_valid_revokes_rejected_refresh_token(tmp_path) -> None:
    """Invalid refresh tokens should be cleared so the caller can request consent again."""

    fernet_key = Fernet.generate_key().decode("utf-8")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async def refresh(_: str) -> RefreshPayload:
        raise RefreshRejected("refresh token expired")

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        vault = TokenVault(store, fernet_key=fernet_key, now_factory=lambda: now)
        await vault.put(
            principal="principal-1",
            service="calendar",
            user_access_token="old-access",
            refresh_token="expired-refresh",
            scopes=("calendar:read",),
            expires_at=datetime(2026, 1, 1, 11, 59, tzinfo=UTC),
        )

        resolution = await vault.get_valid(
            "principal-1",
            "calendar",
            refresh=refresh,
            refresh_rejected_exceptions=(RefreshRejected,),
        )

        assert resolution.token is None
        assert resolution.needs_reauthorization is True
        assert resolution.reauthorization_reason == "refresh_rejected"
        assert await vault.get("principal-1", "calendar") is None


@pytest.mark.asyncio
async def test_token_vault_get_valid_preserves_token_on_unrelated_refresh_error(tmp_path) -> None:
    """Refresh failures that are not explicit rejection signals must not revoke grants."""

    fernet_key = Fernet.generate_key().decode("utf-8")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async def refresh(_: str) -> RefreshPayload:
        raise TransientRefreshError("oauth service unavailable")

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        vault = TokenVault(store, fernet_key=fernet_key, now_factory=lambda: now)
        await vault.put(
            principal="principal-1",
            service="calendar",
            user_access_token="old-access",
            refresh_token="refresh-token",
            scopes=("calendar:read",),
            expires_at=datetime(2026, 1, 1, 11, 59, tzinfo=UTC),
        )

        with pytest.raises(TransientRefreshError):
            await vault.get_valid(
                "principal-1",
                "calendar",
                refresh=refresh,
                refresh_rejected_exceptions=(RefreshRejected,),
            )

        stored = await vault.get("principal-1", "calendar")

    assert stored is not None
    assert stored.user_access_token == "old-access"
    assert stored.refresh_token == "refresh-token"


@pytest.mark.asyncio
async def test_token_vault_get_valid_revokes_stale_token_without_refresh_token(tmp_path) -> None:
    """A stale grant without refresh material cannot be recovered silently."""

    fernet_key = Fernet.generate_key().decode("utf-8")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async def refresh(_: str) -> RefreshPayload:
        raise AssertionError("refresh should not be called without refresh token")

    async with SQLiteStore(tmp_path / "assistant.db") as store:
        await store.initialize()
        vault = TokenVault(store, fernet_key=fernet_key, now_factory=lambda: now)
        await vault.put(
            principal="principal-1",
            service="calendar",
            user_access_token="old-access",
            refresh_token=None,
            scopes=("calendar:read",),
            expires_at=datetime(2026, 1, 1, 11, 59, tzinfo=UTC),
        )

        resolution = await vault.get_valid(
            "principal-1",
            "calendar",
            refresh=refresh,
            refresh_rejected_exceptions=(RefreshRejected,),
        )

        assert resolution.token is None
        assert resolution.needs_reauthorization is True
        assert resolution.reauthorization_reason == "missing_refresh_token"
        assert await vault.get("principal-1", "calendar") is None
