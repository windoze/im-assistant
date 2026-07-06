"""Encrypted storage for user-level DingTalk OBO tokens."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from cryptography.fernet import Fernet, InvalidToken

from src.infra.config import TokenVaultConfig
from src.infra.log import get_logger
from src.infra.store import SQLiteStore, TokenVaultRecord

DEFAULT_REFRESH_SKEW_SECONDS = 300
logger = get_logger(__name__)
TokenReauthorizationReason = Literal["missing", "missing_refresh_token", "refresh_rejected"]


class TokenVaultError(RuntimeError):
    """Raised when delegated token material cannot be encrypted or decrypted."""


@dataclass(frozen=True, slots=True)
class UserToken:
    """Decrypted OBO token material for one principal and external service."""

    principal_id: str
    service: str
    user_access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None
    needs_refresh: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RefreshedUserToken(Protocol):
    """Shape returned by a provider-specific refresh-token exchange."""

    access_token: str
    refresh_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TokenVaultResolution:
    """Result of resolving stored delegated token material for immediate use."""

    token: UserToken | None = None
    refreshed: bool = False
    reauthorization_reason: TokenReauthorizationReason | None = None

    @property
    def needs_reauthorization(self) -> bool:
        """Return whether the caller must start a fresh consent flow."""

        return self.reauthorization_reason is not None


class TokenVault:
    """Encrypt, persist, retrieve, and revoke user-level delegated tokens."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        fernet_key: str,
        refresh_skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
        now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._fernet = _fernet_from_key(fernet_key)
        self._refresh_skew = timedelta(
            seconds=_positive_int(refresh_skew_seconds, "refresh_skew_seconds")
        )
        self._now_factory = now_factory

    @classmethod
    def from_config(cls, store: SQLiteStore, config: TokenVaultConfig) -> TokenVault:
        """Create a vault using the Fernet key loaded from `.env`."""

        return cls(store, fernet_key=config.fernet_key)

    async def get(self, principal: str, service: str) -> UserToken | None:
        """Return decrypted token material, or `None` when no grant is stored."""

        record = await self._store.get_token(principal, service)
        if record is None:
            return None
        return self._decrypt_record(record)

    async def get_valid(
        self,
        principal: str,
        service: str,
        *,
        refresh: Callable[[str], Awaitable[RefreshedUserToken]],
        refresh_rejected_exceptions: Sequence[type[Exception]],
    ) -> TokenVaultResolution:
        """Return a usable token, silently refreshing stale grants when possible."""

        if not callable(refresh):
            raise ValueError("refresh must be callable")
        rejected_exception_types = _exception_type_tuple(refresh_rejected_exceptions)
        token = await self.get(principal, service)
        if token is None:
            return TokenVaultResolution(reauthorization_reason="missing")
        if not token.needs_refresh:
            return TokenVaultResolution(token=token)
        if token.refresh_token is None:
            await self.revoke(token.principal_id, token.service)
            logger.warning(
                "token_vault_refresh_token_missing",
                extra={"principal_id": token.principal_id, "service": token.service},
            )
            return TokenVaultResolution(reauthorization_reason="missing_refresh_token")

        try:
            refreshed = await refresh(token.refresh_token)
        except rejected_exception_types:
            await self.revoke(token.principal_id, token.service)
            logger.warning(
                "token_vault_refresh_rejected",
                extra={"principal_id": token.principal_id, "service": token.service},
            )
            return TokenVaultResolution(reauthorization_reason="refresh_rejected")

        stored = await self.put(
            principal=token.principal_id,
            service=token.service,
            user_access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
            scopes=token.scopes,
            expires_at=refreshed.expires_at,
        )
        return TokenVaultResolution(token=stored, refreshed=True)

    async def put(
        self,
        *,
        principal: str,
        service: str,
        user_access_token: str,
        refresh_token: str | None,
        scopes: Sequence[str],
        expires_at: datetime | None,
    ) -> UserToken:
        """Encrypt and upsert token material for one principal/service pair."""

        record = await self._store.upsert_token(
            TokenVaultRecord(
                principal_id=_non_empty_string(principal, "principal"),
                service=_non_empty_string(service, "service"),
                access_token_ciphertext=self._encrypt(user_access_token, "user_access_token"),
                refresh_token_ciphertext=(
                    None if refresh_token is None else self._encrypt(refresh_token, "refresh_token")
                ),
                scopes=_normalize_scopes(scopes),
                expires_at=None if expires_at is None else _to_utc(expires_at),
            )
        )
        return self._decrypt_record(record)

    async def revoke(self, principal: str, service: str) -> bool:
        """Delete stored token material for one principal/service pair."""

        return await self._store.delete_token(principal, service)

    def _encrypt(self, value: str, field_name: str) -> str:
        plaintext = _non_empty_string(value, field_name).encode("utf-8")
        return self._fernet.encrypt(plaintext).decode("utf-8")

    def _decrypt_record(self, record: TokenVaultRecord) -> UserToken:
        return UserToken(
            principal_id=record.principal_id,
            service=record.service,
            user_access_token=self._decrypt(record.access_token_ciphertext, "user_access_token"),
            refresh_token=(
                None
                if record.refresh_token_ciphertext is None
                else self._decrypt(record.refresh_token_ciphertext, "refresh_token")
            ),
            scopes=record.scopes,
            expires_at=record.expires_at,
            needs_refresh=self._needs_refresh(record.expires_at),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def _decrypt(self, ciphertext: str, field_name: str) -> str:
        try:
            return self._fernet.decrypt(
                _non_empty_string(ciphertext, f"{field_name}_ciphertext").encode("utf-8")
            ).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise TokenVaultError(f"Unable to decrypt stored {field_name}") from exc

    def _needs_refresh(self, expires_at: datetime | None) -> bool:
        if expires_at is None:
            return False
        return _to_utc(expires_at) <= _to_utc(self._now_factory()) + self._refresh_skew


def _fernet_from_key(key: str) -> Fernet:
    try:
        return Fernet(_non_empty_string(key, "fernet_key").encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise TokenVaultError("TOKEN_VAULT_FERNET_KEY must be a valid Fernet key") from exc


def _normalize_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_non_empty_string(scope, "scope") for scope in scopes))


def _non_empty_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"`{field_name}` must be a non-empty string")
    return value.strip()


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"`{field_name}` must be an integer")
    if value <= 0:
        raise ValueError(f"`{field_name}` must be greater than 0")
    return value


def _exception_type_tuple(values: Sequence[type[Exception]]) -> tuple[type[Exception], ...]:
    if isinstance(values, type) or not isinstance(values, Sequence):
        raise ValueError("refresh_rejected_exceptions must be a sequence of exception types")
    normalized = tuple(values)
    for value in normalized:
        if not isinstance(value, type) or not issubclass(value, Exception):
            raise ValueError("refresh_rejected_exceptions must contain only exception types")
    return normalized


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
