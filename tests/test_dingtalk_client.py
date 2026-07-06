"""Tests for DingTalk OpenAPI token and request handling."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from src.infra.config import DingTalkConfig
from src.infra.dingtalk_client import (
    AccessToken,
    DingTalkAPIError,
    DingTalkCalendar,
    DingTalkCalendarEvent,
    DingTalkClient,
    DingTalkDocument,
    DingTalkTodo,
    DingTalkUser,
    DingTalkUserAccessToken,
    DingTalkUserTokenRefreshRejected,
)


def _config() -> DingTalkConfig:
    return DingTalkConfig(
        app_key="app-key",
        app_secret="app-secret",
        robot_code="robot-code",
        api_base="https://api.example.com",
        legacy_api_base="https://oapi.example.com",
    )


@pytest.mark.asyncio
async def test_get_access_token_fetches_and_caches() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "https://api.example.com/v1.0/oauth2/accessToken"
        assert json.loads(request.content) == {
            "appKey": "app-key",
            "appSecret": "app-secret",
        }
        return httpx.Response(200, json={"accessToken": "token-1", "expireIn": 7200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        first = await client.get_access_token()
        second = await client.get_access_token()

    assert first == AccessToken(access_token="token-1", expire_in=7200)
    assert second == first
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_get_access_token_refreshes_five_minutes_before_expiry() -> None:
    current_time = 0.0
    request_count = 0

    def clock() -> float:
        return current_time

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={"accessToken": f"token-{request_count}", "expireIn": 3600},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client, clock=clock)

        first = await client.get_access_token()
        current_time = 3299.0
        still_cached = await client.get_access_token()
        current_time = 3301.0
        refreshed = await client.get_access_token()

    assert first.access_token == "token-1"
    assert still_cached.access_token == "token-1"
    assert refreshed.access_token == "token-2"
    assert request_count == 2


@pytest.mark.asyncio
async def test_concurrent_get_access_token_uses_single_network_request() -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"accessToken": "shared-token", "expireIn": 7200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        tokens = await asyncio.gather(*(client.get_access_token() for _ in range(10)))

    assert {token.access_token for token in tokens} == {"shared-token"}
    assert request_count == 1


@pytest.mark.asyncio
async def test_api_post_uses_application_token_header() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.method == "POST"
        assert request.url.path == "/v1.0/example"
        assert request.headers["x-acs-dingtalk-access-token"] == "app-token"
        assert json.loads(request.content) == {"hello": "world"}
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        payload = await client.api_post("/v1.0/example", {"hello": "world"})

    assert payload == {"ok": True}
    assert paths == ["/v1.0/oauth2/accessToken", "/v1.0/example"]


@pytest.mark.asyncio
async def test_api_get_uses_supplied_user_token_without_fetching_app_token() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.url.path == "/v1.0/users"
        assert request.url.params["name"] == "alice"
        assert request.headers["x-acs-dingtalk-access-token"] == "user-token"
        return httpx.Response(200, json={"result": ["user-1"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        payload = await client.api_get(
            "v1.0/users",
            params={"name": "alice"},
            use_user_token="user-token",
        )

    assert payload == {"result": ["user-1"]}
    assert paths == ["/v1.0/users"]


@pytest.mark.asyncio
async def test_refresh_user_access_token_posts_refresh_grant_and_parses_expiry() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1.0/oauth2/userAccessToken"
        assert json.loads(request.content) == {
            "clientId": "app-key",
            "clientSecret": "app-secret",
            "refreshToken": "old-refresh-token",
            "grantType": "refresh_token",
        }
        return httpx.Response(
            200,
            json={
                "accessToken": "new-user-token",
                "refreshToken": "new-refresh-token",
                "expireIn": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client, now_factory=lambda: now)

        token = await client.refresh_user_access_token(" old-refresh-token ")

    assert token == DingTalkUserAccessToken(
        access_token="new-user-token",
        refresh_token="new-refresh-token",
        expire_in=3600,
        expires_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        raw={
            "accessToken": "new-user-token",
            "refreshToken": "new-refresh-token",
            "expireIn": 3600,
        },
    )
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_refresh_user_access_token_rejects_invalid_refresh_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": "invalid_grant", "message": "refresh token expired"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        with pytest.raises(DingTalkUserTokenRefreshRejected) as exc_info:
            await client.refresh_user_access_token("expired-refresh-token")

    assert exc_info.value.error.status_code == 400
    assert exc_info.value.error.errcode == "invalid_grant"


@pytest.mark.asyncio
async def test_refresh_user_access_token_does_not_revoke_on_unrelated_oauth_error() -> None:
    """Only refresh-token-specific OAuth errors should be treated as revocable grants."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"code": "forbidden_app", "message": "application is forbidden"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        with pytest.raises(DingTalkAPIError) as exc_info:
            await client.refresh_user_access_token("old-refresh-token")

    assert not isinstance(exc_info.value, DingTalkUserTokenRefreshRejected)
    assert exc_info.value.status_code == 403
    assert exc_info.value.errcode == "forbidden_app"


