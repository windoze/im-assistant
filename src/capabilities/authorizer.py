"""Authorization gate for capability requirements."""

from __future__ import annotations

import inspect
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urlparse, urlunparse

from src.capabilities.base import Requirement
from src.capabilities.credential import CredentialHandle
from src.capabilities.registry import CapabilityMode
from src.infra.audit import AuditLogger, OBOAuditDecision
from src.infra.config import OAuthConfig
from src.infra.dingtalk_client import DingTalkUserTokenRefreshRejected
from src.infra.oauth import PendingAuth, PendingAuthStore
from src.infra.token_vault import TokenVaultResolution, UserToken


class AuthorizerError(RuntimeError):
    """Raised when authorization cannot be resolved because wiring is invalid."""


class AuthorizerActorContext(Protocol):
    """Actor shape required by the Authorizer."""

    id: str


class AuthorizerTokenVault(Protocol):
    """TokenVault methods consumed by the Authorizer."""

    async def get_valid(
        self,
        principal: str,
        service: str,
        *,
        refresh: Callable[[str], Awaitable[object]],
        refresh_rejected_exceptions: Sequence[type[Exception]],
    ) -> TokenVaultResolution:
        """Return a valid user token or a reauthorization reason."""


class AuthorizerDingTalkClient(Protocol):
    """DingTalk client method used to refresh OBO user tokens."""

    async def refresh_user_access_token(self, refresh_token: str) -> object:
        """Refresh a DingTalk user token using a stored refresh token."""


ActorIdentityResolver = Callable[[AuthorizerActorContext], Awaitable[str] | str]
NonceFactory = Callable[[], str]


@dataclass(frozen=True, slots=True)
class Granted:
    """Authorization succeeded and a credential handle is ready."""

    handle: CredentialHandle


@dataclass(frozen=True, slots=True)
class NeedsConsent:
    """Authorization requires an OAuth consent interaction."""

    url: str
    pending: PendingAuth
    reason: str


@dataclass(frozen=True, slots=True)
class Denied:
    """Authorization is impossible in the current context."""

    reason: str


AuthorizationResolution = Granted | NeedsConsent | Denied


