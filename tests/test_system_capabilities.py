"""Tests for built-in application-level DingTalk system capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from src.capabilities import (
    Capability,
    CredentialContext,
    CredentialHandle,
    Requirement,
    can_use,
    load_capability_registry,
)
from src.capabilities.system.contact_lookup import CAPABILITY as CONTACT_LOOKUP
from src.capabilities.system.create_doc import CAPABILITY as CREATE_DOC
from src.capabilities.system.create_todo import CAPABILITY as CREATE_TODO
from src.capabilities.system.schedule_summary import CAPABILITY as SCHEDULE_SUMMARY
from src.capabilities.system.send_notification import CAPABILITY as SEND_NOTIFICATION
from src.core import (
    Actor,
    BotIdentity,
    CapabilityExecutionContext,
    Principal,
    Session,
)
from src.infra.dingtalk_client import (
    DingTalkCalendar,
    DingTalkCalendarEvent,
    DingTalkDocument,
    DingTalkTodo,
    DingTalkUser,
)


def test_system_registry_loads_t18_application_tools() -> None:
    """The system tier should declare the first app-level no-OBO tools."""

    registry = load_capability_registry()

    assert {
        "contact_lookup",
        "create_doc",
        "create_todo",
        "schedule_summary",
        "send_notification",
    }.issubset(registry.names())
    create_doc = registry.get("create_doc")
    assert create_doc is not None
    assert create_doc.available_in == ("dm", "group")
    assert create_doc.requires == ()
    schedule_summary = registry.get("schedule_summary")
    assert schedule_summary is not None
    assert schedule_summary.available_in == ("dm",)
    assert schedule_summary.requires == (
        Requirement(service="calendar", scopes=["calendar:read"], on_behalf_of="actor"),
    )
    assert can_use(schedule_summary, "dm", Actor(id="user-1", display_name="Alice"), None) is True
    assert (
        can_use(schedule_summary, "group", Actor(id="user-1", display_name="Alice"), None) is False
    )
    send_notification = registry.get("send_notification")
    assert send_notification is not None
    assert send_notification.sensitivity == "high"


@pytest.mark.asyncio
async def test_contact_lookup_defaults_to_current_actor_user_id() -> None:
    """Looking up without explicit arguments should return the current actor."""

    client = FakeDingTalkToolClient()
    context = _context(CONTACT_LOOKUP, client)

    result = await CONTACT_LOOKUP.handler(context)

    assert result == {
        "query": {"user_id": "user-1"},
        "count": 1,
        "matches": [{"user_id": "user-1", "name": "Alice", "union_id": "union-1"}],
    }
    assert client.calls == [("user_by_id", "user-1")]


@pytest.mark.asyncio
async def test_contact_lookup_searches_names_in_department() -> None:
    """Name lookup should search the contact mapping returned by the client."""

    client = FakeDingTalkToolClient()
    context = _context(CONTACT_LOOKUP, client)

    result = await CONTACT_LOOKUP.handler(
        context,
        name="ali",
        match_mode="contains",
        department_id="42",
    )

    assert result == {
        "query": {"name": "ali", "match_mode": "contains", "department_id": "42"},
        "count": 1,
        "matches": [{"user_id": "user-1", "name": "Alice"}],
    }
    assert client.calls == [("get_user_list", "42")]


@pytest.mark.asyncio
async def test_create_doc_uses_defaults_creates_document_and_appends_content() -> None:
    """The document capability should create a doc and write the requested content."""

    client = FakeDingTalkToolClient()
    context = _context(
        CREATE_DOC,
        client,
        document_defaults={
            "parent_object_type": "wiki_space",
            "parent_object_id": "space-1",
        },
    )

    result = await CREATE_DOC.handler(
        context,
        title="会议纪要",
        content="今天讨论 T18。",
    )

    assert result == {
        "doc_id": "doc-1",
        "url": "https://docs.example.com/doc-1",
        "title": "会议纪要",
        "parent_object_type": "wiki_space",
        "parent_object_id": "space-1",
        "content_appended": True,
        "append_result": {"blockId": "block-1"},
    }
    assert client.calls == [
        ("create_document", "会议纪要", "wiki_space", "space-1"),
        ("append_document_content", "doc-1", "今天讨论 T18。"),
    ]


@pytest.mark.asyncio
async def test_create_doc_requires_parent_id_when_default_missing() -> None:
    """A clear handler error should be returned when no document parent is configured."""

    client = FakeDingTalkToolClient()
    context = _context(CREATE_DOC, client)

    with pytest.raises(ValueError, match="parent_object_id"):
        await CREATE_DOC.handler(context, title="No Parent", content="body")


@pytest.mark.asyncio
async def test_create_todo_resolves_actor_union_id_and_creates_task() -> None:
    """Todo creation should use app-level userId to unionId lookup before creating a task."""

    client = FakeDingTalkToolClient()
    context = _context(CREATE_TODO, client)

    result = await CREATE_TODO.handler(
        context,
        subject="整理会议纪要",
        description="今天完成",
        due_time=1783377600000,
        priority=1,
    )

    assert result == {
        "task_id": "task-1",
        "subject": "整理会议纪要",
        "creator_union_id": "union-1",
        "assignee_union_id": "union-1",
    }
    assert client.calls == [
        ("user_by_id", "user-1"),
        (
            "create_todo",
            {
                "union_id": "union-1",
                "subject": "整理会议纪要",
                "creator_union_id": "union-1",
                "executor_union_ids": ["union-1"],
                "description": "今天完成",
                "due_time": 1783377600000,
                "priority": 1,
                "detail_url": None,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_send_notification_requires_confirm_before_sending() -> None:
    """The notification tool should use ctx.confirm before sending through DingTalk."""

    client = FakeDingTalkToolClient()
    confirmation = FakeConfirmation()
    context = _context(SEND_NOTIFICATION, client, confirmation=confirmation)

    result = await SEND_NOTIFICATION.handler(context, content="请大家 3 点开会")

    assert confirmation.calls == [
        ("发送钉钉通知", {"target": "user:user-1", "content": "请大家 3 点开会"})
    ]
    assert client.calls == [("send_oto", ["user-1"], "请大家 3 点开会")]
    assert result["sent"] is True
    assert result["target"] == {"kind": "user", "id": "user-1", "label": "user:user-1"}


@pytest.mark.asyncio
async def test_schedule_summary_reads_actor_calendar_and_summarizes() -> None:
    """The OBO schedule tool should read `me` calendar events with the granted user token."""

    client = FakeDingTalkToolClient()
    llm_client = FakeScheduleLLM("今天上午有晨会，下午有评审。")
    session = _session()
    credentials = CredentialContext.for_session(
        session,
        handles=[
            CredentialHandle.user_token(
                service="calendar",
                user_access_token="user-calendar-token",
                scopes=("calendar:read",),
                principal_id="user:user-1",
                actor_id="user-1",
            )
        ],
    )
    context = _context(
        SCHEDULE_SUMMARY,
        client,
        llm_client=llm_client,
        credentials=credentials,
    )

    result = await SCHEDULE_SUMMARY.handler(context, date="2026-07-07")

    assert result == {
        "date": "2026-07-07",
        "timezone": "Asia/Shanghai",
        "calendar_id": "primary",
        "event_count": 2,
        "summary": "今天上午有晨会，下午有评审。",
    }
    assert client.calls == [
        ("get_primary_calendar", "user-calendar-token"),
        (
            "list_calendar_events",
            {
                "user_id": "me",
                "calendar_id": "primary",
                "start_time": datetime(2026, 7, 6, 16, 0, tzinfo=UTC),
                "end_time": datetime(2026, 7, 7, 16, 0, tzinfo=UTC),
                "use_user_token": "user-calendar-token",
            },
        ),
    ]
    assert len(llm_client.calls) == 1
    assert llm_client.calls[0]["system"].startswith("你是企业内 AI 助手")
    assert "晨会" in llm_client.calls[0]["messages"][0]["content"]
    assert "2026-07-07" in llm_client.calls[0]["messages"][0]["content"]


class FakeDingTalkToolClient:
    """Fake DingTalk client used by system capability tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.users = {
            "user-1": DingTalkUser(
                user_id="user-1",
                name="Alice",
                raw={"userId": "user-1", "unionId": "union-1", "name": "Alice"},
                union_id="union-1",
            ),
            "user-2": DingTalkUser(
                user_id="user-2",
                name="Bob",
                raw={"userId": "user-2", "unionId": "union-2", "name": "Bob"},
                union_id="union-2",
            ),
        }

    async def get_user_list(self, *, department_id: str = "1", **_kwargs: Any) -> dict[str, str]:
        self.calls.append(("get_user_list", department_id))
        return {user.user_id: user.name for user in self.users.values()}

    async def user_by_id(self, user_id: str) -> DingTalkUser:
        self.calls.append(("user_by_id", user_id))
        return self.users[user_id]

    async def create_document(
        self,
        *,
        title: str,
        parent_object_type: str,
        parent_object_id: str,
    ) -> DingTalkDocument:
        self.calls.append(("create_document", title, parent_object_type, parent_object_id))
        return DingTalkDocument(
            doc_id="doc-1",
            url="https://docs.example.com/doc-1",
            raw={"docId": "doc-1", "url": "https://docs.example.com/doc-1"},
        )

    async def append_document_content(self, doc_id: str, text: str) -> dict[str, str]:
        self.calls.append(("append_document_content", doc_id, text))
        return {"blockId": "block-1"}

    async def create_todo(self, **kwargs: Any) -> DingTalkTodo:
        self.calls.append(("create_todo", kwargs))
        return DingTalkTodo(task_id="task-1", raw={"taskId": "task-1"})

    async def send_oto(self, user_ids: list[str], text: str) -> dict[str, str]:
        self.calls.append(("send_oto", list(user_ids), text))
        return {"messageId": "oto-message"}

    async def send_group(self, open_conversation_id: str, text: str) -> dict[str, str]:
        self.calls.append(("send_group", open_conversation_id, text))
        return {"messageId": "group-message"}

    async def get_primary_calendar(self, *, use_user_token: str) -> DingTalkCalendar:
        self.calls.append(("get_primary_calendar", use_user_token))
        return DingTalkCalendar(
            calendar_id="primary",
            summary="我的主日历",
            time_zone="Asia/Shanghai",
            raw={"calendarId": "primary", "summary": "我的主日历"},
        )

    async def list_calendar_events(self, **kwargs: Any) -> list[DingTalkCalendarEvent]:
        self.calls.append(("list_calendar_events", kwargs))
        return [
            DingTalkCalendarEvent(
                event_id="event-1",
                summary="晨会",
                start_time="2026-07-07T09:00:00+08:00",
                end_time="2026-07-07T09:30:00+08:00",
                location="会议室 A",
                raw={"eventId": "event-1", "summary": "晨会"},
            ),
            DingTalkCalendarEvent(
                event_id="event-2",
                summary="评审",
                start_time="2026-07-07T14:00:00+08:00",
                end_time="2026-07-07T15:00:00+08:00",
                raw={"eventId": "event-2", "summary": "评审"},
            ),
        ]


