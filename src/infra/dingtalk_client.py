"""Async DingTalk OpenAPI client with application access-token caching."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from src.infra.config import DingTalkConfig
from src.infra.log import get_logger

logger = get_logger(__name__)

ACCESS_TOKEN_PATH = "/v1.0/oauth2/accessToken"
TOKEN_HEADER = "x-acs-dingtalk-access-token"
TOKEN_REFRESH_SKEW_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class AccessToken:
    """DingTalk application access token response."""

    access_token: str
    expire_in: int


class DingTalkAPIError(RuntimeError):
    """Raised when DingTalk returns an HTTP or application-level API error."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        errcode: str | int | None,
        errmsg: str | None,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.errcode = errcode
        self.errmsg = errmsg

        details = [f"{method} {path} failed", f"status={status_code}"]
        if errcode is not None:
            details.append(f"errcode={errcode}")
        if errmsg:
            details.append(f"errmsg={errmsg}")
        super().__init__(", ".join(details))


class DingTalkClient:
    """Client for DingTalk OpenAPI calls that need application or user tokens."""

    def __init__(
        self,
        config: DingTalkConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http_client = http_client is None
        self._clock = clock
        self._token_lock = asyncio.Lock()
        self._cached_token: AccessToken | None = None
        self._refresh_after = 0.0

    async def __aenter__(self) -> DingTalkClient:
        """Return this client when used as an async context manager."""

        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close an internally owned HTTP client on context-manager exit."""

        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally owned HTTP client, if this client created one."""

        if self._owns_http_client:
            await self._http_client.aclose()

    async def get_access_token(self) -> AccessToken:
        """Fetch and cache the DingTalk application access token."""

        if self._cached_token is not None and self._is_cached_token_fresh():
            return self._cached_token

        async with self._token_lock:
            if self._cached_token is not None and self._is_cached_token_fresh():
                return self._cached_token

            token = await self._fetch_access_token()
            self._cached_token = token
            self._refresh_after = self._clock() + max(
                0,
                token.expire_in - TOKEN_REFRESH_SKEW_SECONDS,
            )
            return token

    async def api_post(
        self,
        path: str,
        json: Mapping[str, Any] | None,
        *,
        use_user_token: str | None = None,
    ) -> Any:
        """POST JSON to DingTalk with the application token or a supplied user token."""

        return await self._api_request("POST", path, json=json, use_user_token=use_user_token)

    async def api_get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        use_user_token: str | None = None,
    ) -> Any:
        """GET from DingTalk with the application token or a supplied user token."""

        return await self._api_request("GET", path, params=params, use_user_token=use_user_token)

    def _is_cached_token_fresh(self) -> bool:
        return self._clock() < self._refresh_after

    async def _fetch_access_token(self) -> AccessToken:
        request_body = {
            "appKey": self._config.app_key,
            "appSecret": self._config.app_secret,
        }
        try:
            response = await self._http_client.post(
                self._build_url(ACCESS_TOKEN_PATH),
                json=request_body,
            )
        except httpx.HTTPError:
            logger.exception(
                "dingtalk_access_token_request_failed",
                extra={"method": "POST", "path": ACCESS_TOKEN_PATH},
            )
            raise

        payload = _parse_response(response, method="POST", path=ACCESS_TOKEN_PATH)
        if not isinstance(payload, Mapping):
            raise DingTalkAPIError(
                method="POST",
                path=ACCESS_TOKEN_PATH,
                status_code=response.status_code,
                errcode=None,
                errmsg="access token response must be a JSON object",
            )

        access_token = payload.get("accessToken")
        expire_in = payload.get("expireIn")
        if (
            not isinstance(access_token, str)
            or access_token.strip() == ""
            or isinstance(expire_in, bool)
            or not isinstance(expire_in, int)
            or expire_in <= 0
        ):
            logger.error(
                "dingtalk_access_token_response_invalid",
                extra={
                    "method": "POST",
                    "path": ACCESS_TOKEN_PATH,
                    "status_code": response.status_code,
                },
            )
            raise DingTalkAPIError(
                method="POST",
                path=ACCESS_TOKEN_PATH,
                status_code=response.status_code,
                errcode=None,
                errmsg="access token response must include accessToken and positive expireIn",
            )

        return AccessToken(access_token=access_token.strip(), expire_in=expire_in)

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        use_user_token: str | None = None,
    ) -> Any:
        access_token = await self._resolve_access_token(use_user_token)
        try:
            response = await self._http_client.request(
                method,
                self._build_url(path),
                headers={TOKEN_HEADER: access_token},
                json=json,
                params=params,
            )
        except httpx.HTTPError:
            logger.exception(
                "dingtalk_api_request_failed",
                extra={"method": method, "path": path},
            )
            raise

        return _parse_response(response, method=method, path=path)

    async def _resolve_access_token(self, use_user_token: str | None) -> str:
        if use_user_token is not None:
            token = use_user_token.strip()
            if token == "":
                raise ValueError("use_user_token must be a non-empty string when provided")
            return token
        return (await self.get_access_token()).access_token

    def _build_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._config.api_base.rstrip('/')}/{path.lstrip('/')}"


def _parse_response(response: httpx.Response, *, method: str, path: str) -> Any:
    payload = _json_payload(response, method=method, path=path)
    errcode, errmsg = _api_error(payload)
    if response.is_error or errcode is not None:
        if errmsg is None and response.is_error:
            errmsg = response.reason_phrase
        logger.error(
            "dingtalk_api_error",
            extra={
                "method": method,
                "path": path,
                "status_code": response.status_code,
                "errcode": errcode,
                "errmsg": errmsg,
            },
        )
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=response.status_code,
            errcode=errcode,
            errmsg=errmsg,
        )
    return payload


def _json_payload(response: httpx.Response, *, method: str, path: str) -> Any:
    if not response.content:
        return {}

    try:
        return response.json()
    except ValueError as exc:
        errmsg = response.text.strip() or response.reason_phrase
        logger.error(
            "dingtalk_api_invalid_json",
            extra={
                "method": method,
                "path": path,
                "status_code": response.status_code,
                "errmsg": errmsg,
            },
        )
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=response.status_code,
            errcode=None,
            errmsg=errmsg,
        ) from exc


def _api_error(payload: Any) -> tuple[str | int | None, str | None]:
    if not isinstance(payload, Mapping):
        return None, None

    errcode = payload.get("errcode")
    if errcode not in (None, 0, "0"):
        return errcode, _optional_message(payload.get("errmsg"))

    code = payload.get("code")
    if code not in (None, 0, "0", "ok", "OK"):
        return code, _optional_message(payload.get("message"))

    return None, None


def _optional_message(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
