"""DingTalk OAuth2 endpoints and pending authorization state."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from aiohttp import web

from src.infra.config import AppConfig, DingTalkConfig, OAuthConfig
from src.infra.dingtalk_client import TOKEN_HEADER, DingTalkAPIError, parse_dingtalk_response
from src.infra.log import get_logger
from src.infra.token_vault import UserToken

logger = get_logger(__name__)

AUTHORIZATION_URL = "https://login.dingtalk.com/oauth2/auth"
USER_ACCESS_TOKEN_PATH = "/v1.0/oauth2/userAccessToken"
CONTACT_USER_ME_PATH = "/v1.0/contact/users/me"
DEFAULT_PENDING_AUTH_TTL_SECONDS = 600
DEFAULT_TIMEOUT_SECONDS = 10.0
OAUTH_SUCCESS_MESSAGE = "DingTalk authorization completed. You can return to the chat."
OAUTH_IDENTITY_MISMATCH_MESSAGE = "DingTalk authorization user did not match the requesting actor."


class OAuthError(RuntimeError):
    """Raised when a DingTalk OAuth flow cannot continue."""


class PendingAuthNotFound(OAuthError):
    """Raised when an OAuth state nonce is unknown or already consumed."""


class PendingAuthExpired(OAuthError):
    """Raised when an OAuth state nonce exists but has expired."""


class OAuthIdentityMismatch(OAuthError):
    """Raised when the authorized DingTalk user is not the pending actor."""


class OAuthTokenVault(Protocol):
    """TokenVault methods needed by the OAuth callback."""

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
        """Persist verified user-level token material."""


@dataclass(frozen=True, slots=True)
class PendingAuth:
    """One short-lived, single-use OAuth authorization request."""

    nonce: str
    principal_id: str
    actor_id: str
    session_id: str
    service: str
    scopes: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthUserToken:
    """DingTalk user-level OAuth token returned by the authorization-code exchange."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expire_in: int
    expires_at: datetime
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class OAuthUserIdentity:
    """DingTalk identity proven by calling `contact/users/me` with the user token."""

    union_id: str
    user_id: str | None = None
    name: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class OAuthCallbackResult:
    """Verified pending request plus the exchanged and stored DingTalk user token."""

    pending: PendingAuth
    token: OAuthUserToken
    identity: OAuthUserIdentity | None = None
    stored_token: UserToken | None = field(default=None, repr=False)


