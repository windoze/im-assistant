"""Async DingTalk OpenAPI client with application access-token caching."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from src.infra.config import DingTalkConfig
from src.infra.log import get_logger

logger = get_logger(__name__)

ACCESS_TOKEN_PATH = "/v1.0/oauth2/accessToken"
OTO_MESSAGE_PATH = "/v1.0/robot/oToMessages/batchSend"
GROUP_MESSAGE_PATH = "/v1.0/robot/groupMessages/send"
CONTACT_USER_LIST_PATH_TEMPLATE = "/v1.0/contact/departments/{department_id}/users"
CONTACT_USER_BY_ID_PATH_TEMPLATE = "/v1.0/contact/users/{user_id}"
DOCUMENT_CREATE_PATH = "/v1.0/documents"
DOCUMENT_CONTENT_BLOCKS_PATH_TEMPLATE = "/v1.0/documents/{doc_id}/contentBlocks"
TODO_CREATE_PATH_TEMPLATE = "/v1.0/todo/users/{union_id}/tasks"
TOKEN_HEADER = "x-acs-dingtalk-access-token"
TOKEN_REFRESH_SKEW_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CONTACT_DEPARTMENT_ID = "1"
MAX_CONTACT_PAGE_SIZE = 100
TEXT_MESSAGE_KEY = "sampleText"


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
    return parse_dingtalk_response(response, method=method, path=path)


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