@pytest.mark.asyncio
async def test_send_oto_posts_robot_text_message() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.method == "POST"
        assert request.url.path == "/v1.0/robot/oToMessages/batchSend"
        assert request.headers["x-acs-dingtalk-access-token"] == "app-token"
        body = json.loads(request.content)
        assert body["robotCode"] == "robot-code"
        assert body["userIds"] == ["user-1", "user-2"]
        assert body["msgKey"] == "sampleText"
        assert json.loads(body["msgParam"]) == {"content": "hello"}
        return httpx.Response(200, json={"processQueryKey": "query-key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        payload = await client.send_oto([" user-1 ", "user-2"], "hello")

    assert payload == {"processQueryKey": "query-key"}
    assert paths == ["/v1.0/oauth2/accessToken", "/v1.0/robot/oToMessages/batchSend"]


@pytest.mark.asyncio
async def test_send_group_posts_robot_text_message() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.method == "POST"
        assert request.url.path == "/v1.0/robot/groupMessages/send"
        body = json.loads(request.content)
        assert body["robotCode"] == "robot-code"
        assert body["openConversationId"] == "cid-1"
        assert body["msgKey"] == "sampleText"
        assert json.loads(body["msgParam"]) == {"content": "group hello"}
        return httpx.Response(200, json={"messageId": "message-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        payload = await client.send_group(" cid-1 ", "group hello")

    assert payload == {"messageId": "message-1"}
    assert paths == ["/v1.0/oauth2/accessToken", "/v1.0/robot/groupMessages/send"]


@pytest.mark.asyncio
async def test_get_user_list_returns_user_id_name_mapping_across_pages() -> None:
    contact_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        contact_requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1.0/contact/departments/1/users"
        assert request.headers["x-acs-dingtalk-access-token"] == "app-token"
        assert request.url.params["maxResults"] == "2"
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "users": [{"userId": "user-1", "name": "Alice"}],
                    "nextPageToken": "next-page",
                },
            )

        assert request.url.params["pageToken"] == "next-page"
        return httpx.Response(200, json={"users": [{"userId": "user-2", "name": "Bob"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        users = await client.get_user_list(page_size=2)

    assert users == {"user-1": "Alice", "user-2": "Bob"}
    assert len(contact_requests) == 2


@pytest.mark.asyncio
async def test_user_by_id_fetches_and_normalizes_contact_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.method == "GET"
        assert request.url.path == "/v1.0/contact/users/user-1"
        return httpx.Response(
            200,
            json={"userId": "user-1", "name": "Alice", "email": "alice@example.com"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        user = await client.user_by_id(" user-1 ")

    assert user == DingTalkUser(
        user_id="user-1",
        name="Alice",
        raw={"userId": "user-1", "name": "Alice", "email": "alice@example.com"},
    )


@pytest.mark.asyncio
async def test_user_by_id_preserves_union_id_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        return httpx.Response(
            200,
            json={"userId": "user-1", "unionId": "union-1", "name": "Alice"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        user = await client.user_by_id("user-1")

    assert user.union_id == "union-1"


@pytest.mark.asyncio
async def test_create_document_posts_parent_and_parses_document() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.method == "POST"
        assert request.url.path == "/v1.0/documents"
        assert request.headers["x-acs-dingtalk-access-token"] == "app-token"
        assert json.loads(request.content) == {
            "title": "Meeting Notes",
            "parentObjectType": "wiki_space",
            "parentObjectId": "space-1",
        }
        return httpx.Response(
            200,
            json={"result": {"docId": "doc-1", "url": "https://docs.example.com/doc-1"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        document = await client.create_document(
            title="Meeting Notes",
            parent_object_type="wiki_space",
            parent_object_id="space-1",
        )

    assert document == DingTalkDocument(
        doc_id="doc-1",
        url="https://docs.example.com/doc-1",
        raw={"docId": "doc-1", "url": "https://docs.example.com/doc-1"},
    )
    assert paths == ["/v1.0/oauth2/accessToken", "/v1.0/documents"]


@pytest.mark.asyncio
async def test_append_document_content_posts_text_block() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.method == "POST"
        assert request.url.path == "/v1.0/documents/doc-1/contentBlocks"
        assert json.loads(request.content) == {
            "contentBlockType": "text",
            "blockContent": {"text": "hello doc"},
        }
        return httpx.Response(200, json={"blockId": "block-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        payload = await client.append_document_content("doc-1", "hello doc")

    assert payload == {"blockId": "block-1"}
    assert paths == ["/v1.0/oauth2/accessToken", "/v1.0/documents/doc-1/contentBlocks"]


@pytest.mark.asyncio
async def test_create_todo_posts_union_id_task_and_parses_result() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})

        assert request.method == "POST"
        assert request.url.path == "/v1.0/todo/users/union-1/tasks"
        assert json.loads(request.content) == {
            "subject": "Submit report",
            "creatorId": "creator-union",
            "executorIds": ["union-1"],
            "description": "before Friday",
            "dueTime": 1783377600000,
            "priority": 1,
            "detailUrl": {"url": "https://example.com/todo"},
        }
        return httpx.Response(200, json={"result": {"taskId": "task-1"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        todo = await client.create_todo(
            union_id="union-1",
            subject="Submit report",
            creator_union_id="creator-union",
            executor_union_ids=["union-1"],
            description="before Friday",
            due_time=1783377600000,
            priority=1,
            detail_url={"url": "https://example.com/todo"},
        )

    assert todo == DingTalkTodo(task_id="task-1", raw={"taskId": "task-1"})
    assert paths == ["/v1.0/oauth2/accessToken", "/v1.0/todo/users/union-1/tasks"]


@pytest.mark.asyncio
async def test_get_primary_calendar_uses_user_token_and_parses_calendar() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1.0/calendar/primary"
        assert request.headers["x-acs-dingtalk-access-token"] == "user-token"
        return httpx.Response(
            200,
            json={
                "result": {
                    "calendarId": "primary",
                    "summary": "我的主日历",
                    "timeZone": "Asia/Shanghai",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        calendar = await client.get_primary_calendar(use_user_token=" user-token ")

    assert calendar == DingTalkCalendar(
        calendar_id="primary",
        summary="我的主日历",
        time_zone="Asia/Shanghai",
        raw={"calendarId": "primary", "summary": "我的主日历", "timeZone": "Asia/Shanghai"},
    )
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_list_calendar_events_queries_range_with_user_token_and_paginates() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1.0/calendar/users/me/calendars/primary/events"
        assert request.headers["x-acs-dingtalk-access-token"] == "user-token"
        assert request.url.params["startTime"] == "2026-07-06T16:00:00Z"
        assert request.url.params["endTime"] == "2026-07-07T16:00:00Z"
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "eventId": "event-1",
                            "summary": "晨会",
                            "description": "同步项目进展",
                            "start": {"dateTime": "2026-07-07T09:00:00+08:00"},
                            "end": {"dateTime": "2026-07-07T09:30:00+08:00"},
                            "location": {"displayName": "会议室 A"},
                        }
                    ],
                    "nextPageToken": "next-page",
                },
            )

        assert request.url.params["pageToken"] == "next-page"
        return httpx.Response(
            200,
            json={"result": {"events": [{"eventId": "event-2", "subject": "评审"}]}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        events = await client.list_calendar_events(
            user_id="me",
            calendar_id="primary",
            start_time=datetime(2026, 7, 6, 16, 0, tzinfo=UTC),
            end_time=datetime(2026, 7, 7, 16, 0, tzinfo=UTC),
            use_user_token="user-token",
        )

    assert events == [
        DingTalkCalendarEvent(
            event_id="event-1",
            summary="晨会",
            description="同步项目进展",
            start_time="2026-07-07T09:00:00+08:00",
            end_time="2026-07-07T09:30:00+08:00",
            location="会议室 A",
            raw={
                "eventId": "event-1",
                "summary": "晨会",
                "description": "同步项目进展",
                "start": {"dateTime": "2026-07-07T09:00:00+08:00"},
                "end": {"dateTime": "2026-07-07T09:30:00+08:00"},
                "location": {"displayName": "会议室 A"},
            },
        ),
        DingTalkCalendarEvent(
            event_id="event-2",
            summary="评审",
            raw={"eventId": "event-2", "subject": "评审"},
        ),
    ]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_api_error_logs_errcode_and_errmsg(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 88, "errmsg": "bad request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DingTalkClient(_config(), http_client=http_client)

        with caplog.at_level(logging.ERROR), pytest.raises(DingTalkAPIError) as exc_info:
            await client.api_post("/v1.0/fail", {}, use_user_token="user-token")

    assert exc_info.value.errcode == 88
    assert exc_info.value.errmsg == "bad request"
    assert any(record.errcode == 88 and record.errmsg == "bad request" for record in caplog.records)