class PendingAuthStore:
    """In-memory store for short-lived OAuth nonces awaiting a callback."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_PENDING_AUTH_TTL_SECONDS,
        now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ttl = timedelta(seconds=_positive_int(ttl_seconds, "ttl_seconds"))
        self._now_factory = now_factory
        self._pending: dict[str, PendingAuth] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        nonce: str,
        principal: str,
        actor: str | None = None,
        session: str,
        service: str,
        scopes: Sequence[str],
        expires_at: datetime | None = None,
    ) -> PendingAuth:
        """Create one pending authorization nonce."""

        now = _to_utc(self._now_factory())
        normalized_nonce = _non_empty_string(nonce, "nonce")
        normalized_principal = _non_empty_string(principal, "principal")
        pending = PendingAuth(
            nonce=normalized_nonce,
            principal_id=normalized_principal,
            actor_id=(
                _non_empty_string(actor, "actor")
                if actor is not None
                else _actor_from_principal(normalized_principal)
            ),
            session_id=_non_empty_string(session, "session"),
            service=_non_empty_string(service, "service"),
            scopes=_normalize_scopes(scopes),
            expires_at=_to_utc(expires_at) if expires_at is not None else now + self._ttl,
        )
        if pending.expires_at <= now:
            raise ValueError("expires_at must be in the future")

        async with self._lock:
            self._purge_expired_locked(now)
            if normalized_nonce in self._pending:
                raise ValueError(f"pending OAuth nonce already exists: {normalized_nonce}")
            self._pending[normalized_nonce] = pending
        return pending

    async def get(self, nonce: str) -> PendingAuth | None:
        """Return a pending authorization without consuming it."""

        now = _to_utc(self._now_factory())
        normalized_nonce = _non_empty_string(nonce, "nonce")
        async with self._lock:
            pending = self._pending.get(normalized_nonce)
            if pending is None:
                return None
            if _is_expired(pending, now):
                del self._pending[normalized_nonce]
                return None
            return pending

    async def consume(self, nonce: str) -> PendingAuth:
        """Consume and return a pending authorization exactly once."""

        now = _to_utc(self._now_factory())
        normalized_nonce = _non_empty_string(nonce, "nonce")
        async with self._lock:
            pending = self._pending.pop(normalized_nonce, None)

        if pending is None:
            raise PendingAuthNotFound(f"Unknown OAuth state nonce: {normalized_nonce}")
        if _is_expired(pending, now):
            raise PendingAuthExpired(f"Expired OAuth state nonce: {normalized_nonce}")
        return pending

    async def discard(self, nonce: str) -> bool:
        """Remove a pending authorization without treating it as completed."""

        normalized_nonce = _non_empty_string(nonce, "nonce")
        async with self._lock:
            return self._pending.pop(normalized_nonce, None) is not None

    def _purge_expired_locked(self, now: datetime) -> None:
        expired = [nonce for nonce, pending in self._pending.items() if _is_expired(pending, now)]
        for nonce in expired:
            del self._pending[nonce]


class DingTalkOAuthClient:
    """Client for exchanging DingTalk OAuth authorization codes for user tokens."""

    def __init__(
        self,
        config: DingTalkConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http_client = http_client is None
        self._now_factory = now_factory

    async def __aenter__(self) -> DingTalkOAuthClient:
        """Return this client when used as an async context manager."""

        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close an internally owned HTTP client on context-manager exit."""

        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally owned HTTP client, if this client created one."""

        if self._owns_http_client:
            await self._http_client.aclose()

    async def exchange_authorization_code(self, code: str) -> OAuthUserToken:
        """Exchange one DingTalk OAuth authorization code for a user-level token."""

        request_body = {
            "clientId": self._config.app_key,
            "clientSecret": self._config.app_secret,
            "code": _non_empty_string(code, "code"),
            "grantType": "authorization_code",
        }
        try:
            response = await self._http_client.post(
                self._build_url(USER_ACCESS_TOKEN_PATH),
                json=request_body,
            )
        except httpx.HTTPError:
            logger.exception(
                "dingtalk_user_token_request_failed",
                extra={"method": "POST", "path": USER_ACCESS_TOKEN_PATH},
            )
            raise

        payload = parse_dingtalk_response(
            response,
            method="POST",
            path=USER_ACCESS_TOKEN_PATH,
        )
        return _parse_user_token_payload(
            payload,
            now=_to_utc(self._now_factory()),
            method="POST",
            path=USER_ACCESS_TOKEN_PATH,
        )

    async def get_current_user(self, user_access_token: str) -> OAuthUserIdentity:
        """Fetch the DingTalk identity represented by one user-level token."""

        try:
            response = await self._http_client.get(
                self._build_url(CONTACT_USER_ME_PATH),
                headers={
                    TOKEN_HEADER: _non_empty_string(user_access_token, "user_access_token"),
                },
            )
        except httpx.HTTPError:
            logger.exception(
                "dingtalk_oauth_current_user_request_failed",
                extra={"method": "GET", "path": CONTACT_USER_ME_PATH},
            )
            raise

        payload = parse_dingtalk_response(
            response,
            method="GET",
            path=CONTACT_USER_ME_PATH,
        )
        return _parse_current_user_payload(
            payload,
            method="GET",
            path=CONTACT_USER_ME_PATH,
        )

    def _build_url(self, path: str) -> str:
        return f"{self._config.api_base.rstrip('/')}/{path.lstrip('/')}"


class OAuthRequestHandler:
    """aiohttp route handler for DingTalk OAuth start and callback endpoints."""

    def __init__(
        self,
        *,
        config: AppConfig,
        pending_store: PendingAuthStore,
        token_vault: OAuthTokenVault,
        oauth_client: DingTalkOAuthClient | None = None,
        authorization_url: str = AUTHORIZATION_URL,
        on_authorized: Callable[[OAuthCallbackResult], Awaitable[None] | None] | None = None,
    ) -> None:
        self._config = config
        self._pending_store = pending_store
        self._token_vault = token_vault
        self._oauth_client = oauth_client or DingTalkOAuthClient(config.dingtalk)
        self._owns_oauth_client = oauth_client is None
        self._authorization_url = _non_empty_string(authorization_url, "authorization_url")
        self._on_authorized = on_authorized

    async def aclose(self) -> None:
        """Close internally owned resources."""

        if self._owns_oauth_client:
            await self._oauth_client.aclose()

    async def start(self, request: web.Request) -> web.StreamResponse:
        """Validate a pending nonce and redirect the browser to DingTalk consent."""

        nonce = _required_query_param(request, "nonce")
        pending = await self._pending_store.get(nonce)
        if pending is None:
            logger.warning("dingtalk_oauth_start_unknown_nonce", extra={"nonce": nonce})
            raise web.HTTPNotFound(text="Unknown or expired OAuth nonce")

        redirect_url = build_authorization_url(
            self._config.dingtalk,
            self._config.oauth,
            pending,
            authorization_url=self._authorization_url,
        )
        raise web.HTTPFound(redirect_url)

    async def callback(self, request: web.Request) -> web.Response:
        """Consume OAuth state, exchange the code, and report the completed result."""

        code = _required_query_param(request, "code")
        state = _required_query_param(request, "state")
        try:
            pending = await self._pending_store.consume(state)
        except PendingAuthNotFound:
            logger.warning("dingtalk_oauth_callback_unknown_state", extra={"state": state})
            raise web.HTTPNotFound(text="Unknown OAuth state") from None
        except PendingAuthExpired:
            logger.warning("dingtalk_oauth_callback_expired_state", extra={"state": state})
            raise web.HTTPGone(text="Expired OAuth state") from None

        token = await self._oauth_client.exchange_authorization_code(code)
        identity = await self._oauth_client.get_current_user(token.access_token)
        if identity.union_id != pending.actor_id:
            logger.warning(
                "dingtalk_oauth_identity_mismatch",
                extra={
                    "principal_id": pending.principal_id,
                    "actor_id": pending.actor_id,
                    "authorized_union_id": identity.union_id,
                    "session_id": pending.session_id,
                    "service": pending.service,
                },
            )
            raise web.HTTPForbidden(text=OAUTH_IDENTITY_MISMATCH_MESSAGE)

        stored_token = await self._token_vault.put(
            principal=pending.principal_id,
            service=pending.service,
            user_access_token=token.access_token,
            refresh_token=token.refresh_token,
            scopes=pending.scopes,
            expires_at=token.expires_at,
        )
        result = OAuthCallbackResult(
            pending=pending,
            token=token,
            identity=identity,
            stored_token=stored_token,
        )
        await self._notify_authorized(result)
        logger.info(
            "dingtalk_oauth_callback_completed",
            extra={
                "principal_id": pending.principal_id,
                "actor_id": pending.actor_id,
                "authorized_union_id": identity.union_id,
                "session_id": pending.session_id,
                "service": pending.service,
                "scopes": list(pending.scopes),
                "expires_at": token.expires_at.isoformat(),
            },
        )
        return web.Response(text=OAUTH_SUCCESS_MESSAGE)

    async def _notify_authorized(self, result: OAuthCallbackResult) -> None:
        if self._on_authorized is None:
            return

        callback_result = self._on_authorized(result)
        if inspect.isawaitable(callback_result):
            await callback_result


OAUTH_HANDLER_KEY = web.AppKey("oauth_handler", OAuthRequestHandler)


def create_oauth_app(
    config: AppConfig,
    pending_store: PendingAuthStore,
    *,
    token_vault: OAuthTokenVault,
    oauth_client: DingTalkOAuthClient | None = None,
    authorization_url: str = AUTHORIZATION_URL,
    on_authorized: Callable[[OAuthCallbackResult], Awaitable[None] | None] | None = None,
) -> web.Application:
    """Create an aiohttp application exposing DingTalk OAuth endpoints."""

    handler = OAuthRequestHandler(
        config=config,
        pending_store=pending_store,
        token_vault=token_vault,
        oauth_client=oauth_client,
        authorization_url=authorization_url,
        on_authorized=on_authorized,
    )
    app = web.Application()
    app[OAUTH_HANDLER_KEY] = handler
    app.router.add_get("/oauth/start", handler.start)
    app.router.add_get("/oauth/callback", handler.callback)

    async def cleanup(_: web.Application) -> None:
        await handler.aclose()

    app.on_cleanup.append(cleanup)
    return app


def build_authorization_url(
    dingtalk_config: DingTalkConfig,
    oauth_config: OAuthConfig,
    pending: PendingAuth,
    *,
    authorization_url: str = AUTHORIZATION_URL,
) -> str:
    """Build the DingTalk browser authorization URL for one pending request."""

    params = {
        "client_id": dingtalk_config.app_key,
        "response_type": "code",
        "scope": "openid",
        "state": pending.nonce,
        "redirect_uri": oauth_config.redirect_uri,
        "prompt": "consent",
    }
    return f"{_non_empty_string(authorization_url, 'authorization_url')}?{urlencode(params)}"


def _required_query_param(request: web.Request, name: str) -> str:
    value = request.query.get(name)
    if value is None or value.strip() == "":
        raise web.HTTPBadRequest(text=f"Missing required query parameter: {name}")
    return value.strip()


def _parse_user_token_payload(
    payload: Any,
    *,
    now: datetime,
    method: str,
    path: str,
) -> OAuthUserToken:
    if not isinstance(payload, Mapping):
        raise _invalid_user_token_response(method=method, path=path)

    access_token = payload.get("accessToken")
    refresh_token = payload.get("refreshToken")
    expire_in = payload.get("expireIn")
    if (
        not isinstance(access_token, str)
        or access_token.strip() == ""
        or not isinstance(refresh_token, str)
        or refresh_token.strip() == ""
        or isinstance(expire_in, bool)
        or not isinstance(expire_in, int)
        or expire_in <= 0
    ):
        raise _invalid_user_token_response(method=method, path=path)

    return OAuthUserToken(
        access_token=access_token.strip(),
        refresh_token=refresh_token.strip(),
        expire_in=expire_in,
        expires_at=now + timedelta(seconds=expire_in),
        raw=dict(payload),
    )


def _parse_current_user_payload(
    payload: Any,
    *,
    method: str,
    path: str,
) -> OAuthUserIdentity:
    payload_object = _response_object(payload, method=method, path=path)
    if isinstance(payload_object.get("result"), Mapping):
        payload_object = _response_object(payload_object["result"], method=method, path=path)

    union_id = _string_field(payload_object, "unionId", "unionid", "union_id")
    if union_id is None:
        raise _invalid_current_user_response(method=method, path=path)

    return OAuthUserIdentity(
        union_id=union_id,
        user_id=_string_field(payload_object, "userId", "userid", "staffId", "staffid"),
        name=_string_field(payload_object, "name", "username", "nick", "nickname"),
        raw=dict(payload_object),
    )


def _invalid_user_token_response(*, method: str, path: str) -> DingTalkAPIError:
    logger.error(
        "dingtalk_user_token_response_invalid",
        extra={"method": method, "path": path, "status_code": 200},
    )
    return DingTalkAPIError(
        method=method,
        path=path,
        status_code=200,
        errcode=None,
        errmsg="user token response must include accessToken, refreshToken, and positive expireIn",
    )


def _invalid_current_user_response(*, method: str, path: str) -> DingTalkAPIError:
    logger.error(
        "dingtalk_current_user_response_invalid",
        extra={"method": method, "path": path, "status_code": 200},
    )
    return DingTalkAPIError(
        method=method,
        path=path,
        status_code=200,
        errcode=None,
        errmsg="current user response must include unionId",
    )


def _response_object(payload: Any, *, method: str, path: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=200,
            errcode=None,
            errmsg="DingTalk response must be a JSON object",
        )
    return payload


def _string_field(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()
    return None


def _normalize_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    if isinstance(scopes, (str, bytes)):
        raise ValueError("scopes must be a sequence of strings")
    return tuple(dict.fromkeys(_non_empty_string(scope, "scope") for scope in scopes))


def _actor_from_principal(principal: str) -> str:
    normalized = _non_empty_string(principal, "principal")
    if normalized.startswith("user:") and normalized != "user:":
        return normalized.removeprefix("user:")
    return normalized


def _non_empty_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"`{field_name}` must be a non-empty string")
    return value.strip()


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"`{field_name}` must be a positive integer")
    return value


def _is_expired(pending: PendingAuth, now: datetime) -> bool:
    return _to_utc(pending.expires_at) <= _to_utc(now)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
