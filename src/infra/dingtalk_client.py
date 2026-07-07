"""Async DingTalk OpenAPI client with application access-token caching."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from src.infra.config import DingTalkConfig
from src.infra.log import get_logger

logger = get_logger(__name__)

ACCESS_TOKEN_PATH = "/v1.0/oauth2/accessToken"
USER_ACCESS_TOKEN_PATH = "/v1.0/oauth2/userAccessToken"
OTO_MESSAGE_PATH = "/v1.0/robot/oToMessages/batchSend"
GROUP_MESSAGE_PATH = "/v1.0/robot/groupMessages/send"
CONTACT_USER_LIST_PATH_TEMPLATE = "/v1.0/contact/departments/{department_id}/users"
CONTACT_USER_BY_ID_PATH_TEMPLATE = "/v1.0/contact/users/{user_id}"
DOCUMENT_CREATE_PATH = "/v1.0/documents"
DOCUMENT_CONTENT_BLOCKS_PATH_TEMPLATE = "/v1.0/documents/{doc_id}/contentBlocks"
TODO_CREATE_PATH_TEMPLATE = "/v1.0/todo/users/{union_id}/tasks"
CALENDAR_PRIMARY_PATH = "/v1.0/calendar/primary"
CALENDAR_EVENTS_PATH_TEMPLATE = "/v1.0/calendar/users/{user_id}/calendars/{calendar_id}/events"
CARD_INSTANCE_CREATE_PATH = "/v1.0/card/instances"
CARD_INSTANCE_DELIVER_PATH = "/v1.0/card/instances/deliver"
TOKEN_HEADER = "x-acs-dingtalk-access-token"
TOKEN_REFRESH_SKEW_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CONTACT_DEPARTMENT_ID = "1"
MAX_CONTACT_PAGE_SIZE = 100
TEXT_MESSAGE_KEY = "sampleText"
MARKDOWN_BUTTON_CARD_TEMPLATE_ID = "1366a1eb-bc54-4859-ac88-517c56a9acb1.schema"


@dataclass(frozen=True, slots=True)
class AccessToken:
    """DingTalk application access token response."""

    access_token: str
    expire_in: int


@dataclass(frozen=True, slots=True)
class DingTalkUser:
    """Normalized DingTalk contact user details."""

    user_id: str
    name: str
    raw: Mapping[str, Any]
    union_id: str | None = None


@dataclass(frozen=True, slots=True)
class DingTalkDocument:
    """Normalized DingTalk document creation result."""

    doc_id: str
    raw: Mapping[str, Any]
    url: str | None = None


@dataclass(frozen=True, slots=True)
class DingTalkTodo:
    """Normalized DingTalk todo creation result."""

    task_id: str
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DingTalkCardDelivery:
    """DingTalk interactive-card create and deliver result."""

    card_instance_id: str
    create_payload: Any
    deliver_payload: Any


@dataclass(frozen=True, slots=True)
class DingTalkCalendar:
    """Normalized DingTalk calendar metadata."""

    calendar_id: str
    raw: Mapping[str, Any]
    summary: str | None = None
    time_zone: str | None = None


@dataclass(frozen=True, slots=True)
class DingTalkCalendarEvent:
    """Normalized DingTalk calendar event details."""

    event_id: str | None
    raw: Mapping[str, Any]
    summary: str | None = None
    description: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = None


@dataclass(frozen=True, slots=True)
class DingTalkUserAccessToken:
    """DingTalk user-level OAuth token returned by refresh-token exchange."""

    access_token: str
    refresh_token: str
    expire_in: int
    expires_at: datetime
    raw: Mapping[str, Any]


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


class DingTalkUserTokenRefreshRejected(RuntimeError):
    """Raised when DingTalk rejects a stored user refresh token as unusable."""

    def __init__(self, error: DingTalkAPIError) -> None:
        self.error = error
        super().__init__(f"DingTalk user refresh token was rejected: {error}")


class DingTalkClient:
    """Client for DingTalk OpenAPI calls that need application or user tokens."""

    def __init__(
        self,
        config: DingTalkConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http_client = http_client is None
        self._clock = clock
        self._now_factory = now_factory
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

    async def refresh_user_access_token(self, refresh_token: str) -> DingTalkUserAccessToken:
        """Exchange a stored DingTalk user refresh token for fresh user token material."""

        request_body = {
            "clientId": self._config.app_key,
            "clientSecret": self._config.app_secret,
            "refreshToken": _non_empty_string(refresh_token, "refresh_token"),
            "grantType": "refresh_token",
        }
        try:
            response = await self._http_client.post(
                self._build_url(USER_ACCESS_TOKEN_PATH),
                json=request_body,
            )
            payload = _parse_response(response, method="POST", path=USER_ACCESS_TOKEN_PATH)
        except DingTalkAPIError as exc:
            if _is_refresh_token_rejected(exc):
                logger.warning(
                    "dingtalk_user_token_refresh_rejected",
                    extra={
                        "method": "POST",
                        "path": USER_ACCESS_TOKEN_PATH,
                        "status_code": exc.status_code,
                        "errcode": exc.errcode,
                        "errmsg": exc.errmsg,
                    },
                )
                raise DingTalkUserTokenRefreshRejected(exc) from exc
            raise
        except httpx.HTTPError:
            logger.exception(
                "dingtalk_user_token_refresh_request_failed",
                extra={"method": "POST", "path": USER_ACCESS_TOKEN_PATH},
            )
            raise

        return _parse_user_access_token_payload(
            payload,
            now=_to_utc(self._now_factory()),
            method="POST",
            path=USER_ACCESS_TOKEN_PATH,
        )

    async def send_oto(self, user_ids: list[str], text: str) -> Any:
        """Send a text robot message to one or more DingTalk one-to-one chats."""

        request_body = {
            "robotCode": self._config.robot_code,
            "userIds": _normalize_user_ids(user_ids),
            "msgKey": TEXT_MESSAGE_KEY,
            "msgParam": _text_msg_param(text),
        }
        return await self.api_post(OTO_MESSAGE_PATH, request_body)

    async def send_group(self, open_conversation_id: str, text: str) -> Any:
        """Send a text robot message to a DingTalk group conversation."""

        request_body = {
            "robotCode": self._config.robot_code,
            "openConversationId": _non_empty_string(
                open_conversation_id,
                "open_conversation_id",
            ),
            "msgKey": TEXT_MESSAGE_KEY,
            "msgParam": _text_msg_param(text),
        }
        return await self.api_post(GROUP_MESSAGE_PATH, request_body)

    async def send_confirm_card(
        self,
        *,
        conversation_type: int,
        conversation_id: str,
        responder_user_id: str,
        action: str,
        details: Mapping[str, Any],
        correlation_id: str,
        open_conversation_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> DingTalkCardDelivery:
        """Create and deliver an interactive confirm/cancel card with Stream callbacks."""

        normalized_correlation_id = _non_empty_string(correlation_id, "correlation_id")
        normalized_action = _non_empty_string(action, "action")
        normalized_details = _plain_json_object(details, "details")
        card_data = {
            "title": "请确认操作",
            "markdown": _confirm_card_markdown(
                action=normalized_action,
                details=normalized_details,
                expires_at=expires_at,
            ),
            "tips": normalized_action,
            "sys_full_json_obj": json.dumps(
                {
                    "msgButtons": [
                        _confirm_card_button(
                            text="确认",
                            color="blue",
                            correlation_id=normalized_correlation_id,
                            decision="confirm",
                        ),
                        _confirm_card_button(
                            text="取消",
                            color="gray",
                            correlation_id=normalized_correlation_id,
                            decision="cancel",
                        ),
                    ]
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        create_payload = await self.api_post(
            CARD_INSTANCE_CREATE_PATH,
            {
                "cardTemplateId": MARKDOWN_BUTTON_CARD_TEMPLATE_ID,
                "outTrackId": normalized_correlation_id,
                "cardData": {"cardParamMap": card_data},
                "callbackType": "STREAM",
                "imGroupOpenSpaceModel": {"supportForward": False},
                "imRobotOpenSpaceModel": {"supportForward": False},
            },
        )
        deliver_payload = await self.api_post(
            CARD_INSTANCE_DELIVER_PATH,
            _confirm_card_deliver_body(
                conversation_type=conversation_type,
                conversation_id=conversation_id,
                open_conversation_id=open_conversation_id,
                responder_user_id=responder_user_id,
                robot_code=self._config.robot_code,
                correlation_id=normalized_correlation_id,
            ),
        )
        return DingTalkCardDelivery(
            card_instance_id=normalized_correlation_id,
            create_payload=create_payload,
            deliver_payload=deliver_payload,
        )

    async def get_user_list(
        self,
        *,
        department_id: str | int = DEFAULT_CONTACT_DEPARTMENT_ID,
        page_size: int = MAX_CONTACT_PAGE_SIZE,
    ) -> dict[str, str]:
        """Return a DingTalk contact mapping from userId to display name."""

        normalized_department_id = _normalize_identifier(department_id, "department_id")
        normalized_page_size = _normalize_page_size(page_size)
        path = CONTACT_USER_LIST_PATH_TEMPLATE.format(
            department_id=_quote_path_segment(normalized_department_id)
        )

        users: dict[str, str] = {}
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {"maxResults": normalized_page_size}
            if page_token is not None:
                params["pageToken"] = page_token

            payload = await self.api_get(path, params=params)
            for user in _extract_contact_users(payload, method="GET", path=path):
                users[user.user_id] = user.name

            page_token = _extract_next_page_token(payload, method="GET", path=path)
            if page_token is None:
                return users

    async def user_by_id(self, user_id: str) -> DingTalkUser:
        """Fetch and normalize one DingTalk contact user by userId."""

        normalized_user_id = _non_empty_string(user_id, "user_id")
        path = CONTACT_USER_BY_ID_PATH_TEMPLATE.format(
            user_id=_quote_path_segment(normalized_user_id)
        )
        payload = await self.api_get(path)
        return _parse_contact_user(payload, method="GET", path=path)

    async def create_document(
        self,
        *,
        title: str,
        parent_object_type: str,
        parent_object_id: str,
    ) -> DingTalkDocument:
        """Create a DingTalk document with the application access token."""

        request_body = {
            "title": _non_empty_string(title, "title"),
            "parentObjectType": _non_empty_string(parent_object_type, "parent_object_type"),
            "parentObjectId": _non_empty_string(parent_object_id, "parent_object_id"),
        }
        payload = await self.api_post(DOCUMENT_CREATE_PATH, request_body)
        return _parse_document(payload, method="POST", path=DOCUMENT_CREATE_PATH)

    async def append_document_content(self, doc_id: str, text: str) -> Any:
        """Append one text content block to a DingTalk document."""

        normalized_doc_id = _non_empty_string(doc_id, "doc_id")
        path = DOCUMENT_CONTENT_BLOCKS_PATH_TEMPLATE.format(
            doc_id=_quote_path_segment(normalized_doc_id)
        )
        request_body = {
            "contentBlockType": "text",
            "blockContent": {"text": _non_empty_string(text, "text")},
        }
        return await self.api_post(path, request_body)

    async def create_todo(
        self,
        *,
        union_id: str,
        subject: str,
        creator_union_id: str,
        executor_union_ids: Sequence[str],
        description: str | None = None,
        due_time: int | None = None,
        priority: int | None = None,
        detail_url: Mapping[str, str] | None = None,
    ) -> DingTalkTodo:
        """Create a DingTalk todo task for one user with application credentials."""

        normalized_union_id = _non_empty_string(union_id, "union_id")
        path = TODO_CREATE_PATH_TEMPLATE.format(union_id=_quote_path_segment(normalized_union_id))
        request_body: dict[str, Any] = {
            "subject": _non_empty_string(subject, "subject"),
            "creatorId": _non_empty_string(creator_union_id, "creator_union_id"),
            "executorIds": _normalize_union_ids(executor_union_ids),
        }
        if description is not None:
            request_body["description"] = _non_empty_string(description, "description")
        if due_time is not None:
            request_body["dueTime"] = _non_negative_int(due_time, "due_time")
        if priority is not None:
            request_body["priority"] = _positive_int(priority, "priority")
        if detail_url is not None:
            request_body["detailUrl"] = _detail_url_mapping(detail_url)

        payload = await self.api_post(path, request_body)
        return _parse_todo(payload, method="POST", path=path)

    async def get_primary_calendar(self, *, use_user_token: str) -> DingTalkCalendar:
        """Fetch the primary calendar visible to one DingTalk user token."""

        payload = await self.api_get(
            CALENDAR_PRIMARY_PATH,
            use_user_token=_non_empty_string(use_user_token, "use_user_token"),
        )
        return _parse_calendar(payload, method="GET", path=CALENDAR_PRIMARY_PATH)

    async def list_calendar_events(
        self,
        *,
        user_id: str,
        calendar_id: str,
        start_time: datetime | str,
        end_time: datetime | str,
        use_user_token: str,
        page_size: int | None = None,
    ) -> list[DingTalkCalendarEvent]:
        """List DingTalk calendar events in a time range with a user-level token."""

        normalized_user_id = _non_empty_string(user_id, "user_id")
        normalized_calendar_id = _non_empty_string(calendar_id, "calendar_id")
        path = CALENDAR_EVENTS_PATH_TEMPLATE.format(
            user_id=_quote_path_segment(normalized_user_id),
            calendar_id=_quote_path_segment(normalized_calendar_id),
        )
        base_params: dict[str, str | int] = {
            "startTime": _calendar_query_time(start_time, "start_time"),
            "endTime": _calendar_query_time(end_time, "end_time"),
        }
        if page_size is not None:
            base_params["maxResults"] = _positive_int(page_size, "page_size")

        events: list[DingTalkCalendarEvent] = []
        page_token: str | None = None
        while True:
            params = dict(base_params)
            if page_token is not None:
                params["pageToken"] = page_token
            payload = await self.api_get(
                path,
                params=params,
                use_user_token=_non_empty_string(use_user_token, "use_user_token"),
            )
            events.extend(_extract_calendar_events(payload, method="GET", path=path))
            page_token = _extract_next_page_token(payload, method="GET", path=path)
            if page_token is None:
                return events

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
        for attempt in range(2):
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

            try:
                return _parse_response(response, method=method, path=path)
            except DingTalkAPIError as exc:
                if use_user_token is None and attempt == 0 and _is_access_token_invalid_error(exc):
                    await self._invalidate_cached_access_token(access_token)
                    logger.warning(
                        "dingtalk_access_token_invalid_retrying",
                        extra={
                            "method": method,
                            "path": path,
                            "status_code": exc.status_code,
                            "errcode": exc.errcode,
                            "errmsg": exc.errmsg,
                        },
                    )
                    continue
                raise
        raise RuntimeError("unreachable DingTalk API retry state")

    async def _resolve_access_token(self, use_user_token: str | None) -> str:
        if use_user_token is not None:
            token = use_user_token.strip()
            if token == "":
                raise ValueError("use_user_token must be a non-empty string when provided")
            return token
        return (await self.get_access_token()).access_token

    async def _invalidate_cached_access_token(self, failed_token: str) -> None:
        async with self._token_lock:
            if self._cached_token is not None and self._cached_token.access_token == failed_token:
                self._cached_token = None
                self._refresh_after = 0.0

    def _build_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._config.api_base.rstrip('/')}/{path.lstrip('/')}"


def _parse_response(response: httpx.Response, *, method: str, path: str) -> Any:
    return parse_dingtalk_response(response, method=method, path=path)


def _parse_user_access_token_payload(
    payload: Any,
    *,
    now: datetime,
    method: str,
    path: str,
) -> DingTalkUserAccessToken:
    if not isinstance(payload, Mapping):
        raise _invalid_user_access_token_response(method=method, path=path)

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
        raise _invalid_user_access_token_response(method=method, path=path)

    return DingTalkUserAccessToken(
        access_token=access_token.strip(),
        refresh_token=refresh_token.strip(),
        expire_in=expire_in,
        expires_at=_to_utc(now) + timedelta(seconds=expire_in),
        raw=dict(payload),
    )


def parse_dingtalk_response(response: httpx.Response, *, method: str, path: str) -> Any:
    """Parse DingTalk HTTP responses and raise structured API errors."""

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


def _invalid_user_access_token_response(*, method: str, path: str) -> DingTalkAPIError:
    logger.error(
        "dingtalk_user_token_response_invalid",
        extra={"method": method, "path": path, "status_code": 200},
    )
    return DingTalkAPIError(
        method=method,
        path=path,
        status_code=200,
        errcode=None,
        errmsg=(
            "user access token response must include accessToken, "
            "refreshToken, and positive expireIn"
        ),
    )


def _is_refresh_token_rejected(error: DingTalkAPIError) -> bool:
    details = f"{error.errcode or ''} {error.errmsg or ''}".lower()
    if "invalid_grant" in details:
        return True

    if "refresh" not in details:
        return False

    return any(
        marker in details
        for marker in (
            "invalid",
            "expired",
            "expire",
            "rejected",
            "unauthorized",
            "forbidden",
        )
    )


def _is_access_token_invalid_error(error: DingTalkAPIError) -> bool:
    details = f"{error.errcode or ''} {error.errmsg or ''}".lower()
    if error.status_code == 401:
        return True
    if (
        "access_token" not in details
        and "access token" not in details
        and "accesstoken" not in details
    ):
        return False
    return any(
        marker in details
        for marker in (
            "invalid",
            "expired",
            "expire",
            "unauthorized",
            "forbidden",
            "401",
        )
    )


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


def _normalize_user_ids(user_ids: Sequence[str]) -> list[str]:
    if isinstance(user_ids, (str, bytes)) or len(user_ids) == 0:
        raise ValueError("user_ids must contain at least one userId")

    normalized = [_non_empty_string(user_id, "user_id") for user_id in user_ids]
    return normalized


def _normalize_union_ids(union_ids: Sequence[str]) -> list[str]:
    if isinstance(union_ids, (str, bytes)) or len(union_ids) == 0:
        raise ValueError("executor_union_ids must contain at least one unionId")
    return [_non_empty_string(union_id, "executor_union_id") for union_id in union_ids]


def _text_msg_param(text: str) -> str:
    content = _non_empty_string(text, "text")
    return json.dumps({"content": content}, ensure_ascii=False, separators=(",", ":"))


def _confirm_card_button(
    *,
    text: str,
    color: str,
    correlation_id: str,
    decision: str,
) -> dict[str, str]:
    callback_value = json.dumps(
        {
            "correlation_id": _non_empty_string(correlation_id, "correlation_id"),
            "decision": _non_empty_string(decision, "decision"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    title = _non_empty_string(text, "button_text")
    return {
        "text": title,
        "title": title,
        "actionType": "callback",
        "value": callback_value,
        "color": _non_empty_string(color, "button_color"),
    }


def _confirm_card_markdown(
    *,
    action: str,
    details: Mapping[str, Any],
    expires_at: datetime | None,
) -> str:
    lines = [
        "### 请确认是否执行该操作",
        "",
        f"**操作**：{_non_empty_string(action, 'action')}",
        "",
        "**详情**：",
        "```json",
        json.dumps(_plain_json_object(details, "details"), ensure_ascii=False, indent=2),
        "```",
    ]
    if expires_at is not None:
        lines.extend(["", f"**过期时间**：{_to_utc(expires_at).isoformat()}"])
    return "\n".join(lines)


def _confirm_card_deliver_body(
    *,
    conversation_type: int,
    conversation_id: str,
    open_conversation_id: str | None,
    responder_user_id: str,
    robot_code: str,
    correlation_id: str,
) -> dict[str, Any]:
    normalized_conversation_id = _non_empty_string(conversation_id, "conversation_id")
    normalized_responder = _non_empty_string(responder_user_id, "responder_user_id")
    body: dict[str, Any] = {
        "outTrackId": _non_empty_string(correlation_id, "correlation_id"),
        "userIdType": 1,
    }
    if conversation_type == 1:
        body["openSpaceId"] = f"dtv1.card//IM_ROBOT.{normalized_responder}"
        body["imRobotOpenDeliverModel"] = {"spaceType": "IM_ROBOT"}
        return body
    if conversation_type == 2:
        body["openSpaceId"] = f"dtv1.card//IM_GROUP.{normalized_conversation_id}"
        body["imGroupOpenDeliverModel"] = {
            "robotCode": _non_empty_string(robot_code, "robot_code"),
            "recipients": [normalized_responder],
        }
        if open_conversation_id is not None:
            body["imGroupOpenDeliverModel"]["extension"] = {
                "openConversationId": _non_empty_string(
                    open_conversation_id,
                    "open_conversation_id",
                )
            }
        return body
    raise ValueError("conversation_type must be 1 or 2")


def _plain_json_object(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return {
        _non_empty_string(key, f"{field_name}.key"): _plain_json_value(
            nested_value,
            f"{field_name}.{key}",
        )
        for key, nested_value in value.items()
    }


def _plain_json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return _plain_json_object(value, field_name)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_json_value(item, field_name) for item in value]
    raise ValueError(f"{field_name} must be JSON-compatible")


def _normalize_identifier(value: str | int, field_name: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-empty string or positive integer")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"{field_name} must be greater than 0")
        return str(value)
    return _non_empty_string(value, field_name)


def _normalize_page_size(page_size: int) -> int:
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise ValueError("page_size must be an integer")
    if page_size <= 0 or page_size > MAX_CONTACT_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_CONTACT_PAGE_SIZE}")
    return page_size


def _non_negative_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _non_empty_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _calendar_query_time(value: datetime | str, field_name: str) -> str:
    if isinstance(value, datetime):
        return _to_utc(value).isoformat().replace("+00:00", "Z")
    return _non_empty_string(value, field_name)


def _detail_url_mapping(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("detail_url must be a mapping")
    normalized: dict[str, str] = {}
    for key in ("url", "pcUrl"):
        raw_value = value.get(key)
        if raw_value is not None:
            normalized[key] = _non_empty_string(raw_value, f"detail_url.{key}")
    if not normalized:
        raise ValueError("detail_url must include url or pcUrl")
    return normalized


def _quote_path_segment(value: str) -> str:
    return quote(value, safe="")


def _extract_contact_users(payload: Any, *, method: str, path: str) -> list[DingTalkUser]:
    payload_object = _response_object(payload, method=method, path=path)
    raw_users = _first_present(payload_object, "users", "list")

    if raw_users is None and isinstance(payload_object.get("result"), Mapping):
        raw_users = _first_present(payload_object["result"], "users", "list")

    if not isinstance(raw_users, list):
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=200,
            errcode=None,
            errmsg="contact user list response must include a users list",
        )

    return [_parse_contact_user(raw_user, method=method, path=path) for raw_user in raw_users]


def _parse_contact_user(payload: Any, *, method: str, path: str) -> DingTalkUser:
    payload_object = _response_object(payload, method=method, path=path)
    if isinstance(payload_object.get("result"), Mapping):
        payload_object = payload_object["result"]

    user_id = _string_field(payload_object, "userId", "userid")
    name = _string_field(payload_object, "name", "username")
    union_id = _string_field(payload_object, "unionId", "unionid")
    if user_id is None or name is None:
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=200,
            errcode=None,
            errmsg="contact user response must include userId and name",
        )

    return DingTalkUser(user_id=user_id, name=name, raw=dict(payload_object), union_id=union_id)


def _parse_document(payload: Any, *, method: str, path: str) -> DingTalkDocument:
    payload_object = _response_object(payload, method=method, path=path)
    result_object = _nested_result_object(payload_object)
    doc_id = _string_field(result_object, "docId", "documentId", "id")
    if doc_id is None:
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=200,
            errcode=None,
            errmsg="document response must include docId",
        )
    url = _string_field(result_object, "url", "webUrl", "docUrl")
    return DingTalkDocument(doc_id=doc_id, url=url, raw=dict(result_object))


def _parse_todo(payload: Any, *, method: str, path: str) -> DingTalkTodo:
    payload_object = _response_object(payload, method=method, path=path)
    result_object = _nested_result_object(payload_object)
    task_id = _string_field(result_object, "taskId", "todoId", "id")
    if task_id is None:
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=200,
            errcode=None,
            errmsg="todo response must include taskId or todoId",
        )
    return DingTalkTodo(task_id=task_id, raw=dict(result_object))


def _parse_calendar(payload: Any, *, method: str, path: str) -> DingTalkCalendar:
    payload_object = _response_object(payload, method=method, path=path)
    result_object = _nested_result_object(payload_object)
    calendar_id = _string_field(result_object, "calendarId", "id")
    if calendar_id is None:
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=200,
            errcode=None,
            errmsg="calendar response must include calendarId",
        )
    return DingTalkCalendar(
        calendar_id=calendar_id,
        summary=_string_field(result_object, "summary", "name"),
        time_zone=_string_field(result_object, "timeZone", "timezone"),
        raw=dict(result_object),
    )


def _extract_calendar_events(
    payload: Any, *, method: str, path: str
) -> list[DingTalkCalendarEvent]:
    payload_object = _response_object(payload, method=method, path=path)
    raw_events = _first_present(payload_object, "events", "items", "list")
    result = payload_object.get("result")
    if raw_events is None and isinstance(result, Mapping):
        raw_events = _first_present(result, "events", "items", "list")
    if raw_events is None and isinstance(result, list):
        raw_events = result

    if not isinstance(raw_events, list):
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=200,
            errcode=None,
            errmsg="calendar events response must include an events list",
        )

    return [_parse_calendar_event(raw_event, method=method, path=path) for raw_event in raw_events]


def _parse_calendar_event(payload: Any, *, method: str, path: str) -> DingTalkCalendarEvent:
    event_object = _response_object(payload, method=method, path=path)
    return DingTalkCalendarEvent(
        event_id=_string_field(event_object, "eventId", "event_id", "id"),
        summary=_string_field(event_object, "summary", "title", "subject"),
        description=_string_field(event_object, "description", "body"),
        start_time=_calendar_event_time(event_object.get("start")),
        end_time=_calendar_event_time(event_object.get("end")),
        location=_calendar_event_location(event_object.get("location")),
        raw=dict(event_object),
    )


def _calendar_event_time(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, Mapping):
        return _string_field(value, "dateTime", "datetime", "date", "time")
    return None


def _calendar_event_location(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, Mapping):
        return _string_field(value, "displayName", "name", "address")
    return None


def _nested_result_object(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    result = payload.get("result")
    if isinstance(result, Mapping):
        return result
    return payload


def _extract_next_page_token(payload: Any, *, method: str, path: str) -> str | None:
    payload_object = _response_object(payload, method=method, path=path)
    raw_token = _first_present(payload_object, "nextPageToken", "nextToken")

    if raw_token is None and isinstance(payload_object.get("result"), Mapping):
        raw_token = _first_present(payload_object["result"], "nextPageToken", "nextToken")

    if raw_token is None:
        return None
    if not isinstance(raw_token, str):
        raise DingTalkAPIError(
            method=method,
            path=path,
            status_code=200,
            errcode=None,
            errmsg="contact user list next page token must be a string",
        )

    token = raw_token.strip()
    return token or None


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


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _string_field(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()
    return None