class Authorizer:
    """Resolve capability requirements into granted credentials or consent links."""

    def __init__(
        self,
        *,
        token_vault: AuthorizerTokenVault,
        pending_store: PendingAuthStore,
        dingtalk_client: AuthorizerDingTalkClient,
        oauth_config: OAuthConfig,
        actor_identity_resolver: ActorIdentityResolver | None = None,
        nonce_factory: NonceFactory = lambda: secrets.token_urlsafe(24),
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._token_vault = token_vault
        self._pending_store = pending_store
        self._dingtalk_client = dingtalk_client
        self._oauth_config = oauth_config
        self._actor_identity_resolver = actor_identity_resolver
        self._nonce_factory = nonce_factory
        self._audit_logger = audit_logger

    async def resolve(
        self,
        requirement: Requirement,
        actor: AuthorizerActorContext,
        mode: CapabilityMode,
        *,
        principal_id: str | None = None,
        session_id: str | None = None,
    ) -> AuthorizationResolution:
        """Resolve one requirement for the current actor and conversation mode."""

        if not isinstance(requirement, Requirement):
            raise TypeError("requirement must be a Requirement instance")
        actor_id = _non_empty_string(getattr(actor, "id", None), "actor.id")
        if mode not in ("dm", "group"):
            raise ValueError("mode must be 'dm' or 'group'")

        principal = _non_empty_string(principal_id or f"user:{actor_id}", "principal_id")
        session = _non_empty_string(session_id or principal, "session_id")
        if requirement.on_behalf_of is None:
            return Granted(
                CredentialHandle.application(
                    service=requirement.service,
                    scopes=requirement.scopes,
                    principal_id=principal,
                    actor_id=actor_id,
                )
            )
        if requirement.on_behalf_of != "actor":
            reason = f"unsupported on_behalf_of target: {requirement.on_behalf_of}"
            await self._record_obo_decision(
                actor_id=actor_id,
                principal_id=principal,
                session_id=session,
                requirement=requirement,
                mode=mode,
                decision="denied",
                reason=reason,
            )
            return Denied(reason)
        if mode != "dm":
            reason = "OBO requirements can only be granted in DM sessions"
            await self._record_obo_decision(
                actor_id=actor_id,
                principal_id=principal,
                session_id=session,
                requirement=requirement,
                mode=mode,
                decision="denied",
                reason=reason,
            )
            return Denied(reason)

        resolution = await self._token_vault.get_valid(
            principal,
            requirement.service,
            refresh=self._dingtalk_client.refresh_user_access_token,
            refresh_rejected_exceptions=(DingTalkUserTokenRefreshRejected,),
        )
        if resolution.token is not None and _scopes_cover(
            resolution.token.scopes,
            requirement.scopes,
        ):
            await self._record_obo_decision(
                actor_id=actor_id,
                principal_id=principal,
                session_id=session,
                requirement=requirement,
                mode=mode,
                decision="granted",
                refreshed=resolution.refreshed,
            )
            return Granted(
                _handle_from_user_token(
                    resolution.token,
                    scopes=requirement.scopes,
                    actor_id=actor_id,
                    refreshed=resolution.refreshed,
                )
            )

        reason = (
            "missing_scopes"
            if resolution.token is not None
            else resolution.reauthorization_reason or "missing"
        )
        actor_identity = await self._actor_identity(actor)
        pending = await self._pending_store.create(
            nonce=_non_empty_string(self._nonce_factory(), "nonce"),
            principal=principal,
            actor=actor_identity,
            session=session,
            service=requirement.service,
            scopes=requirement.scopes,
        )
        await self._record_obo_decision(
            actor_id=actor_id,
            principal_id=principal,
            session_id=session,
            requirement=requirement,
            mode=mode,
            decision="needs_consent",
            reason=reason,
            pending_nonce=pending.nonce,
            actor_identity=actor_identity,
        )
        return NeedsConsent(
            url=build_oauth_start_url(self._oauth_config, pending),
            pending=pending,
            reason=reason,
        )

    async def _actor_identity(self, actor: AuthorizerActorContext) -> str:
        if self._actor_identity_resolver is None:
            return _non_empty_string(getattr(actor, "id", None), "actor.id")
        result = self._actor_identity_resolver(actor)
        if inspect.isawaitable(result):
            result = await result
        return _non_empty_string(result, "actor_identity")

    async def _record_obo_decision(
        self,
        *,
        actor_id: str,
        principal_id: str,
        session_id: str,
        requirement: Requirement,
        mode: CapabilityMode,
        decision: OBOAuditDecision,
        reason: str | None = None,
        refreshed: bool = False,
        pending_nonce: str | None = None,
        actor_identity: str | None = None,
    ) -> None:
        if self._audit_logger is None:
            return
        await self._audit_logger.record_obo_authorization(
            actor_id=actor_id,
            principal_id=principal_id,
            session_id=session_id,
            service=requirement.service,
            scopes=requirement.scopes,
            mode=mode,
            decision=decision,
            on_behalf_of=requirement.on_behalf_of,
            reason=reason,
            refreshed=refreshed,
            pending_nonce=pending_nonce,
            actor_identity=actor_identity,
        )


def build_oauth_start_url(oauth_config: OAuthConfig, pending: PendingAuth) -> str:
    """Build the assistant `/oauth/start` URL for one pending authorization."""

    redirect_uri = _non_empty_string(oauth_config.redirect_uri, "oauth.redirect_uri")
    parsed = urlparse(redirect_uri)
    if parsed.scheme == "" or parsed.netloc == "":
        raise AuthorizerError("OAUTH_REDIRECT_URI must be an absolute URL")
    query = urlencode({"nonce": pending.nonce})
    return urlunparse((parsed.scheme, parsed.netloc, "/oauth/start", "", query, ""))


def _handle_from_user_token(
    token: UserToken,
    *,
    scopes: Sequence[str],
    actor_id: str,
    refreshed: bool,
) -> CredentialHandle:
    return CredentialHandle.user_token(
        service=token.service,
        user_access_token=token.user_access_token,
        scopes=scopes,
        principal_id=token.principal_id,
        actor_id=actor_id,
        refreshed=refreshed,
    )


def _scopes_cover(granted_scopes: Sequence[str], required_scopes: Sequence[str]) -> bool:
    return set(required_scopes).issubset(set(granted_scopes))


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


__all__ = [
    "AuthorizationResolution",
    "Authorizer",
    "AuthorizerActorContext",
    "AuthorizerDingTalkClient",
    "AuthorizerError",
    "AuthorizerTokenVault",
    "Denied",
    "Granted",
    "NeedsConsent",
    "build_oauth_start_url",
]
