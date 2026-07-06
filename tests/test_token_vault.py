"""Tests for encrypted delegated-token storage."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from src.infra.store import SQLiteStore
from src.infra.token_vault import TokenVault


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