class FakeScheduleLLM:
    """Fake LLM service used by the schedule summary capability tests."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []

    async def complete(self, system: str, messages: list[dict[str, Any]]) -> str:
        self.calls.append({"system": system, "messages": messages})
        return self._reply


class FakeConfirmation:
    """Fake ctx.confirm implementation for capability unit tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def confirm(self, action: str, details: Mapping[str, Any]) -> bool:
        self.calls.append((action, dict(details)))
        return True


def _context(
    capability: Capability,
    client: FakeDingTalkToolClient,
    *,
    document_defaults: Mapping[str, object] | None = None,
    llm_client: object | None = None,
    credentials: CredentialContext | None = None,
    confirmation: object | None = None,
) -> CapabilityExecutionContext:
    services: dict[str, object] = {"dingtalk_client": client}
    if document_defaults is not None:
        services["dingtalk_document_defaults"] = dict(document_defaults)
    if llm_client is not None:
        services["llm_client"] = llm_client
    return CapabilityExecutionContext(
        session=_session(),
        capability=capability,
        services=services,
        credentials=credentials,
        confirmation=confirmation,
    )


def _session() -> Session:
    return Session(
        session_id="dingtalk:dm:conversation-1",
        conversation_id="conversation-1",
        kind="dm",
        bot=BotIdentity(id="robot-code"),
        principal=Principal(kind="user", id="user:user-1"),
        actor=Actor(id="user-1", display_name="Alice"),
        context={"platform": "dingtalk"},
    )
